from __future__ import annotations

from conftest import install_legacy_entry, request


def test_corrupted_artifact_is_a_cache_miss(cache):
    built = cache.get_or_build(request(), lambda _path: b"trusted", writer_id="one")
    version = built.manifest["generation"]
    token = built.manifest["lease_token"]
    artifact = (
        cache.root / "entries" / built.cache_key / "versions"
        / f"g{version}-{token}" / "artifact.bin"
    )
    artifact.write_bytes(b"tampered")

    assert cache.get(request()) is None


def test_valid_legacy_manifest_is_read_and_completed_safely(cache):
    item = request(source=b"legacy-source")
    key = cache.key_for(item)
    install_legacy_entry(cache.root, key, b"legacy-artifact")

    entry = cache.get(item)

    assert entry is not None
    assert entry.artifact == b"legacy-artifact"
    assert entry.manifest["schema_version"] == 1
    assert entry.manifest["size"] == len(b"legacy-artifact")
    assert entry.manifest["artifact_format"] == "binary"

