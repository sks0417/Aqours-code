from __future__ import annotations

import inspect

import pytest

from conftest import basic_registry, version
from dependency_resolver import UnknownPackage, ValidationError, build_application


@pytest.mark.parametrize("name", [
    "ValidationError", "UnknownPackage", "UnsatisfiedConstraints",
    "DependencyCycle", "PackageConflict", "LockError",
])
def test_documented_errors_inherit_base(name):
    import dependency_resolver

    assert issubclass(
        getattr(dependency_resolver, name),
        dependency_resolver.DependencyResolverError)


def test_unknown_top_level_and_registry_dependency_errors_are_typed():
    app = build_application(basic_registry())
    with pytest.raises(UnknownPackage) as caught:
        app.api.resolve({"missing": "1.0.0"}, platform="linux")
    assert caught.value.package == "missing"

    invalid = basic_registry()
    invalid["app"]["1.0.0"]["dependencies"]["missing"] = "1.0.0"
    with pytest.raises(ValidationError) as caught:
        build_application(invalid)
    assert caught.value.field == "dependencies"


def test_build_metadata_normalizes_and_duplicate_normalized_versions_fail():
    registry = {
        "a": {"1.0.0+build.1": version("a")},
    }
    result = build_application(registry).api.resolve(
        {"a": "1.0.0"}, platform="linux")
    assert result["packages"][0]["version"] == "1.0.0"
    duplicate = {
        "a": {
            "1.0.0+one": version("a"),
            "1.0.0+two": version("b"),
        }
    }
    with pytest.raises(ValidationError) as caught:
        build_application(duplicate)
    assert caught.value.field == "version"


def test_facade_delegates_without_storage_access():
    import dependency_resolver.api as module

    source = inspect.getsource(module.ResolverAPI)
    for marker in ("._registry", "._values", "._revision"):
        assert marker not in source
