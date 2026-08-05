from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import inspect

import pytest

import artifact_cache
from artifact_cache import (
    ARTIFACT_NAME,
    CURRENT_MANIFEST_VERSION,
    DEFAULT_NAMESPACE_VERSION,
    LEGACY_MANIFEST_VERSION,
    MANIFEST_NAME,
    ArtifactCache,
    BuildInProgress,
    BuildLease,
    BuildRequest,
    CacheEntry,
    CacheError,
    InvalidArtifact,
    InvalidManifest,
    InvalidRequest,
    RecoveryReport,
    StaleWriter,
)
from conftest import make_request


def test_exports_constants_types_and_method_signatures(tmp_path):
    assert set(artifact_cache.__all__) == {
        "ARTIFACT_NAME", "CURRENT_MANIFEST_VERSION", "DEFAULT_NAMESPACE_VERSION",
        "LEGACY_MANIFEST_VERSION", "MANIFEST_NAME", "ArtifactCache",
        "BuildInProgress", "BuildLease", "BuildRequest", "CacheEntry",
        "CacheError", "InvalidArtifact", "InvalidManifest", "InvalidRequest",
        "RecoveryReport", "StaleWriter",
    }
    assert ARTIFACT_NAME == "artifact.bin"
    assert MANIFEST_NAME == "manifest.json"
    assert (CURRENT_MANIFEST_VERSION, LEGACY_MANIFEST_VERSION) == (2, 1)
    assert DEFAULT_NAMESPACE_VERSION == "artifact-cache-v1"
    for error in (
        BuildInProgress, InvalidArtifact, InvalidManifest, InvalidRequest, StaleWriter,
    ):
        assert issubclass(error, CacheError)
    assert isinstance(ArtifactCache(tmp_path / "cache"), ArtifactCache)

    expected = {
        "__init__": ["self", "root", "clock", "lease_seconds", "fault_hook"],
        "key_for": ["self", "request"],
        "get": ["self", "request"],
        "begin_build": ["self", "request", "writer_id"],
        "commit": ["self", "lease", "artifact"],
        "abort": ["self", "lease"],
        "get_or_build": ["self", "request", "builder", "writer_id"],
        "recover": ["self"],
    }
    for name, parameters in expected.items():
        assert list(inspect.signature(getattr(ArtifactCache, name)).parameters) == parameters


def test_public_dataclass_fields_are_stable_and_immutable(make_cache):
    assert [field.name for field in fields(BuildRequest)] == [
        "inputs", "options", "tool_version", "namespace_version",
        "artifact_format", "scratch_dir",
    ]
    assert [field.name for field in fields(BuildLease)] == [
        "cache_key", "writer_id", "generation", "token", "expires_at",
        "staging_dir", "artifact_format",
    ]
    assert [field.name for field in fields(CacheEntry)] == [
        "cache_key", "artifact", "manifest", "cache_hit",
    ]
    assert [field.name for field in fields(RecoveryReport)] == [
        "kept_entries", "kept_active_builds", "removed_staging_dirs",
        "removed_versions", "invalid_entries", "removed_cache_keys",
        "active_cache_keys",
    ]
    request = make_request()
    with pytest.raises(FrozenInstanceError):
        request.tool_version = "changed"


def test_results_are_detached_and_builder_file_forms_remain_supported(make_cache):
    cache = make_cache()
    item = make_request()

    def builder(staging):
        (staging / ARTIFACT_NAME).write_bytes(b"from-file")
        return None

    built = cache.get_or_build(item, builder, writer_id="writer")
    built.manifest["digest"] = "caller mutation"
    reread = cache.get(item)

    assert reread is not None
    assert reread.artifact == b"from-file"
    assert reread.manifest["digest"] != "caller mutation"
    assert reread.cache_hit is True


def test_documented_validation_and_external_artifact_path_errors(
    make_cache, tmp_path,
):
    with pytest.raises(InvalidRequest):
        ArtifactCache(tmp_path / "zero", lease_seconds=0)
    with pytest.raises(InvalidRequest):
        ArtifactCache(tmp_path / "bool", lease_seconds=True)
    broken_clock = ArtifactCache(tmp_path / "clock", clock=lambda: float("nan"))
    with pytest.raises(InvalidRequest):
        broken_clock.begin_build(make_request(), writer_id="writer")
    cache = make_cache()
    with pytest.raises(InvalidRequest):
        cache.begin_build(make_request(), writer_id=" ")
    lease = cache.begin_build(make_request(), writer_id="writer")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(InvalidArtifact):
        cache.commit(lease, outside)

