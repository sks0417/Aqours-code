from __future__ import annotations

import pytest

from artifact_cache import ArtifactCache, StaleWriter
from conftest import request


def test_replaced_generation_cannot_publish(cache, clock):
    item = request()
    old = cache.begin_build(item, writer_id="old")
    clock.set(old.expires_at)
    current = cache.begin_build(item, writer_id="current")
    lock_path = cache.root / "locks" / f"{current.cache_key}.json"
    before = lock_path.read_bytes()

    with pytest.raises(StaleWriter):
        cache.commit(old, b"late")

    assert cache.get(item) is None
    assert lock_path.read_bytes() == before
    assert current.staging_dir.is_dir()


def test_prepublication_failure_preserves_old_valid_entry(tmp_path, clock):
    root = tmp_path / "cache"
    healthy = ArtifactCache(root, clock=clock, lease_seconds=10)
    item = request()
    healthy.get_or_build(item, lambda _path: b"old", writer_id="first")

    def fail(stage, _path):
        if stage == "before_publish":
            raise OSError("controlled publication failure")

    failing = ArtifactCache(
        root, clock=clock, lease_seconds=10, fault_hook=fail)
    lease = failing.begin_build(item, writer_id="replacement")
    with pytest.raises(OSError, match="controlled"):
        failing.commit(lease, b"new")

    entry = ArtifactCache(root, clock=clock, lease_seconds=10).get(item)
    assert entry is not None
    assert entry.artifact == b"old"


def test_builder_failure_releases_key_and_removes_private_staging(cache):
    item = request()

    def broken(_path):
        raise RuntimeError("builder failed")

    with pytest.raises(RuntimeError, match="builder failed"):
        cache.get_or_build(item, broken, writer_id="broken")

    assert list((cache.root / "staging").iterdir()) == []
    retry = cache.get_or_build(item, lambda _path: b"recovered", writer_id="retry")
    assert retry.artifact == b"recovered"
