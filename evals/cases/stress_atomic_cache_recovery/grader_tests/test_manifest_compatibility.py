from __future__ import annotations

import json

from conftest import entry_paths, install_version, legacy_manifest, make_request


def test_valid_v1_manifest_derives_only_safe_missing_fields(make_cache):
    cache = make_cache()
    item = make_request(inputs={"legacy": b"source"})
    key = cache.key_for(item)
    artifact = b"legacy-artifact"
    install_version(cache.root, key, artifact, legacy_manifest(key, artifact))

    entry = cache.get(item)

    assert entry is not None
    assert entry.artifact == artifact
    assert entry.manifest == {
        "schema_version": 1,
        "cache_key": key,
        "digest": legacy_manifest(key, artifact)["sha256"],
        "size": len(artifact),
        "artifact_format": "binary",
        "generation": 0,
    }


def test_v1_digest_is_mandatory_and_verified(make_cache):
    cache = make_cache()
    item = make_request(inputs={"legacy-corrupt": b"source"})
    key = cache.key_for(item)
    artifact = b"legacy-artifact"
    install_version(
        cache.root, key, artifact,
        legacy_manifest(key, artifact, sha256="0" * 64),
    )

    assert cache.get(item) is None


def test_unknown_or_ambiguous_legacy_manifests_are_rejected(make_cache):
    cache = make_cache()
    cases = [
        {"schema_version": 99, "cache_key": "placeholder"},
        {"schema_version": 1, "cache_key": "placeholder"},
        {"schema_version": True, "cache_key": "placeholder", "sha256": "0" * 64},
    ]
    for index, value in enumerate(cases):
        item = make_request(inputs={f"bad-{index}": b"source"})
        key = cache.key_for(item)
        value["cache_key"] = key
        install_version(
            cache.root, key, b"artifact", value, version=f"bad-{index}")
        assert cache.get(item) is None


def test_new_publications_always_write_complete_schema_v2(make_cache):
    cache = make_cache()
    item = make_request(artifact_format="zip")
    entry = cache.get_or_build(item, lambda _path: b"new", writer_id="writer")
    _root, _artifact, manifest_path = entry_paths(cache.root, entry)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == 2
    assert raw["artifact_format"] == "zip"
    assert set(raw) == {
        "schema_version", "cache_key", "digest", "size", "artifact_format",
        "generation", "writer_id", "lease_token", "created_at",
    }
    assert cache.get(item) is not None

