from __future__ import annotations

import pytest

from conftest import version
from dependency_resolver import DependencyCycle, build_application


def test_solver_backtracks_when_highest_choice_breaks_later_constraint():
    registry = {
        "app": {
            "2.0.0": version("a", dependencies={"core": "^2.0.0"}),
            "1.0.0": version("b", dependencies={"core": "^1.0.0"}),
        },
        "tool": {
            "1.0.0": version("c", dependencies={"core": "^1.0.0"}),
        },
        "core": {
            "2.0.0": version("d"),
            "1.5.0": version("e"),
        },
    }
    app = build_application(registry)
    result = app.api.resolve(
        {"app": ">=1.0.0,<3.0.0", "tool": "1.0.0"}, platform="linux")
    assert {row["name"]: row["version"] for row in result["packages"]} == {
        "app": "1.0.0", "tool": "1.0.0", "core": "1.5.0"}


def test_topological_order_and_ready_tie_break_are_deterministic():
    registry = {
        "app": {"1.0.0": version(
            "a", dependencies={"zeta": "1.0.0", "alpha": "1.0.0"})},
        "alpha": {"1.0.0": version("b", dependencies={"base": "1.0.0"})},
        "zeta": {"1.0.0": version("c", dependencies={"base": "1.0.0"})},
        "base": {"1.0.0": version("d")},
    }
    result = build_application(registry).api.resolve(
        {"app": "1.0.0"}, platform="linux")
    assert [row["name"] for row in result["packages"]] == [
        "base", "alpha", "zeta", "app"]


def test_dependency_cycle_reports_closed_path_and_does_not_cache():
    registry = {
        "a": {"1.0.0": version("a", dependencies={"b": "1.0.0"})},
        "b": {"1.0.0": version("b", dependencies={"c": "1.0.0"})},
        "c": {"1.0.0": version("c", dependencies={"a": "1.0.0"})},
    }
    app = build_application(registry)
    with pytest.raises(DependencyCycle) as caught:
        app.api.resolve({"a": "1.0.0"}, platform="linux")
    assert caught.value.path[0] == caught.value.path[-1]
    assert set(caught.value.path[:-1]) == {"a", "b", "c"}
    assert app.api.cache_size() == 0
