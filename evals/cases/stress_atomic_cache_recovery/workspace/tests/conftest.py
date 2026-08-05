from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self._value = float(value)
        self._guard = threading.Lock()

    def __call__(self) -> float:
        with self._guard:
            return self._value

    def set(self, value: float) -> None:
        with self._guard:
            self._value = float(value)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cache(tmp_path, clock):
    from artifact_cache import ArtifactCache

    return ArtifactCache(tmp_path / "cache", clock=clock, lease_seconds=10)


def request(*, source: bytes = b"source", options=None, tool_version: str = "py-1",
            namespace_version: str = "cache-v2", artifact_format: str = "binary",
            scratch_dir=None):
    from artifact_cache import BuildRequest

    return BuildRequest(
        inputs={"src/main.txt": source},
        options={} if options is None else options,
        tool_version=tool_version,
        namespace_version=namespace_version,
        artifact_format=artifact_format,
        scratch_dir=scratch_dir,
    )


def install_legacy_entry(cache_root: Path, cache_key: str, artifact: bytes,
                         *, digest: str | None = None) -> Path:
    version = "legacy-v1"
    version_root = cache_root / "entries" / cache_key / "versions" / version
    version_root.mkdir(parents=True, exist_ok=True)
    (version_root / "artifact.bin").write_bytes(artifact)
    (version_root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "cache_key": cache_key,
        "sha256": digest or hashlib.sha256(artifact).hexdigest(),
    }), encoding="utf-8")
    (version_root.parents[1] / "current.json").write_text(
        json.dumps({"version": version}), encoding="utf-8")
    return version_root

