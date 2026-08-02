from __future__ import annotations

import pytest

from conftest import version
from dependency_resolver import ValidationError, build_application


def feature_registry():
    return {
        "app": {"1.0.0": version(
            "a",
            optional_dependencies={
                "tls": {"crypto": "^1.0.0"},
                "docs": {"markdown": "1.0.0"},
            },
            platform_dependencies={
                "linux": {"epoll": "1.0.0"},
                "win32": {"iocp": "1.0.0"},
            },
        )},
        "crypto": {"1.0.0": version("b")},
        "markdown": {"1.0.0": version("c")},
        "epoll": {"1.0.0": version("d")},
        "iocp": {"1.0.0": version("e")},
    }


def test_only_requested_features_and_current_platform_activate():
    app = build_application(feature_registry())
    result = app.api.resolve(
        {"app": "1.0.0"}, platform="linux", features={"app": ["tls"]})
    assert {row["name"] for row in result["packages"]} == {
        "app", "crypto", "epoll"}


def test_feature_order_is_normalized_but_semantic_changes_are_distinct():
    app = build_application(feature_registry())
    app.api.resolve(
        {"app": "1.0.0"}, platform="linux",
        features={"app": ["docs", "tls"]})
    app.api.resolve(
        {"app": "1.0.0"}, platform="linux",
        features={"app": ["tls", "docs"]})
    assert app.api.cache_size() == 1
    app.api.resolve(
        {"app": "1.0.0"}, platform="linux", features={"app": ["tls"]})
    assert app.api.cache_size() == 2


def test_conflicting_high_candidate_backtracks_to_lower_release():
    registry = {
        "app": {
            "2.0.0": version("a", conflicts={"tool": ">=1.0.0"}),
            "1.0.0": version("b"),
        },
        "tool": {"1.0.0": version("c")},
    }
    result = build_application(registry).api.resolve(
        {"app": ">=1.0.0", "tool": "1.0.0"}, platform="linux")
    assert {row["name"]: row["version"] for row in result["packages"]} == {
        "app": "1.0.0", "tool": "1.0.0"}


def test_unknown_feature_is_validation_error():
    app = build_application(feature_registry())
    with pytest.raises(ValidationError) as caught:
        app.api.resolve(
            {"app": "1.0.0"}, platform="linux",
            features={"app": ["missing"]})
    assert caught.value.field == "features"
