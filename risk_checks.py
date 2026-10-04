"""Bounded, read-only upstream evidence checks. Never execute upstream text."""

import json
import re
from datetime import datetime

import requests
from semantic_version import NpmSpec, Version

API = "https://api.github.com/repos/immich-app/immich"
SERIOUS = {"high", "critical"}
PACKAGES = {
    "ghcr.io/immich-app/immich-server",
    "immich-server",
    "@immich/server",
    "immich",
    "immich-app/immich",
    "",
}


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def stamp(raw):
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Evidence timestamp must have a timezone.")
    return value


def version(raw):
    if not isinstance(raw, str) or not re.fullmatch(
        r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", raw
    ):
        raise ValueError("Expected an exact stable semantic version.")
    return Version(raw[1:] if raw.startswith("v") else raw)


def spec(raw):
    """Accept documented GitHub/NPM range spelling, retaining raw evidence."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Missing affected range.")
    if any(not part.strip() for part in raw.split(",")):
        raise ValueError("Empty range clause.")
    # GitHub adds spaces after comparison operators and uses comma AND clauses.
    # This is grammar handling, not repair of invalid version identifiers.
    expression = re.sub(r"([<>]=?|[~^=])\s+(?=v?\d)", r"\1", raw.strip())
    expression = re.sub(r"(?<![\w.])v(?=\d)", "", expression)
    return NpmSpec(" ".join(expression.replace(",", " ").split()))


class GitHub:
    def __init__(self):
        self.cache = {}

    def get(self, path, **params):
        url = path if path.startswith("https://api.github.com/") else API + path
        key = (url, tuple(sorted(params.items())))
        if key not in self.cache:
            response = requests.get(
                url,
                params=params,
                timeout=30,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            self.cache[key] = response.json(), bool(response.links.get("next"))
        return self.cache[key]

    def pages(self, path, limit=10, **params):
        rows = []
        for page in range(1, limit + 1):
            data, more = self.get(path, per_page=100, page=page, **params)
            if not isinstance(data, list):
                raise ValueError("Expected a paginated upstream list.")
            rows.extend(data)
            if not more:
                return rows
        raise RuntimeError(
            "Upstream list exceeds page limit; refusing an incomplete scan."
        )

    def contains(self, ref, tag):
        if not re.fullmatch(r"[0-9a-f]{7,40}", ref):
            raise ValueError("Invalid advisory commit identifier.")
        version(tag)
        data, _ = self.get("/compare/" + ref + "..." + tag)
        state = data.get("status")
        if state not in {"ahead", "behind", "identical", "diverged"}:
            raise ValueError("Unknown commit ancestry result.")
        return state in {"ahead", "identical"}


def advisory_risks(github, advisories, tag):
    target = version(tag)
    blockers, fixes = [], set()
    for advisory in advisories:
        if advisory.get("state") != "published" or advisory.get("withdrawn_at"):
            continue
        severity = advisory.get("severity")
        serious = severity in SERIOUS or severity not in {
            "low",
            "medium",
            "high",
            "critical",
        }
        vulnerabilities = advisory.get("vulnerabilities")
        if not isinstance(vulnerabilities, list) or not vulnerabilities:
            if serious:
                blockers.append(advisory["ghsa_id"] + ": unresolved package/range")
            continue
        for affected in vulnerabilities:
            package = affected.get("package") or {}
            if package.get("name") not in PACKAGES:
                if serious:
                    blockers.append(
                        advisory["ghsa_id"] + ": unresolved package applicability"
                    )
                continue
            raw = affected.get("vulnerable_version_range")
            patched = affected.get("patched_versions")
            patched_match, vulnerable_match = False, None
            try:
                introduced = re.fullmatch(r">\s*main@([0-9a-f]{7,40})", raw or "")
                repaired = re.fullmatch(r">\s*main@([0-9a-f]{7,40})", patched or "")
                if introduced and repaired:
                    patched_match = github.contains(repaired.group(1), tag)
                    vulnerable_match = not patched_match and github.contains(
                        introduced.group(1), tag
                    )
                else:
                    vulnerable_match = spec(raw).match(target)
                    if patched and patched not in {"None", "none"}:
                        patched_match = spec(patched).match(target)
            except ValueError:
                # A known exact published patch can resolve an ambiguous old range.
                try:
                    patched_match = version(patched) == target
                except (ValueError, TypeError):
                    pass
            status = (
                "patched"
                if patched_match
                else "affected"
                if vulnerable_match
                else "unresolved"
                if vulnerable_match is None
                else "outside_range"
            )
            log(
                "advisory",
                tag=tag,
                id=advisory["ghsa_id"],
                severity=severity,
                status=status,
                package=package.get("name"),
                affected_range=raw,
                patched_versions=patched,
                source=advisory.get("html_url"),
            )
            if serious and status in {"affected", "unresolved"}:
                blockers.append(advisory["ghsa_id"] + ": " + status)
            if serious and patched_match:
                fixes.add(advisory["ghsa_id"])
    return blockers, fixes
