from __future__ import annotations

import json

import pytest

from artifact_cache import ArtifactCache, StaleWriter
from conftest import entry_paths, lock_bytes, make_request


def test_builder_exception_cleans_and_allows_immediate_retry(make_cache):
    cache = make_cache()
    item = make_request()

    with pytest.raises(RuntimeError, match="builder"):
        cache.get_or_build(
            item,
            lambda _path: (_ for _ in ()).throw(RuntimeError("builder failed")),
            writer_id="broken",
        )

    assert list((cache.root / "staging").iterdir()) == []
    state = json.loads(lock_bytes(cache, cache.key_for(item)))
    assert state["state"] == "aborted"
    retried = cache.get_or_build(item, lambda _path: b"ok", writer_id="retry")
    assert retried.artifact == b"ok"


def test_staging_and_manifest_faults_abort_without_overwriting_old(tmp_path, clock):
    for stage in ("artifact_staged", "manifest_staged"):
        root = tmp_path / stage
        item = make_request()
        healthy = ArtifactCache(root, clock=clock, lease_seconds=10)
        healthy.get_or_build(item, lambda _path: b"old", writer_id="old")

        def fail(observed, _path, target=stage):
            if observed == target:
                raise OSError(target)

        broken = ArtifactCache(root, clock=clock, lease_seconds=10, fault_hook=fail)
        lease = broken.begin_build(item, writer_id="new")
        with pytest.raises(OSError, match=stage):
            broken.commit(lease, b"new")

        assert not lease.staging_dir.exists()
        assert json.loads(lock_bytes(broken, lease.cache_key))["state"] == "aborted"
        assert ArtifactCache(root, clock=clock).get(item).artifact == b"old"


def test_after_publish_failure_is_a_durable_hit_and_retry_does_not_rebuild(
    tmp_path, clock,
):
    root = tmp_path / "cache"
    item = make_request()

    def fail(stage, _path):
        if stage == "after_publish":
            raise OSError("client lost acknowledgement")

    cache = ArtifactCache(root, clock=clock, lease_seconds=10, fault_hook=fail)
    lease = cache.begin_build(item, writer_id="writer")
    with pytest.raises(OSError, match="acknowledgement"):
        cache.commit(lease, b"published")

    assert json.loads(lock_bytes(cache, lease.cache_key))["state"] == "committed"
    observed = ArtifactCache(root, clock=clock, lease_seconds=10).get(item)
    assert observed is not None and observed.artifact == b"published"

    calls = []
    retry = ArtifactCache(root, clock=clock).get_or_build(
        item, lambda _path: calls.append(True) or b"wrong", writer_id="retry")
    assert retry.artifact == b"published"
    assert calls == []


def test_commit_retry_is_idempotent_but_changed_content_is_stale(make_cache):
    cache = make_cache()
    item = make_request(inputs={"retry": b"source"})
    lease = cache.begin_build(item, writer_id="writer")
    first = cache.commit(lease, b"same")
    before_pointer = (
        cache.root / "entries" / first.cache_key / "current.json").read_bytes()
    before_lock = lock_bytes(cache, first.cache_key)
    repeated = cache.commit(lease, b"same")

    assert repeated.artifact == first.artifact
    assert repeated.manifest == first.manifest
    assert (
        cache.root / "entries" / first.cache_key / "current.json").read_bytes() == before_pointer
    assert lock_bytes(cache, first.cache_key) == before_lock

    with pytest.raises(StaleWriter):
        cache.commit(lease, b"changed")
    assert cache.get(item).artifact == b"same"
    assert lock_bytes(cache, first.cache_key) == before_lock


def test_failure_for_one_key_does_not_affect_another(tmp_path, clock):
    root = tmp_path / "cache"
    healthy_request = make_request(inputs={"healthy": b"source"})
    failing_request = make_request(inputs={"failing": b"source"})
    healthy = ArtifactCache(root, clock=clock)
    healthy.get_or_build(
        healthy_request, lambda _path: b"healthy", writer_id="healthy")

    def fail(stage, _path):
        if stage == "before_publish":
            raise OSError("controlled")

    mixed = ArtifactCache(root, clock=clock, fault_hook=fail)
    lease = mixed.begin_build(failing_request, writer_id="failing")
    with pytest.raises(OSError, match="controlled"):
        mixed.commit(lease, b"bad")

    survivor = ArtifactCache(root, clock=clock).get(healthy_request)
    assert survivor is not None and survivor.artifact == b"healthy"
    assert ArtifactCache(root, clock=clock).get(failing_request) is None
