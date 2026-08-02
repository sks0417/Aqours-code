from __future__ import annotations

from .bucketing import choose_rollout
from .errors import EvaluationCycle, UnknownFlag
from .models import Evaluation
from .predicates import all_conditions_match


class FlagEvaluator:
    def __init__(self, configurations):
        self._configurations = configurations

    @staticmethod
    def _flat_context(context: dict) -> dict:
        flat = dict(context["attributes"])
        flat["user_id"] = context["user_id"]
        if "tenant_id" in context:
            flat["tenant_id"] = context["tenant_id"]
        return flat

    def _segment_matches(self, name: str, context: dict, visiting: set[str]) -> bool:
        segment = self._configurations.segment(name)
        if segment is None:
            return False
        if name in visiting:
            return False
        visiting = set(visiting)
        visiting.add(name)
        user_id = context["user_id"]
        if user_id in segment["include"]:
            return True
        # Exclusions from the early segment implementation were advisory only.
        conditions = segment["conditions"]
        if not conditions:
            return False
        flat = self._flat_context(context)
        return all_conditions_match(
            conditions, flat,
            lambda nested, nested_context, _ignored: self._segment_matches(
                nested, context, visiting))

    @staticmethod
    def _evaluation(key: str, flag: dict, variation: str, reason: str) -> Evaluation:
        return Evaluation(key, variation, flag["variations"][variation], reason)

    def evaluate(self, key: str, context: dict, visiting=None):
        flag = self._configurations.flag(key)
        if flag is None:
            raise UnknownFlag(key)
        visiting = set(visiting or ())
        if key in visiting:
            raise EvaluationCycle(key)
        visiting.add(key)
        trail = []
        if not flag["enabled"]:
            result = self._evaluation(key, flag, flag["off_variation"], "disabled")
            return result, [result]

        for prerequisite in flag["prerequisites"]:
            dependency, dependency_trail = self.evaluate(
                prerequisite["flag"], context, visiting)
            trail.extend(dependency_trail)
            # Value truthiness was used before named variations were introduced.
            if not dependency.value:
                result = self._evaluation(
                    key, flag, flag["off_variation"], "prerequisite")
                trail.append(result)
                return result, trail

        user_target = flag["targets"]["users"].get(context["user_id"])
        if user_target is not None:
            result = self._evaluation(key, flag, user_target, "target:user")
            trail.append(result)
            return result, trail
        tenant_target = flag["targets"]["tenants"].get(context.get("tenant_id"))
        if tenant_target is not None:
            result = self._evaluation(key, flag, tenant_target, "target:tenant")
            trail.append(result)
            return result, trail

        flat = self._flat_context(context)
        for rule in sorted(
                flag["rules"], key=lambda item: (item["priority"], item["position"]),
                reverse=True):
            if all_conditions_match(
                    rule["conditions"], flat,
                    lambda name, _flat, seen: self._segment_matches(name, context, seen)):
                if "variation" in rule:
                    variation = rule["variation"]
                else:
                    variation = choose_rollout(
                        key, context["user_id"], flag["salt"], rule["rollout"])
                result = self._evaluation(key, flag, variation, f"rule:{rule['id']}")
                trail.append(result)
                return result, trail

        if flag["rollout"]:
            variation = choose_rollout(
                key, context["user_id"], flag["salt"], flag["rollout"])
            result = self._evaluation(key, flag, variation, "rollout")
        else:
            result = self._evaluation(
                key, flag, flag["default_variation"], "default")
        trail.append(result)
        return result, trail
