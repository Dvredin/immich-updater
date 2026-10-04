"""Private state, immutable image identities and candidate preparation.

The active single-stack controller is simple_update.py. Historical cold-filesystem
checkpoint and automatic restoration live in archive/transaction.py, never imported
or installed by this module.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from pathlib import Path

import requests

from compose_runtime import Compose, UpdaterError, private_json, run
from risk_checks import version

SERVICES = {"immich-server", "immich-machine-learning", "database", "redis"}


def sync_parent(path):
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    target = path.with_name("." + path.name + ".new-" + uuid.uuid4().hex)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fchmod(out.fileno(), mode)
        os.fsync(out.fileno())
    os.replace(target, path)
    sync_parent(path)


def state_save(path, state):
    atomic_bytes(path, json.dumps(state, sort_keys=True).encode())


def private_root(path):
    path = Path(path)
    if path.is_symlink() or path.absolute() != path.resolve():
        raise UpdaterError("State root and its parents must not be symbolic links.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise UpdaterError("State root must be mode 0700.")
    return path.resolve()


def pinned(config):
    result = copy.deepcopy(config)
    for name, service in result["services"].items():
        data = json.loads(run(["docker", "image", "inspect", service["image"]]))[0]
        # Content-addressed local image ID; never re-pull mutable tags during recovery.
        service["image"] = data["Id"]
        service["pull_policy"] = "never"
    return result


def running_pinned(stack, *, config=None):
    """Checkpoint the images actually used by old containers, never moved tags."""
    captured = config is not None
    config = stack.config() if config is None else config
    ids = stack.call("ps", "-aq").decode().split()
    if not ids:
        raise UpdaterError("No old runtime containers for checkpoint provenance.")
    items = json.loads(run(["docker", "inspect", *ids]))
    found = {}
    for item in items:
        labels = item.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != config["name"]:
            raise UpdaterError("Old container belongs to another project.")
        name = labels.get("com.docker.compose.service")
        if name not in SERVICES or name in found:
            raise UpdaterError("Old runtime has extra/duplicate services.")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("Image", "")):
            raise UpdaterError(
                "Old runtime has invalid content-addressed image identity."
            )
        if captured:
            from simple_update import verify_mounts

            verify_mounts(config, name, item)
        found[name] = item["Image"]
    if set(found) != SERVICES:
        raise UpdaterError("Every stock old service must have a concrete container.")
    result = copy.deepcopy(config)
    for name, image in found.items():
        result["services"][name]["image"] = image
        result["services"][name]["pull_policy"] = "never"
    return result


def candidate_config(source_path, selected, state_dir):
    """Prepare the exact release while the original single stack remains available.

    Preserve verified site mappings and pull explicit images before any downtime.
    No rehearsal or filesystem checkpoint is part of candidate preparation.
    """
    from simple_update import runtime_config

    version(selected)
    source = Compose(source_path)
    original = runtime_config(source)
    if set(original.get("services", {})) != SERVICES:
        raise UpdaterError("Unsupported production service layout.")
    for name, item in original["services"].items():
        if any(
            item.get(key)
            for key in (
                "privileged",
                "devices",
                "network_mode",
                "pid",
                "ipc",
                "cap_add",
                "entrypoint",
            )
        ):
            raise UpdaterError(
                "Source hardware/host/custom-entrypoint features need a verified backend."
            )
        if name == "immich-machine-learning" and re.search(
            r"-(cuda|rocm|openvino|armnn|rknn)(?:@|$)", item.get("image", "")
        ):
            raise UpdaterError("Accelerated source cannot be silently changed to CPU.")
    requested_state = Path(state_dir).absolute()
    checked_roots = {
        Path(m["source"]).resolve()
        for service in original["services"].values()
        for m in service.get("volumes", [])
        if m.get("type") == "bind" and m.get("target") != "/etc/localtime"
    }
    for root in checked_roots:
        if (
            root == requested_state
            or root in requested_state.parents
            or requested_state in root.parents
        ):
            raise UpdaterError(
                "Candidate state storage overlaps source data; no captured settings may be written."
            )
    state_dir = private_root(state_dir)
    work = state_dir / ("candidate-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    response = requests.get(
        "https://raw.githubusercontent.com/immich-app/immich/"
        + selected
        + "/docker/docker-compose.yml",
        timeout=30,
    )
    response.raise_for_status()
    if not response.content or len(response.content) > 262144:
        raise UpdaterError("Invalid official Compose template size.")
    template = work / "official.yml"
    atomic_bytes(template, response.content)

    server_mounts = {
        m["target"]: m for m in original["services"]["immich-server"].get("volumes", [])
    }
    db_mounts = {
        m["target"]: m for m in original["services"]["database"].get("volumes", [])
    }
    if "/data" not in server_mounts or "/var/lib/postgresql/data" not in db_mounts:
        raise UpdaterError("Unsupported stock data layout.")
    # Parse upstream execution/image definitions with neutral values only. Real
    # paths/[[HERMES-PII:v1:PASSWORD:282a07f5bad82994:YatoQI4Dv_nu-RON:95c817d5af8705cd323589173f22b103]]s are already resolved and are copied after template parsing.
    # Placeholders are binds, so stock named volumes need no premature declaration.
    values = {
        "IMMICH_VERSION": selected,
        "UPLOAD_LOCATION": "/immich-updater-template/media",
        "DB_DATA_LOCATION": "/immich-updater-template/database",
        "DB_PASSWORD": "immich-updater-template-placeholder",
        "DB_USERNAME": "postgres",
        "DB_DATABASE_NAME": "immich",
    }
    atomic_bytes(
        work / ".env",
        ("\n".join(k + "=" + v for k, v in values.items()) + "\n").encode(),
    )
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COMPOSE_") and k not in values
    }
    environment.update(values)
    resolved = Compose(template).config(environment=environment)
    if set(resolved.get("services", {})) != SERVICES:
        raise UpdaterError(
            "New official topology requires an unsupported adapter; production unchanged."
        )
    resolved["name"] = original["name"]
    for name, service in resolved["services"].items():
        old = original["services"][name]
        if "container_name" in old:
            service["container_name"] = old["container_name"]
        else:
            service.pop("container_name", None)
        # Keep official image, command and health checks, but retain site configuration.
        service["environment"] = copy.deepcopy(old.get("environment", {}))
        service["volumes"] = copy.deepcopy(old.get("volumes", []))
        for field in (
            "ports",
            "restart",
            "networks",
            "user",
            "mem_limit",
            "memswap_limit",
            "mem_swappiness",
            "oom_kill_disable",
            "oom_score_adj",
            "cgroup_parent",
            "cpus",
            "shm_size",
        ):
            if field in old:
                service[field] = copy.deepcopy(old[field])
            elif field in ("ports", "networks"):
                service.pop(field, None)
        if name.startswith("immich-"):
            wanted = "ghcr.io/immich-app/" + name + ":" + selected
            if service.get("image") != wanted:
                raise UpdaterError(
                    "Official template is not bound to the selected stable images."
                )
        if name == "database":
            image = service.get("image", "")
            if not (
                image.startswith("ghcr.io/immich-app/postgres:")
                or image.startswith("tensorchord/")
                or image.startswith("postgres:")
            ):
                raise UpdaterError("Unexpected upstream PostgreSQL image.")
    for field in ("volumes", "networks"):
        if field in original:
            resolved[field] = copy.deepcopy(original[field])
    path = work / "candidate.json"
    private_json(path, resolved)
    # Pull before stopping any source service. Only the explicit chosen images.
    images = {service["image"] for service in resolved["services"].values()}
    for image in sorted(images):
        run(["docker", "pull", image], timeout=1800)
    resolved = pinned(resolved)
    private_json(path, resolved)
    return path
