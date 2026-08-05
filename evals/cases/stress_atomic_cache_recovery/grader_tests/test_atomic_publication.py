from __future__ import annotations

import json
import threading

import pytest

from artifact_cache import ArtifactCache
from conftest import entry_paths, make_request


def test_each_prepublication_fault_keeps_previous_complete_entry(tmp_path, clock):
    for stage in ("artifact_staged", "manifest_staged", "before_publish"):
        root = tmp_path / f"cache-{stage}"
        healthy = ArtifactCache(root, clock=clock, lease_seconds=10)
        item = make_request()
        healthy.get_or_build(item, lambda _path: b"old-complete", writer_id="old")

        def fail(observed, _path, target=stage):
            if observed == target:
                raise OSError(f"fault at {target}")

        failing = ArtifactCache(root, clock=clock, lease_seconds=10, fault_hook=fail)
        lease = failing.begin_build(item, writer_id="new")
        with pytest.raises(OSError, match="fault"):
            failing.commit(lease, b"new-content")

        visible = ArtifactCache(root, clock=clock, lease_seconds=10).get(item)
        assert visible is not None
        assert visible.artifact == b"old-complete"
        assert visible.manifest["digest"] != ""


def test_reader_observes_old_version_while_new_publication_is_blocked(tmp_path, clock):
    root = tmp_path / "cache"
    item = make_request()
    initial = ArtifactCache(root, clock=clock, lease_seconds=10)
    initial.get_or_build(item, lambda _path: b"old", writer_id="old")
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def hook(stage, _path):
        if stage == "before_publish":
            entered.set()
            assert release.wait(2)

    writer = ArtifactCache(root, clock=clock, lease_seconds=10, fault_hook=hook)
    lease = writer.begin_build(item, writer_id="new")

    def commit():
        try:
            writer.commit(lease, b"new")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=commit)
    thread.start()
    assert entered.wait(2)
    during = ArtifactCache(root, clock=clock, lease_seconds=10).get(item)
    release.set()
    thread.join(3)

    assert not thread.is_alive()
    assert errors == []
    assert during is not None and during.artifact == b"old"
    assert ArtifactCache(root, clock=clock, lease_seconds=10).get(item).artifact == b"new"


def test_current_pointer_replace_failure_does_not_destroy_old_entry(
    tmp_path, clock, monkeypatch,
):
    import artifact_cache.store as store_module

    root = tmp_path / "cache"
    item = make_request()
    cache = ArtifactCache(root, clock=clock, lease_seconds=10)
    cache.get_or_build(item, lambda _path: b"old", writer_id="old")
    lease = cache.begin_build(item, writer_id="new")
    real_replace = store_module.os.replace

    def controlled_replace(source, destination):
        if str(destination).endswith("current.json"):
            raise OSError("pointer replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", controlled_replace)
    with pytest.raises(OSError, match="pointer replace failed"):
        cache.commit(lease, b"new")

    entry = cache.get(item)
    assert entry is not None and entry.artifact == b"old"


def test_visible_pointer_always_names_a_complete_matching_version(make_cache):
    cache = make_cache()
    item = make_request()
    result = cache.get_or_build(item, lambda _path: b"complete", writer_id="writer")
    pointer_path = cache.root / "entries" / result.cache_key / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    version_root, artifact_path, manifest_path = entry_paths(cache.root, result)

    assert pointer == {"version": version_root.name}
    assert artifact_path.read_bytes() == b"complete"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["cache_key"] == result.cache_key
