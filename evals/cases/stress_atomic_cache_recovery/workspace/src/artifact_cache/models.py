from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


CURRENT_MANIFEST_VERSION = 2
LEGACY_MANIFEST_VERSION = 1
DEFAULT_NAMESPACE_VERSION = "artifact-cache-v1"
ARTIFACT_NAME = "artifact.bin"
MANIFEST_NAME = "manifest.json"


class CacheError(Exception):
    """Base class for public artifact-cache errors."""


class InvalidRequest(CacheError):
    pass


class BuildInProgress(CacheError):
    pass


class StaleWriter(CacheError):
    pass


class InvalidArtifact(CacheError):
    pass


class InvalidManifest(CacheError):
    pass


@dataclass(frozen=True)
class BuildRequest:
    inputs: Mapping[str, bytes | str]
    options: Mapping[str, Any] = field(default_factory=dict)
    tool_version: str = "1"
    namespace_version: str = DEFAULT_NAMESPACE_VERSION
    artifact_format: str = "binary"
    scratch_dir: str | Path | None = None


@dataclass(frozen=True)
class BuildLease:
    cache_key: str
    writer_id: str
    generation: int
    token: str
    expires_at: float
    staging_dir: Path
    artifact_format: str


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    artifact: bytes
    manifest: dict[str, Any]
    cache_hit: bool


@dataclass(frozen=True)
class RecoveryReport:
    kept_entries: int = 0
    kept_active_builds: int = 0
    removed_staging_dirs: int = 0
    removed_versions: int = 0
    invalid_entries: int = 0
    removed_cache_keys: tuple[str, ...] = field(default_factory=tuple)
    active_cache_keys: tuple[str, ...] = field(default_factory=tuple)

