from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .manifest import artifact_digest, build_manifest, read_manifest, write_manifest
from .models import (
    ARTIFACT_NAME,
    MANIFEST_NAME,
    BuildLease,
    CacheEntry,
    InvalidArtifact,
    InvalidManifest,
)


class CacheStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.entries = self.root / "entries"
        self.staging = self.root / "staging"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def create_staging(self, lease: BuildLease) -> None:
        lease.staging_dir.mkdir(parents=True, exist_ok=False)
        marker = {
            "cache_key": lease.cache_key,
            "writer_id": lease.writer_id,
            "generation": lease.generation,
            "token": lease.token,
            "expires_at": lease.expires_at,
            "artifact_format": lease.artifact_format,
        }
        (lease.staging_dir / "lease.json").write_text(
            json.dumps(marker, sort_keys=True), encoding="utf-8")

    def cleanup_staging(self, lease: BuildLease) -> None:
        if lease.staging_dir.is_dir():
            shutil.rmtree(lease.staging_dir)

    def _entry_root(self, cache_key: str) -> Path:
        return self.entries / cache_key

    @staticmethod
    def _safe_version(value: object) -> str | None:
        if not isinstance(value, str) or not value or value in {".", ".."}:
            return None
        if "/" in value or "\\" in value or Path(value).name != value:
            return None
        return value

    def read(self, cache_key: str) -> CacheEntry | None:
        entry_root = self._entry_root(cache_key)
        pointer = entry_root / "current.json"
        try:
            current = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        version = self._safe_version(
            current.get("version") if isinstance(current, dict) else None)
        if version is None:
            return None
        version_root = entry_root / "versions" / version
        artifact_path = version_root / ARTIFACT_NAME
        manifest_path = version_root / MANIFEST_NAME
        try:
            artifact = artifact_path.read_bytes()
            manifest = read_manifest(
                manifest_path, artifact_path, expected_key=cache_key)
        except (OSError, InvalidManifest):
            return None
        return CacheEntry(
            cache_key=cache_key,
            artifact=bytes(artifact),
            manifest=dict(manifest),
            cache_hit=True,
        )

    def _artifact_bytes(self, lease: BuildLease, artifact: object) -> bytes:
        if artifact is None:
            path = lease.staging_dir / ARTIFACT_NAME
            try:
                return path.read_bytes()
            except OSError as exc:
                raise InvalidArtifact("builder did not produce artifact.bin") from exc
        if isinstance(artifact, (bytes, bytearray, memoryview)):
            return bytes(artifact)
        if isinstance(artifact, (str, Path)):
            candidate = Path(artifact)
            if not candidate.is_absolute():
                candidate = lease.staging_dir / candidate
            try:
                candidate.resolve().relative_to(lease.staging_dir.resolve())
            except ValueError as exc:
                raise InvalidArtifact("artifact path escapes staging directory") from exc
            try:
                return candidate.read_bytes()
            except OSError as exc:
                raise InvalidArtifact("artifact path is unreadable") from exc
        raise InvalidArtifact("artifact must be bytes, a staging path, or None")

    @staticmethod
    def _fault(hook: Callable[[str, Path], None] | None,
               stage: str, path: Path) -> None:
        if hook is not None:
            hook(stage, path)

    def publish(self, lease: BuildLease, artifact: object, *, created_at: float,
                fault_hook: Callable[[str, Path], None] | None,
                validate: Callable[[], None]) -> CacheEntry:
        data = self._artifact_bytes(lease, artifact)
        staged_artifact = lease.staging_dir / ARTIFACT_NAME
        staged_artifact.write_bytes(data)
        self._fault(fault_hook, "artifact_staged", staged_artifact)

        manifest = build_manifest(
            cache_key=lease.cache_key,
            artifact=data,
            artifact_format=lease.artifact_format,
            generation=lease.generation,
            writer_id=lease.writer_id,
            lease_token=lease.token,
            created_at=created_at,
        )
        validate()
        version_name = f"g{lease.generation}-{lease.token}"
        entry_root = self._entry_root(lease.cache_key)
        version_root = entry_root / "versions" / version_name
        version_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_artifact, version_root / ARTIFACT_NAME)

        entry_root.mkdir(parents=True, exist_ok=True)
        pointer = entry_root / "current.json"
        pointer.write_text(json.dumps({"version": version_name}), encoding="utf-8")
        self._fault(fault_hook, "before_publish", pointer)

        write_manifest(version_root / MANIFEST_NAME, manifest)
        self._fault(fault_hook, "manifest_staged", version_root / MANIFEST_NAME)
        self._fault(fault_hook, "after_publish", pointer)
        self.cleanup_staging(lease)
        return CacheEntry(
            cache_key=lease.cache_key,
            artifact=data,
            manifest=dict(manifest),
            cache_hit=False,
        )

    def committed_for_lease(self, lease: BuildLease, artifact: object) -> CacheEntry | None:
        entry = self.read(lease.cache_key)
        if entry is None:
            return None
        data = self._artifact_bytes(lease, artifact)
        if (
            entry.manifest.get("lease_token") == lease.token
            and entry.manifest.get("generation") == lease.generation
            and entry.manifest.get("digest") == artifact_digest(data)
        ):
            return CacheEntry(
                cache_key=entry.cache_key,
                artifact=entry.artifact,
                manifest=dict(entry.manifest),
                cache_hit=False,
            )
        return None

