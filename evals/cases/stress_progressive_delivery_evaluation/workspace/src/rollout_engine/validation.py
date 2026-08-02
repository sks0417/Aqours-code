from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .errors import ConfigurationError, ContextError


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_OPERATORS = {"eq", "neq", "in", "not_in", "contains", "semver_gte", "segment"}


def _text(value, field: str, error=ConfigurationError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string", field=field)
    return value.strip()


def _json_value(value, field: str):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("value must be JSON-compatible", field=field) from exc
    return value


def normalize_request_id(value) -> str:
    result = _text(value, "request_id", ContextError)
    if len(result) > 128 or not _REQUEST_ID.fullmatch(result):
        raise ContextError("invalid request ID", field="request_id")
    return result


def normalize_context(value) -> dict:
    if not isinstance(value, Mapping):
        raise ContextError("context must be a mapping", field="context")
    user_id = _text(value.get("user_id"), "user_id", ContextError)
    result = {"user_id": user_id}
    if value.get("tenant_id") is not None:
        result["tenant_id"] = _text(value.get("tenant_id"), "tenant_id", ContextError)
    attributes = value.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise ContextError("attributes must be a mapping", field="attributes")
    normalized = {}
    for raw_name, raw_value in attributes.items():
        name = _text(raw_name, "attribute", ContextError)
        if name in {"user_id", "tenant_id"}:
            raise ContextError("built-in attribute cannot be overridden", field=name)
        if isinstance(raw_value, (list, dict, tuple, set)):
            raise ContextError("attribute must be a JSON scalar", field=name)
        try:
            json.dumps(raw_value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ContextError("attribute must be a JSON scalar", field=name) from exc
        normalized[name] = raw_value
    result["attributes"] = dict(sorted(normalized.items()))
    return result


def _normalize_condition(raw, field: str) -> dict:
    if not isinstance(raw, Mapping):
        raise ConfigurationError("condition must be a mapping", field=field)
    attribute = _text(raw.get("attribute"), f"{field}.attribute")
    operator = _text(raw.get("operator"), f"{field}.operator")
    if operator not in _OPERATORS:
        raise ConfigurationError("unknown condition operator", field=f"{field}.operator")
    value = raw.get("value")
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value:
            raise ConfigurationError("membership value must be a list", field=f"{field}.value")
        for item in value:
            if isinstance(item, (list, dict, tuple, set)):
                raise ConfigurationError("membership values must be scalars", field=f"{field}.value")
    elif operator in {"contains", "semver_gte", "segment"}:
        value = _text(value, f"{field}.value")
    elif isinstance(value, (list, dict, tuple, set)):
        raise ConfigurationError("condition value must be scalar", field=f"{field}.value")
    return {"attribute": attribute, "operator": operator, "value": value}


def _normalize_rollout(raw, variations: dict, field: str) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ConfigurationError("rollout must be a non-empty list", field=field)
    result, total = [], 0
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ConfigurationError("rollout item must be a mapping", field=field)
        variation = _text(item.get("variation"), f"{field}[{index}].variation")
        weight = item.get("weight")
        if variation not in variations:
            raise ConfigurationError("unknown rollout variation", field=f"{field}[{index}].variation")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise ConfigurationError("weight must be positive integer", field=f"{field}[{index}].weight")
        total += weight
        result.append({"variation": variation, "weight": weight})
    if total != 10_000:
        raise ConfigurationError("rollout weights must total 10000", field=field)
    return result


def _normalize_segments(raw) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigurationError("segments must be a mapping", field="segments")
    result = {}
    for raw_name, value in raw.items():
        name = _text(raw_name, "segment")
        if not isinstance(value, Mapping):
            raise ConfigurationError("segment must be a mapping", field=f"segments.{name}")
        include = [_text(item, f"segments.{name}.include") for item in value.get("include", [])]
        exclude = [_text(item, f"segments.{name}.exclude") for item in value.get("exclude", [])]
        conditions_raw = value.get("conditions", [])
        if not isinstance(conditions_raw, list):
            raise ConfigurationError("conditions must be a list", field=f"segments.{name}.conditions")
        conditions = [
            _normalize_condition(item, f"segments.{name}.conditions[{index}]")
            for index, item in enumerate(conditions_raw)
        ]
        result[name] = {
            "include": list(dict.fromkeys(include)),
            "exclude": list(dict.fromkeys(exclude)),
            "conditions": conditions,
        }
    return dict(sorted(result.items()))


def normalize_configuration(flags, segments=None):
    if not isinstance(flags, Mapping) or not flags:
        raise ConfigurationError("flags must be a non-empty mapping", field="flags")
    normalized_segments = _normalize_segments(segments)
    result = {}
    for raw_key, raw in flags.items():
        key = _text(raw_key, "flag")
        if not isinstance(raw, Mapping):
            raise ConfigurationError("flag must be a mapping", field=f"flags.{key}")
        if not isinstance(raw.get("enabled"), bool):
            raise ConfigurationError("enabled must be boolean", field=f"flags.{key}.enabled")
        raw_variations = raw.get("variations")
        if not isinstance(raw_variations, Mapping) or not raw_variations:
            raise ConfigurationError("variations must be non-empty", field=f"flags.{key}.variations")
        variations = {}
        for raw_name, value in raw_variations.items():
            name = _text(raw_name, f"flags.{key}.variation")
            variations[name] = _json_value(value, f"flags.{key}.variations.{name}")
        off = _text(raw.get("off_variation"), f"flags.{key}.off_variation")
        default = _text(raw.get("default_variation"), f"flags.{key}.default_variation")
        if off not in variations or default not in variations:
            raise ConfigurationError("unknown default variation", field=f"flags.{key}")
        targets = raw.get("targets", {})
        if not isinstance(targets, Mapping):
            raise ConfigurationError("targets must be a mapping", field=f"flags.{key}.targets")
        normalized_targets = {"users": {}, "tenants": {}}
        for kind in ("users", "tenants"):
            values = targets.get(kind, {})
            if not isinstance(values, Mapping):
                raise ConfigurationError("targets must be mappings", field=f"flags.{key}.targets.{kind}")
            for raw_id, raw_variation in values.items():
                target_id = _text(raw_id, f"flags.{key}.targets.{kind}")
                variation = _text(raw_variation, f"flags.{key}.targets.{kind}.{target_id}")
                if variation not in variations:
                    raise ConfigurationError("unknown target variation", field=f"flags.{key}.targets.{kind}")
                normalized_targets[kind][target_id] = variation
        prerequisites = []
        for index, item in enumerate(raw.get("prerequisites", [])):
            if not isinstance(item, Mapping):
                raise ConfigurationError("prerequisite must be a mapping", field=f"flags.{key}.prerequisites")
            prerequisites.append({
                "flag": _text(item.get("flag"), f"flags.{key}.prerequisites[{index}].flag"),
                "variation": _text(item.get("variation"), f"flags.{key}.prerequisites[{index}].variation"),
            })
        rules_raw = raw.get("rules", [])
        if not isinstance(rules_raw, list):
            raise ConfigurationError("rules must be a list", field=f"flags.{key}.rules")
        rules, rule_ids = [], set()
        for index, item in enumerate(rules_raw):
            if not isinstance(item, Mapping):
                raise ConfigurationError("rule must be a mapping", field=f"flags.{key}.rules")
            rule_id = _text(item.get("id"), f"flags.{key}.rules[{index}].id")
            if rule_id in rule_ids:
                raise ConfigurationError("duplicate rule ID", field=f"flags.{key}.rules")
            rule_ids.add(rule_id)
            priority = item.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ConfigurationError("priority must be integer", field=f"flags.{key}.rules[{index}].priority")
            conditions_raw = item.get("conditions")
            if not isinstance(conditions_raw, list) or not conditions_raw:
                raise ConfigurationError("conditions must be non-empty", field=f"flags.{key}.rules[{index}].conditions")
            has_variation = "variation" in item
            has_rollout = "rollout" in item
            if has_variation == has_rollout:
                raise ConfigurationError("rule needs variation xor rollout", field=f"flags.{key}.rules[{index}]")
            rule = {
                "id": rule_id,
                "priority": priority,
                "position": index,
                "conditions": [
                    _normalize_condition(condition, f"flags.{key}.rules[{index}].conditions[{offset}]")
                    for offset, condition in enumerate(conditions_raw)
                ],
            }
            if has_variation:
                variation = _text(item.get("variation"), f"flags.{key}.rules[{index}].variation")
                if variation not in variations:
                    raise ConfigurationError("unknown rule variation", field=f"flags.{key}.rules[{index}].variation")
                rule["variation"] = variation
            else:
                rule["rollout"] = _normalize_rollout(
                    item.get("rollout"), variations, f"flags.{key}.rules[{index}].rollout")
            rules.append(rule)
        rollout = None
        if "rollout" in raw:
            rollout = _normalize_rollout(raw.get("rollout"), variations, f"flags.{key}.rollout")
        result[key] = {
            "enabled": raw["enabled"], "off_variation": off,
            "default_variation": default, "salt": _text(raw.get("salt"), f"flags.{key}.salt"),
            "variations": variations, "targets": normalized_targets,
            "prerequisites": prerequisites, "rules": rules, "rollout": rollout,
        }

    for key, flag in result.items():
        for item in flag["prerequisites"]:
            dependency = result.get(item["flag"])
            if dependency is None or item["variation"] not in dependency["variations"]:
                raise ConfigurationError("unknown prerequisite", field=f"flags.{key}.prerequisites")
    for segment in normalized_segments.values():
        for condition in segment["conditions"]:
            if condition["operator"] == "segment" and condition["value"] not in normalized_segments:
                raise ConfigurationError("unknown segment", field="segments")
    for flag in result.values():
        for rule in flag["rules"]:
            for condition in rule["conditions"]:
                if condition["operator"] == "segment" and condition["value"] not in normalized_segments:
                    raise ConfigurationError("unknown segment", field="rules")
    # Graph validation was postponed in the legacy loader.
    return dict(sorted(result.items())), normalized_segments
