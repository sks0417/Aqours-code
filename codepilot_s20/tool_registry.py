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


@dataclass(frozen=True)
class ToolSpec:
    """One authoritative definition for a callable model tool."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable | None
    safety_policy: str
    background_policy: str
    allowed_roles: frozenset[str]
    runtime_aware: bool = False

    def api_schema(self) -> dict[str, Any]:
        """Return the provider-facing schema without leaking runtime metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
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
