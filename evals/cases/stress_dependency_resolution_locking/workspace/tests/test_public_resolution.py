from __future__ import annotations

from conftest import basic_registry
from dependency_resolver import build_application


def test_selects_highest_version_satisfying_all_constraints():
    app = build_application(basic_registry())
    result = app.api.resolve({"app": "1.0.0"}, platform="linux")
    versions = {row["name"]: row["version"] for row in result["packages"]}
    assert versions == {"app": "1.0.0", "core": "1.10.0"}


def test_installation_order_places_dependencies_first():
    app = build_application(basic_registry())
    result = app.api.resolve({"app": "1.0.0"}, platform="linux")
    assert [row["name"] for row in result["packages"]] == ["core", "app"]


def test_equivalent_request_order_reuses_cache():
    app = build_application(basic_registry())
    first = app.api.resolve({"app": "1.0.0"}, platform="linux", features={})
    second = app.api.resolve({"app": "1.0.0"}, platform="linux")
    assert first == second
    assert app.api.cache_size() == 1
