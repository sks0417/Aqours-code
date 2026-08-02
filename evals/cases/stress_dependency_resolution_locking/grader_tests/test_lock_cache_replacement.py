from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import basic_registry, digest, state, version
from dependency_resolver import (
    LockError, UnsatisfiedConstraints, ValidationError, build_application,
)


def strict_lock(core_version="1.2.0", core_digest=None):
    return {
        "app": {"version": "1.0.0", "digest": digest("a")},
        "core": {
            "version": core_version,
            "digest": core_digest or (
                digest("b") if core_version == "1.2.0" else digest("c")),
        },
    }


@pytest.mark.parametrize("mutate,package", [
    (lambda lock: lock["app"].update({"digest": digest("f")}), "app"),
    (lambda lock: lock.pop("core"), "core"),
    (lambda lock: lock.update(
        {"extra": {"version": "1.0.0", "digest": digest("e")}}), "extra"),
])
def test_lock_digest_completeness_and_extras_are_strict(mutate, package):
    app = build_application(basic_registry())
    lock = strict_lock()
    mutate(lock)
    before = state(app)
    with pytest.raises(LockError) as caught:
        app.api.resolve({"app": "1.0.0"}, platform="linux", lock=lock)
    assert caught.value.package == package
    assert state(app) == before


def test_yanked_or_incompatible_lock_pin_is_rejected():
    registry = basic_registry()
    registry["core"]["1.2.0"]["yanked"] = True
    app = build_application(registry)
    with pytest.raises(LockError):
        app.api.resolve(
            {"app": "1.0.0"}, platform="linux", lock=strict_lock())


def test_cache_key_isolated_and_results_are_deep_fresh():
    app = build_application(basic_registry())
    first = app.api.resolve({"app": "1.0.0"}, platform="linux")
    first["packages"][0]["version"] = "forged"
    first["lock"]["core"]["digest"] = digest("f")
    replay = app.api.resolve({"app": "1.0.0"}, platform="linux")
    assert all(row["version"] != "forged" for row in replay["packages"])
    assert replay["lock"]["core"]["digest"] != digest("f")
    app.api.resolve({"app": "1.0.0"}, platform="darwin")
    assert app.api.cache_size() == 2


def test_registry_replacement_is_atomic_and_clears_cache_once():
    app = build_application(basic_registry())
    app.api.resolve({"app": "1.0.0"}, platform="linux")
    before = state(app)
    invalid = basic_registry()
    invalid["core"]["1.2.0"]["digest"] = "bad"
    with pytest.raises(ValidationError):
        app.api.replace_registry(invalid)
    assert state(app) == before

    replacement = basic_registry()
    replacement["core"]["1.11.0"] = version("e")
    old_revision = app.registry.revision
    app.api.replace_registry(replacement)
    assert app.registry.revision == old_revision + 1
    assert app.api.cache_size() == 0
    result = app.api.resolve({"app": "1.0.0"}, platform="linux")
    assert result["lock"]["core"]["version"] == "1.11.0"


def test_failed_resolution_does_not_populate_cache():
    app = build_application(basic_registry())
    with pytest.raises(UnsatisfiedConstraints):
        app.api.resolve({"core": ">99.0.0"}, platform="linux")
    assert app.api.cache_size() == 0
