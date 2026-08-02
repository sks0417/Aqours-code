from __future__ import annotations

import hashlib

import pytest

from conftest import flag
from rollout_engine import ConfigurationError, build_application
from rollout_engine.bucketing import bucket, choose_rollout


def test_bucket_is_exact_sha256_contract_and_boundary_is_half_open():
    expected = int(hashlib.sha256(
        b"checkout:alice:salt").hexdigest()[:8], 16) % 10_000
    assert bucket("checkout", "alice", "salt") == expected

    rollout = [
        {"variation": "a", "weight": 1},
        {"variation": "b", "weight": 9999},
    ]
    import rollout_engine.bucketing as module
    original = module.bucket
    try:
        module.bucket = lambda *_args: 0
        assert choose_rollout("f", "u", "s", rollout) == "a"
        module.bucket = lambda *_args: 1
        assert choose_rollout("f", "u", "s", rollout) == "b"
    finally:
        module.bucket = original


def test_prerequisite_compares_variation_name_not_truthiness():
    variations = {"off": False, "control": "non-empty", "treatment": "also-non-empty"}
    flags = {
        "gate": flag(default="control", variations=variations),
        "child": flag(
            default="on",
            prerequisites=[{"flag": "gate", "variation": "treatment"}]),
    }
    app = build_application(flags)
    result = app.api.evaluate("child", {"user_id": "u"}, request_id="prereq")
    assert (result["variation"], result["reason"]) == ("off", "prerequisite")
    assert [item["flag_key"] for item in app.api.exposures()] == ["gate", "child"]


def test_shared_prerequisite_is_exposed_only_once_per_request():
    flags = {
        "root": flag(default="on"),
        "left": flag(default="on", prerequisites=[{"flag": "root", "variation": "on"}]),
        "top": flag(default="on", prerequisites=[
            {"flag": "root", "variation": "on"},
            {"flag": "left", "variation": "on"},
        ]),
    }
    app = build_application(flags)
    app.api.evaluate("top", {"user_id": "u"}, request_id="diamond")
    assert [item["flag_key"] for item in app.api.exposures()] == [
        "root", "left", "top"]


@pytest.mark.parametrize("flags", [
    {
        "a": flag(prerequisites=[{"flag": "b", "variation": "on"}]),
        "b": flag(prerequisites=[{"flag": "a", "variation": "on"}]),
    },
    {"a": flag(prerequisites=[{"flag": "a", "variation": "on"}])},
])
def test_prerequisite_cycles_are_configuration_errors(flags):
    with pytest.raises(ConfigurationError) as caught:
        build_application(flags)
    assert caught.value.field == "prerequisites"
