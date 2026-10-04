"""Synthetic-fixture metadata and first-start readiness checks; never installed."""

import json
import re
import time

from compose_runtime import Compose, UpdaterError


def invariants(compose: Compose, username, database, snapshot=None):
    # Actual app metadata only, never dumped to the conversational log.
    columns = compose.sql(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='asset_face';",
        database,
        username,
    ).splitlines()
    if "personId" in columns:
        link_join = 'f."personId"=p.id'
    elif "personGroupId" in columns:
        link_join = 'f."personGroupId"=p."personGroupId"'
    else:
        raise UpdaterError(
            "Unknown face relationship schema; comparison cannot be certified."
        )
    statement = f"""
SELECT json_build_object(
 'users',(SELECT count(*) FROM public."user"),
 'assets',(SELECT count(*) FROM public.asset),
 'albums',(SELECT count(*) FROM public.album),
 'album_links',(SELECT count(*) FROM public.album_asset),
 'album_identity',(SELECT md5(coalesce(string_agg(CAST(id AS text)||"albumName",'' ORDER BY id),'')) FROM public.album),
 'album_relationships',(SELECT md5(coalesce(string_agg(\"albumId\"::text||\"assetId\"::text,'' ORDER BY \"albumId\",\"assetId\"),'')) FROM public.album_asset),
 'people',(SELECT count(*) FROM public.person),
 'faces',(SELECT count(*) FROM public.asset_face),
 'person_links',(SELECT count(*) FROM public.asset_face f JOIN public.person p ON {link_join} JOIN public.asset a ON a.id=f."assetId" AND a."ownerId"=p."ownerId"),
 'asset_identity',(SELECT md5(coalesce(string_agg(id::text||"ownerId"::text||"originalPath"||"originalFileName",'' ORDER BY id),'')) FROM public.asset),
 'person_metadata',(SELECT md5(coalesce(string_agg("ownerId"::text||name||coalesce("birthDate"::text,''),'' ORDER BY "ownerId",name,"birthDate"),'')) FROM public.person),
 'face_links',(SELECT md5(coalesce(string_agg(f.id::text||f."assetId"::text||p."ownerId"::text||p.name,'' ORDER BY f.id,p."ownerId"),'')) FROM public.asset_face f JOIN public.person p ON {link_join} JOIN public.asset a ON a.id=f."assetId" AND a."ownerId"=p."ownerId")
)::text;
"""
    if snapshot:
        if not re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+", snapshot):
            raise UpdaterError("Invalid exported snapshot token.")
        statement = (
            "BEGIN ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '"
            + snapshot
            + "';\n"
            + statement
            + "\nCOMMIT;"
        )
    return json.loads(compose.sql(statement, database, username))


def wait_fixture_workers(clone, timeout=300):
    """Stock HTTP health can precede microservices/geodata initialization."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Logs are consumed locally only. Missing/changed stock bootstrap evidence
        # is an unsupported readiness path, never permission to overlap heavy startup.
        output = clone.call("logs", "--no-color", "immich-server", timeout=30)
        if b"Immich Microservices is running" in output:
            return True
        time.sleep(1)
    raise UpdaterError(
        "Stock background worker initialization did not complete before ML startup."
    )
