from __future__ import annotations

from conftest import flag
from rollout_engine import build_application
from rollout_engine.semver import semver_gte


def condition(attribute, operator, value):
    return {"attribute": attribute, "operator": operator, "value": value}


def test_all_conditions_and_ascending_priority_are_required():
    flags = {"checkout": flag(rules=[
        {"id": "later", "priority": 20,
         "conditions": [condition("plan", "eq", "pro")], "variation": "beta"},
        {"id": "first", "priority": 10,
         "conditions": [
             condition("plan", "eq", "pro"),
             condition("country", "eq", "US"),
         ], "variation": "on"},
    ])}
    app = build_application(flags)
    complete = app.api.evaluate(
        "checkout",
        {"user_id": "a", "attributes": {"plan": "pro", "country": "US"}},
        request_id="complete")
    partial = app.api.evaluate(
        "checkout",
        {"user_id": "b", "attributes": {"plan": "pro", "country": "CA"}},
        request_id="partial")
    assert (complete["variation"], complete["reason"]) == ("on", "rule:first")
    assert (partial["variation"], partial["reason"]) == ("beta", "rule:later")


def test_semver_uses_numeric_and_prerelease_precedence():
    assert semver_gte("10.0.0", "2.9.9")
    assert semver_gte("2.0.0", "2.0.0-rc.9")
    assert semver_gte("1.0.0-beta.11", "1.0.0-beta.2")
    assert not semver_gte("1.0.0-alpha", "1.0.0")
    assert semver_gte("1.2.3+build.1", "1.2.3+other")
    assert not semver_gte("01.2.3", "1.0.0")


def test_segment_exclusion_wins_and_nested_conditions_are_all_required():
    segments = {
        "paid-us": {
            "conditions": [
                condition("plan", "eq", "pro"),
                condition("country", "eq", "US"),
            ],
        },
        "launch": {
            "include": ["vip", "blocked"],
            "exclude": ["blocked"],
            "conditions": [
                condition("ignored", "segment", "paid-us"),
                condition("age", "in", [21, 22]),
            ],
        },
    }
    flags = {"checkout": flag(rules=[{
        "id": "segment", "priority": 1,
        "conditions": [condition("ignored", "segment", "launch")],
        "variation": "on",
    }])}
    app = build_application(flags, segments)
    assert app.api.evaluate(
        "checkout", {"user_id": "vip"}, request_id="vip")["variation"] == "on"
    assert app.api.evaluate(
        "checkout", {"user_id": "blocked"}, request_id="blocked")["variation"] == "off"
    assert app.api.evaluate("checkout", {
        "user_id": "nested",
        "attributes": {"plan": "pro", "country": "US", "age": 21},
    }, request_id="nested")["variation"] == "on"
    assert app.api.evaluate("checkout", {
        "user_id": "partial",
        "attributes": {"plan": "pro", "country": "CA", "age": 21},
    }, request_id="partial")["variation"] == "off"
