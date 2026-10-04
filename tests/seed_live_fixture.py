"""Explicit synthetic Immich fixture. Refuses unmarked/externally-networked stacks."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compose_runtime import Compose, UpdaterError


def seed(source):
    source = Path(source).resolve()
    if not (source.parent / ".synthetic-fixture").is_file():
        raise UpdaterError(
            "Synthetic fixture marker required; never seed owner production."
        )
    stack = Compose(source)
    config = stack.config()
    if any(not item.get("internal") for item in config.get("networks", {}).values()):
        raise UpdaterError("Fixture must be internal-network only.")
    credentials = json.loads((source.parent / "fixture-credentials.json").read_text())
    login = stack.api(
        "/api/auth/login",
        method="POST",
        body={"email": credentials["email"], "password": credentials["password"]},
    )
    if login["status"] != 201:
        raise UpdaterError("Synthetic fixture login failed.")
    headers = {"Authorization": "Bearer " + login["data"]["accessToken"]}
    image = source.parent / "library" / "rehearsal-fixture.jpg"
    Image.new("RGB", (96, 64), color=(30, 110, 190)).save(image, "JPEG")
    uploaded = stack.api(
        "/api/assets",
        method="POST",
        headers=headers,
        file="/data/rehearsal-fixture.jpg",
        fields={
            "deviceAssetId": "synthetic-rehearsal-asset",
            "deviceId": "synthetic-rehearsal",
            "fileCreatedAt": "2026-01-01T12:00:00Z",
            "fileModifiedAt": "2026-01-01T12:00:00Z",
        },
    )
    if uploaded["status"] not in (200, 201) or not (uploaded.get("data") or {}).get(
        "id"
    ):
        raise UpdaterError("Actual multipart photo upload failed.")
    asset = uploaded["data"]["id"]
    album = stack.api(
        "/api/albums",
        method="POST",
        headers=headers,
        body={"albumName": "Synthetic retained album", "assetIds": [asset]},
    )
    if album["status"] != 201:
        raise UpdaterError("Actual synthetic album creation failed.")
    owner = credentials["userId"]
    # Deliberately labelled test metadata on the real 3.1 schema, not a fabricated API reply.
    stack.sql(f"""
INSERT INTO public.person(\"ownerId\", name, \"birthDate\")
VALUES ('{owner}', 'Synthetic retained person', '1990-01-01');
INSERT INTO public.asset_face(\"assetId\",\"personId\",\"imageWidth\",\"imageHeight\",\"boundingBoxX1\",\"boundingBoxY1\",\"boundingBoxX2\",\"boundingBoxY2\")
SELECT '{asset}',id,96,64,1,1,20,20 FROM public.person WHERE name='Synthetic retained person';
""")
    original = stack.api("/api/assets/" + asset + "/original", headers=headers)
    expected = hashlib.sha256(image.read_bytes()).hexdigest()
    if original["status"] != 200 or original["sha256"] != expected:
        raise UpdaterError("Actual uploaded-original read did not preserve JPEG bytes.")
    print(
        json.dumps(
            {
                "synthetic_fixture": True,
                "actual_photo_upload": True,
                "actual_original_read": True,
                "actual_album_created": True,
                "face_metadata_seeded": True,
                "image_sha256": expected,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-compose", required=True)
    args = parser.parse_args()
    seed(args.source_compose)
