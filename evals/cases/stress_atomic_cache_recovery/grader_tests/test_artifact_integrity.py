from __future__ import annotations

import json

from conftest import entry_paths, make_request


def test_digest_mismatch_is_never_a_hit(make_cache):
    cache = make_cache()
    item = make_request()
    entry = cache.get_or_build(item, lambda _path: b"original", writer_id="writer")
    _root, artifact, _manifest = entry_paths(cache.root, entry)
    artifact.write_bytes(b"different")

    assert cache.get(item) is None


def test_truncated_artifact_is_rejected_by_size_and_digest(make_cache):
    cache = make_cache()
    item = make_request(inputs={"truncated": b"source"})
    entry = cache.get_or_build(item, lambda _path: b"0123456789", writer_id="writer")
    _root, artifact, _manifest = entry_paths(cache.root, entry)
    artifact.write_bytes(b"0123")

    assert cache.get(item) is None


def test_manifest_digest_or_size_replacement_is_rejected(make_cache):
    cache = make_cache()
    item = make_request(inputs={"manifest": b"source"})
    entry = cache.get_or_build(item, lambda _path: b"artifact", writer_id="writer")
    _root, _artifact, manifest_path = entry_paths(cache.root, entry)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["digest"] = "0" * 64
    manifest["size"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert cache.get(item) is None


def test_malformed_pointer_and_cache_key_mismatch_are_safe_misses(make_cache):
    cache = make_cache()
    item = make_request(inputs={"pointer": b"source"})
    entry = cache.get_or_build(item, lambda _path: b"artifact", writer_id="writer")
    pointer = cache.root / "entries" / entry.cache_key / "current.json"
    pointer.write_text(json.dumps({"version": "../escape"}), encoding="utf-8")
    assert cache.get(item) is None

    second = make_request(inputs={"key-mismatch": b"source"})
    built = cache.get_or_build(second, lambda _path: b"second", writer_id="writer")
    _root, _artifact, manifest_path = entry_paths(cache.root, built)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cache_key"] = entry.cache_key
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert cache.get(second) is None


def test_corruption_of_one_key_does_not_damage_another(make_cache):
    cache = make_cache()
    first_request = make_request(inputs={"first": b"one"})
    second_request = make_request(inputs={"second": b"two"})
    first = cache.get_or_build(first_request, lambda _path: b"one", writer_id="one")
    second = cache.get_or_build(second_request, lambda _path: b"two", writer_id="two")
    _root, first_artifact, _manifest = entry_paths(cache.root, first)
    first_artifact.write_bytes(b"corrupt")

    assert cache.get(first_request) is None
    healthy = cache.get(second_request)
    assert healthy is not None and healthy.artifact == b"two"
    assert healthy.cache_key == second.cache_key
