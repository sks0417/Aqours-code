from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from artifact_cache import BuildInProgress, StaleWriter
from conftest import lock_bytes, make_request


def test_old_generation_cannot_replace_current_writer(make_cache, clock):
    cache = make_cache()
    item = make_request()
    old = cache.begin_build(item, writer_id="old")
    clock.set(old.expires_at)
    current = cache.begin_build(item, writer_id="current")
    before = lock_bytes(cache, current.cache_key)

    with pytest.raises(StaleWriter):
        cache.commit(old, b"late")

    assert lock_bytes(cache, current.cache_key) == before
    assert cache.get(item) is None
    assert current.staging_dir.is_dir()


def test_forged_token_is_rejected_without_filesystem_side_effects(make_cache):
    cache = make_cache()
    item = make_request()
    lease = cache.begin_build(item, writer_id="writer")
    forged = replace(lease, token=f"{lease.cache_key[:8]}-forged")
    before = lock_bytes(cache, lease.cache_key)
    before_staging = sorted(path.name for path in (cache.root / "staging").iterdir())

    with pytest.raises(StaleWriter):
        cache.commit(forged, b"forged")

    assert lock_bytes(cache, lease.cache_key) == before
    assert sorted(path.name for path in (cache.root / "staging").iterdir()) == before_staging
    assert cache.get(item) is None


def test_expiry_boundary_invalidates_even_current_token(make_cache, clock):
    cache = make_cache()
    item = make_request()
    lease = cache.begin_build(item, writer_id="writer")
    before = lock_bytes(cache, lease.cache_key)
    clock.set(lease.expires_at)

    with pytest.raises(StaleWriter):
        cache.commit(lease, b"expired")

    assert lock_bytes(cache, lease.cache_key) == before
    assert lease.staging_dir.is_dir()
    assert cache.get(item) is None


def test_cross_key_lease_substitution_is_rejected(make_cache):
    cache = make_cache()
    first_request = make_request(inputs={"a": b"one"})
    second_request = make_request(inputs={"b": b"two"})
    first = cache.begin_build(first_request, writer_id="first")
    second = cache.begin_build(second_request, writer_id="second")
    substituted = replace(first, cache_key=second.cache_key)
    before_first = lock_bytes(cache, first.cache_key)
    before_second = lock_bytes(cache, second.cache_key)

    with pytest.raises(StaleWriter):
        cache.commit(substituted, b"wrong")

    assert lock_bytes(cache, first.cache_key) == before_first
    assert lock_bytes(cache, second.cache_key) == before_second
    assert first.staging_dir.is_dir() and second.staging_dir.is_dir()


def test_live_writer_blocks_explicit_acquire_and_abort_is_idempotent(make_cache):
    cache = make_cache()
    item = make_request()
    first = cache.begin_build(item, writer_id="first")

    with pytest.raises(BuildInProgress):
        cache.begin_build(item, writer_id="second")
    cache.abort(first)
    after_first_abort = lock_bytes(cache, first.cache_key)
    cache.abort(first)
    assert lock_bytes(cache, first.cache_key) == after_first_abort

    replacement = cache.begin_build(item, writer_id="second")
    assert replacement.generation == first.generation + 1


def test_per_key_build_mutex_allows_true_parallel_progress(make_cache):
    cache = make_cache()
    barrier = threading.Barrier(2)
    errors = []
    results = []

    def run(label):
        try:
            item = make_request(inputs={label: label.encode()})
            results.append(cache.get_or_build(
                item,
                lambda _path: (barrier.wait(timeout=2), label.encode())[1],
                writer_id=label,
            ))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert {entry.artifact for entry in results} == {b"a", b"b"}
