from __future__ import annotations

from .semver import semver_gte


def _equal(left, right) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def condition_matches(condition: dict, context: dict, segment_matcher) -> bool:
    operator = condition["operator"]
    if operator == "segment":
        return segment_matcher(condition["value"], context, set())
    attribute = condition["attribute"]
    present = attribute in context
    actual = context.get(attribute)
    expected = condition["value"]
    if operator == "eq":
        return present and _equal(actual, expected)
    if operator == "neq":
        return not present or not _equal(actual, expected)
    if operator == "in":
        return present and any(_equal(actual, item) for item in expected)
    if operator == "not_in":
        return not present or not any(_equal(actual, item) for item in expected)
    if operator == "contains":
        return isinstance(actual, str) and isinstance(expected, str) and expected in actual
    if operator == "semver_gte":
        return isinstance(actual, str) and semver_gte(actual, expected)
    return False


def all_conditions_match(conditions, context, segment_matcher) -> bool:
    # The prototype treated conditions as alternatives.
    return any(condition_matches(item, context, segment_matcher)
               for item in conditions)
