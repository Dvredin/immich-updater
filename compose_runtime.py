"""Docker Compose transport, literal settings and safe diagnostics for the active updater.

No clone lifecycle, memory policy, migration rehearsal or recovery controller.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class UpdaterError(RuntimeError):
    diagnostic: dict[str, Any] = {}


NODE_HTTP = r"""
let s='';for await(const b of process.stdin)s+=b;
const q=JSON.parse(s), headers=q.headers||{};
let body;
if(q.file){const fs=await import('node:fs/promises');const bytes=await fs.readFile(q.file);
const form=new FormData();form.append('assetData',new Blob([bytes]),q.filename||'rehearsal.jpg');
for(const [k,v] of Object.entries(q.fields||{}))form.append(k,v);body=form;
}else if(q.body!==undefined){headers['content-type']='application/json';body=JSON.stringify(q.body);}
const r=await fetch('http://127.0.0.1:2283'+q.path,{method:q.method||'GET',headers,body});
const c=await import('node:crypto'), hash=c.createHash('sha256');
let length=0, chunks=[];
if(r.body){for await(const chunk of r.body){length+=chunk.length;hash.update(chunk);if(length<=1048576)chunks.push(chunk);else chunks=[];}}
let data;if(length<=1048576){try{data=JSON.parse(Buffer.concat(chunks).toString())}catch{}}
process.stdout.write(JSON.stringify({status:r.status,data,bytes:length,sha256:hash.digest('hex')}));
"""


def dollar_literals(value: Any, *, encode: bool) -> Any:
    """Compose config output escapes '$'; resolved models keep literal values."""
    if isinstance(value, str):
        return value.replace("$", "$$") if encode else value.replace("$$", "$")
    if isinstance(value, list):
        return [dollar_literals(v, encode=encode) for v in value]
    if isinstance(value, dict):
        return {k: dollar_literals(v, encode=encode) for k, v in value.items()}
    return value


def compose_bytes(config):
    return json.dumps(dollar_literals(config, encode=True), ensure_ascii=False).encode()


def command_failure(command, returncode, stderr):
    """Expose only operation/code/classifications, never command args or stderr."""
    operation = "other"
    if command[:2] == ["docker", "pull"]:
        operation = "image_pull"
    elif command[:3] == ["docker", "image", "inspect"]:
        operation = "image_inspect"
    elif command[:3] == ["docker", "volume", "inspect"]:
        operation = "volume_inspect"
    elif command[:2] == ["docker", "inspect"]:
        operation = "container_inspect"
    elif command[:2] == ["docker", "compose"]:
        for verb in ("config", "ps", "stop", "up", "exec", "create", "down"):
            if verb in command:
                operation = "compose_" + verb
                break
        if "pg_dump" in command:
            operation = "database_dump"
        elif "pg_restore" in command:
            operation = "database_archive_check"
    text = (stderr or b"").lower()
    if isinstance(text, str):
        text = text.encode()
    patterns = {
        "invalid_interpolation": b"invalid interpolation|invalid template",
        "undefined_volume": b"undefined volume",
        "disk_full": b"no space left",
        "registry_denied": b"unauthorized|denied",
        "connection": b"connection|certificate|timeout",
        "permission": b"permission",
        "manifest": b"manifest",
    }
    error = UpdaterError("Command failed; see safe operation/exit-status diagnostics.")
    error.diagnostic = {
        "error_code": "command_failed",
        "operation": operation,
        "exit_status": returncode,
        "hints": [key for key, pattern in patterns.items() if re.search(pattern, text)],
    }
    return error


def private_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def run(
    command, *, payload=None, timeout=60, stdout=None, stdin=None, environment=None
):
    result = subprocess.run(
        command,
        input=payload,
        stdin=stdin,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=environment,
    )
    if result.returncode:
        # Never expose resolved application settings from Docker stderr.
        raise command_failure(command, result.returncode, result.stderr)
    return result.stdout or b""


class Compose:
    def __init__(self, path, project=None):
        self.path = Path(path).resolve()
        self.base = [
            "docker",
            "compose",
            "--project-directory",
            str(self.path.parent),
            "-f",
            str(self.path),
        ]
        if project:
            self.base += ["-p", project]

    def call(self, *arguments, **kwargs):
        return run(self.base + list(arguments), **kwargs)

    def config(self, *, environment=None):
        result = json.loads(
            self.call("config", "--format", "json", environment=environment)
        )
        return dollar_literals(result, encode=False)

    def sql(self, query, database=None, username=None):
        if database is None or username is None:
            env = self.config()["services"]["database"].get("environment", {})
            database = database or env.get("POSTGRES_DB", "immich")
            username = username or env.get("POSTGRES_USER", "postgres")
        return (
            self.call(
                "exec",
                "-T",
                "database",
                "psql",
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                username,
                "-d",
                database,
                payload=query.encode(),
                timeout=60,
            )
            .decode()
            .strip()
        )

    def api(self, path, method="GET", body=None, headers=None, file=None, fields=None):
        q = {"path": path, "method": method, "headers": headers or {}}
        if body is not None:
            q["body"] = body
        if file:
            q.update(file=file, fields=fields or {})
        data = self.call(
            "exec",
            "-T",
            "immich-server",
            "node",
            "--input-type=module",
            "-e",
            NODE_HTTP,
            payload=json.dumps(q).encode(),
            timeout=90,
        )
        return json.loads(data)
