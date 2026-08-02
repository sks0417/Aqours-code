from __future__ import annotations

import inspect

from conftest import flag


def test_exports_and_signatures_are_stable(make_application):
    import rollout_engine

    expected = {
        "RolloutApplication", "build_api", "build_application",
        "RolloutEngineError", "ConfigurationError", "ContextError",
        "UnknownFlag", "RequestConflict", "EvaluationCycle",
    }
    assert expected <= set(rollout_engine.__all__)
    app = make_application()
    assert str(inspect.signature(app.api.evaluate)) == (
        "(flag_key, context, *, request_id)")
    assert str(inspect.signature(app.api.exposures)) == "()"
    assert str(inspect.signature(app.api.replace_configuration)) == (
        "(flags, segments=None)")


def test_application_exposes_expected_diagnostics():
    from rollout_engine import build_application

    app = build_application({"checkout": flag()})
    assert app.configurations.flag("checkout")["default_variation"] == "off"
    assert app.requests.snapshot() == {}
    assert app.exposure_repository.snapshot() == []
