from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


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

    def advance(self, amount: float) -> None:
        with self._guard:
            self._value += float(amount)


def make_request(*, inputs=None, options=None, tool_version="tool-1",
                 namespace_version="namespace-1", artifact_format="binary",
                 scratch_dir=None):
    from artifact_cache import BuildRequest

    return BuildRequest(
        inputs={"src/input.txt": b"input"} if inputs is None else inputs,
        options={} if options is None else options,
        tool_version=tool_version,
        namespace_version=namespace_version,
        artifact_format=artifact_format,
        scratch_dir=scratch_dir,
    )


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_cache(tmp_path, clock):
    from artifact_cache import ArtifactCache

    def factory(*, name="cache", fault_hook=None, lease_seconds=10, use_clock=None):
        return ArtifactCache(
            tmp_path / name,
            clock=clock if use_clock is None else use_clock,
            lease_seconds=lease_seconds,
            fault_hook=fault_hook,
        )

    return factory


def entry_paths(root: Path, entry) -> tuple[Path, Path, Path]:
    version = f"g{entry.manifest['generation']}-{entry.manifest['lease_token']}"
    version_root = root / "entries" / entry.cache_key / "versions" / version
    return (
        version_root,
        version_root / "artifact.bin",
        version_root / "manifest.json",
    )


def install_version(root: Path, cache_key: str, artifact: bytes, manifest: dict,
                    *, version="manual-version", make_current=True) -> Path:
    version_root = root / "entries" / cache_key / "versions" / version
    version_root.mkdir(parents=True, exist_ok=True)
    (version_root / "artifact.bin").write_bytes(artifact)
    (version_root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8")
    if make_current:
        pointer = version_root.parents[1] / "current.json"
        pointer.write_text(json.dumps({"version": version}), encoding="utf-8")
    return version_root


def legacy_manifest(cache_key: str, artifact: bytes, **updates) -> dict:
    value = {
        "schema_version": 1,
        "cache_key": cache_key,
        "sha256": hashlib.sha256(artifact).hexdigest(),
    }
    value.update(updates)
    return value


def lock_bytes(cache, cache_key: str) -> bytes:
    return (cache.root / "locks" / f"{cache_key}.json").read_bytes()
