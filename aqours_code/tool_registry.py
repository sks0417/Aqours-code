"""Canonical tool metadata and lookup helpers.

The registry is deliberately independent from the concrete tool modules.  That
keeps it safe to import from lead, role-agent, teammate, and background paths
without introducing another bootstrap cycle.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from inspect import Parameter, signature
from types import MappingProxyType
from typing import Any


KNOWN_SAFETY_POLICIES = frozenset({
    "standard",
    "command_guard",
    "workspace_write",
    "destructive_confirmation",
    "workspace_integration",
})
KNOWN_BACKGROUND_POLICIES = frozenset({
    "foreground",
    "slow_or_explicit",
})


def _deep_freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({
            key: _deep_freeze(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value):
    if isinstance(value, (dict, MappingProxyType)):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [_deep_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolSpec:
    """One authoritative definition for a callable model tool."""

    name: str
    description: str
    schema: Any
    handler: Callable | None
    safety_policy: str
    background_policy: str
    allowed_roles: frozenset[str]
    runtime_aware: bool = False

    def __post_init__(self):
        if self.safety_policy not in KNOWN_SAFETY_POLICIES:
            raise ValueError(
                f"unknown safety policy for {self.name}: {self.safety_policy}"
            )
        if self.background_policy not in KNOWN_BACKGROUND_POLICIES:
            raise ValueError(
                "unknown background policy for "
                f"{self.name}: {self.background_policy}"
            )
        object.__setattr__(self, "schema", _deep_freeze(self.schema))
        object.__setattr__(self, "allowed_roles", frozenset(self.allowed_roles))

    def api_schema(self) -> dict[str, Any]:
        """Return the provider-facing schema without leaking runtime metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _deep_thaw(self.schema),
        }

    def bind_handler(
        self,
        runtime=None,
        **handler_kwargs,
    ) -> Callable | None:
        if self.handler is None:
            return None
        parameters = signature(self.handler).parameters
        accepts_extras = any(
            item.kind == Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        kwargs = {
            key: value for key, value in handler_kwargs.items()
            if accepts_extras or key in parameters
        }
        if self.runtime_aware and runtime is not None:
            kwargs["runtime"] = runtime
        return partial(self.handler, **kwargs) if kwargs else self.handler


class ToolRegistry:
    """Immutable-name registry used by every agent surface."""

    def __init__(self, specs: Iterable[ToolSpec]):
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValueError(f"duplicate tool registration: {spec.name}")
            by_name[spec.name] = spec
        self._specs = MappingProxyType(by_name)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def __len__(self) -> int:
        return len(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def names_for_role(self, role: str) -> tuple[str, ...]:
        return tuple(
            name for name, spec in self._specs.items()
            if role in spec.allowed_roles
        )

    def schemas_for_role(self, role: str) -> list[dict[str, Any]]:
        return [
            spec.api_schema() for spec in self._specs.values()
            if role in spec.allowed_roles
        ]

    def schemas_for_names(
        self,
        names: Iterable[str],
        *,
        role: str,
    ) -> list[dict[str, Any]]:
        requested = set(names)
        return [
            spec.api_schema() for spec in self._specs.values()
            if spec.name in requested and role in spec.allowed_roles
        ]

    def handlers_for_role(self, role: str, runtime=None) -> dict[str, Callable]:
        return self.handlers_for_names(
            self.names_for_role(role), role=role, runtime=runtime,
        )

    def handlers_for_names(
        self,
        names: Iterable[str],
        *,
        role: str,
        runtime=None,
        **handler_kwargs,
    ) -> dict[str, Callable]:
        handlers: dict[str, Callable] = {}
        for name in names:
            spec = self.get(name)
            if role not in spec.allowed_roles:
                continue
            handler = spec.bind_handler(runtime, **handler_kwargs)
            if handler is not None:
                handlers[name] = handler
        return handlers


def _policy_allowed_tools(policy: dict | None) -> set[str] | None:
    if not isinstance(policy, dict) or "allowed_tools" not in policy:
        return None
    return {str(name) for name in policy.get("allowed_tools", ())}


def delegated_policy_for_role(
    parent_policy: dict | None,
    role: str,
) -> dict | None:
    """Return an explicitly configured delegated policy for one role.

    Merely having a parent Runtime never grants a delegated role more tools.
    Expansion past the parent's ``allowed_tools`` requires both an explicit
    role policy and ``allow_parent_permission_expansion=true``.
    """
    if not isinstance(parent_policy, dict):
        return None
    configured = parent_policy.get("delegated_tool_policy")
    if not isinstance(configured, dict):
        return None
    value = configured.get(role)
    return value if isinstance(value, dict) else None


def effective_tool_names(
    registry: ToolRegistry,
    requested_names: Iterable[str],
    *,
    role: str,
    parent_policy: dict | None = None,
    environment_policy: dict | None = None,
    delegated_policy: dict | None = None,
) -> tuple[str, ...]:
    """Apply every permission boundary without implicit delegation expansion."""
    requested = {str(name) for name in requested_names}
    effective = {
        name for name in registry.names_for_role(role)
        if name in requested
    }
    parent_allowed = _policy_allowed_tools(parent_policy)
    delegated_allowed = _policy_allowed_tools(delegated_policy)
    explicitly_expands = bool(
        isinstance(delegated_policy, dict)
        and delegated_policy.get("allow_parent_permission_expansion") is True
    )
    if parent_allowed is not None and not explicitly_expands:
        effective.intersection_update(parent_allowed)
    if delegated_allowed is not None:
        effective.intersection_update(delegated_allowed)
    environment_allowed = _policy_allowed_tools(environment_policy)
    if environment_allowed is not None:
        effective.intersection_update(environment_allowed)
    return tuple(
        name for name in registry.names_for_role(role)
        if name in effective
    )
