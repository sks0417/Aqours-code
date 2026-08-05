from __future__ import annotations

import json

from artifact_cache import ArtifactCache
from conftest import entry_paths, lock_bytes, make_request


def test_live_staging_is_kept_then_expired_staging_is_removed(make_cache, clock):
    cache = make_cache()
    item = make_request()
    lease = cache.begin_build(item, writer_id="active")
    (lease.staging_dir / "partial").write_bytes(b"partial")

    live = cache.recover()
    assert live.kept_active_builds == 1
    assert live.active_cache_keys == (lease.cache_key,)
    assert live.removed_staging_dirs == 0
    assert lease.staging_dir.is_dir()

    clock.set(lease.expires_at)
    expired = cache.recover()
    assert expired.removed_staging_dirs == 1
    assert expired.removed_cache_keys == (lease.cache_key,)
    assert not lease.staging_dir.exists()


def test_committed_entry_and_recovery_report_are_idempotent(make_cache):
    cache = make_cache()
    item = make_request(inputs={"committed": b"source"})
    committed = cache.get_or_build(item, lambda _path: b"artifact", writer_id="one")
    before = cache.get(item)

    first = cache.recover()
    second = cache.recover()

    assert first.kept_entries == 1
    assert second.kept_entries == 1
    assert first.removed_staging_dirs == second.removed_staging_dirs == 0
    assert first.removed_versions == second.removed_versions == 0
    assert cache.get(item) == before
    assert cache.get(item).artifact == committed.artifact


def test_incomplete_current_entry_is_invalidated_without_becoming_a_hit(make_cache):
    cache = make_cache()
    item = make_request(inputs={"incomplete": b"source"})
    key = cache.key_for(item)
    version_root = cache.root / "entries" / key / "versions" / "incomplete"
    version_root.mkdir(parents=True)
    (version_root / "artifact.bin").write_bytes(b"partial")
    pointer = version_root.parents[1] / "current.json"
    pointer.write_text(json.dumps({"version": "incomplete"}), encoding="utf-8")

    report = cache.recover()

    assert report.invalid_entries == 1
    assert report.removed_versions == 1
    assert report.removed_cache_keys == (key,)
    assert not pointer.exists()
    assert cache.get(item) is None


def test_unreachable_version_is_removed_while_current_version_survives(make_cache):
    cache = make_cache()
    item = make_request(inputs={"orphan": b"source"})
    entry = cache.get_or_build(item, lambda _path: b"current", writer_id="one")
    current_root, _artifact, _manifest = entry_paths(cache.root, entry)
    orphan = current_root.parent / "orphan-version"
    orphan.mkdir()
    (orphan / "artifact.bin").write_bytes(b"orphan")

    report = cache.recover()

    assert report.kept_entries == 1
    assert report.removed_versions == 1
    assert current_root.is_dir()
    assert not orphan.exists()
    assert cache.get(item).artifact == b"current"


def test_terminal_lock_records_are_not_reactivated(make_cache):
    cache = make_cache()
    committed_request = make_request(inputs={"done": b"source"})
    committed = cache.begin_build(committed_request, writer_id="done")
    cache.commit(committed, b"done")
    committed_before = lock_bytes(cache, committed.cache_key)

    aborted_request = make_request(inputs={"aborted": b"source"})
    aborted = cache.begin_build(aborted_request, writer_id="aborted")
    cache.abort(aborted)
    aborted_before = lock_bytes(cache, aborted.cache_key)

    cache.recover()

    assert lock_bytes(cache, committed.cache_key) == committed_before
    assert lock_bytes(cache, aborted.cache_key) == aborted_before
    assert json.loads(committed_before)["state"] == "committed"
    assert json.loads(aborted_before)["state"] == "aborted"


def test_recovery_preserves_unrelated_data_and_isolates_keys(make_cache, clock):
    cache = make_cache()
    note = cache.root / "operator.txt"
    note.write_text("keep", encoding="utf-8")
    live = cache.begin_build(make_request(inputs={"live": b"one"}), writer_id="live")
    expired = cache.begin_build(
        make_request(inputs={"expired": b"two"}), writer_id="expired")
    clock.set(expired.expires_at)
    # Give the first key a separately created, still-live generation.
    live_state = json.loads(lock_bytes(cache, live.cache_key))
    live_state["expires_at"] = expired.expires_at + 10
    (cache.root / "locks" / f"{live.cache_key}.json").write_text(
        json.dumps(live_state), encoding="utf-8")

    report = cache.recover()

    assert note.read_text(encoding="utf-8") == "keep"
    assert live.staging_dir.is_dir()
    assert not expired.staging_dir.exists()
    assert report.active_cache_keys == (live.cache_key,)
    assert report.removed_cache_keys == (expired.cache_key,)

