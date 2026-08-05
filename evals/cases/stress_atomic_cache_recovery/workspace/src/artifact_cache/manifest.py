from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import (
    CURRENT_MANIFEST_VERSION,
    LEGACY_MANIFEST_VERSION,
    InvalidManifest,
)


def artifact_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(*, cache_key: str, artifact: bytes, artifact_format: str,
                   generation: int, writer_id: str, lease_token: str,
                   created_at: float) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_MANIFEST_VERSION,
        "cache_key": cache_key,
        "digest": artifact_digest(artifact),
        "size": len(artifact),
        "artifact_format": artifact_format,
        "generation": generation,
        "writer_id": writer_id,
        "lease_token": lease_token,
        "created_at": created_at,
    }


def write_manifest(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def read_manifest(path: Path, artifact_path: Path, *, expected_key: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidManifest("manifest is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("cache_key") != expected_key:
        raise InvalidManifest("manifest cache key does not match")
    version = raw.get("schema_version")
    if version == LEGACY_MANIFEST_VERSION:
        digest = raw.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise InvalidManifest("legacy manifest digest is invalid")
        try:
            size = artifact_path.stat().st_size
        except OSError as exc:
            raise InvalidManifest("legacy artifact is missing") from exc
        return {
            "schema_version": LEGACY_MANIFEST_VERSION,
            "cache_key": expected_key,
            "digest": digest,
            "size": size,
            "artifact_format": "binary",
            "generation": 0,
        }
    if version != CURRENT_MANIFEST_VERSION:
        raise InvalidManifest("unsupported manifest schema")
    required = {
        "digest": str,
        "size": int,
        "artifact_format": str,
        "generation": int,
        "writer_id": str,
        "lease_token": str,
        "created_at": (int, float),
    }
    for field, expected_type in required.items():
        if isinstance(raw.get(field), bool) or not isinstance(raw.get(field), expected_type):
            raise InvalidManifest(f"manifest field {field} is invalid")
    return dict(raw)

