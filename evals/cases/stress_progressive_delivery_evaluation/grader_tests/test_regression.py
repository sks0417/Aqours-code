from __future__ import annotations

import inspect

import pytest

from conftest import flag
from rollout_engine import ContextError, build_application


@pytest.mark.parametrize("name", [
    "ConfigurationError", "ContextError", "UnknownFlag",
    "RequestConflict", "EvaluationCycle",
])
def test_documented_errors_inherit_base(name):
    import rollout_engine

    assert issubclass(
        getattr(rollout_engine, name), rollout_engine.RolloutEngineError)


def test_context_scalar_rules_and_builtin_protection():
    app = build_application({"checkout": flag()})
    with pytest.raises(ContextError) as caught:
        app.api.evaluate(
            "checkout", {"user_id": "u", "attributes": {"x": [1]}},
            request_id="bad")
    assert caught.value.field == "x"
    with pytest.raises(ContextError) as caught:
        app.api.evaluate(
            "checkout", {"user_id": "u", "attributes": {"user_id": "x"}},
            request_id="bad2")
    assert caught.value.field == "user_id"


def test_facade_delegates_without_storage_access():
    import rollout_engine.api as module

    source = inspect.getsource(module.RolloutAPI)
    for marker in ("._flags", "._segments", "._bindings", "._values"):
        assert marker not in source
