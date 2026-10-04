"""Task graph data model.

A task graph describes a plan only: it never contains run state. Paths are
repository-relative POSIX paths and symbols use ``path::Qualified.name``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

NODE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
SYMBOL_SEPARATOR = "::"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def validate_repo_path(value: str) -> str:
    """Return ``value`` if it is a normalized repository-relative POSIX path."""
    if not value:
        raise ValueError("path must not be empty")
    if "\\" in value:
        raise ValueError(f"path must use '/' separators: {value!r}")
    if value.startswith("/") or _DRIVE_PREFIX.match(value):
        raise ValueError(f"path must be relative to the repository root: {value!r}")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(
                f"path must not contain empty, '.' or '..' segments: {value!r}"
            )
    return value


@dataclass(frozen=True)
class SymbolRef:
    """A parsed ``path::Qualified.name`` symbol."""

    path: str
    qualname: str

    @property
    def parts(self) -> tuple[str, ...]:
        """Return the dotted name components."""
        return tuple(self.qualname.split("."))

    def __str__(self) -> str:
        return f"{self.path}{SYMBOL_SEPARATOR}{self.qualname}"


def parse_symbol(text: str) -> SymbolRef:
    """Parse a symbol string, raising ``ValueError`` when it is malformed."""
    if not isinstance(text, str):
        raise ValueError("symbol must be a string")
    pieces = text.split(SYMBOL_SEPARATOR)
    if len(pieces) != 2:
        raise ValueError(
            f"symbol must contain exactly one '{SYMBOL_SEPARATOR}': {text!r}"
        )
    path, qualname = pieces
    try:
        validate_repo_path(path)
    except ValueError as exc:
        raise ValueError(f"invalid file path in symbol {text!r}: {exc}") from None
    if not path.endswith(".py"):
        # v0 limitation: only Python files are indexed, so only they can
        # define symbols.
        raise ValueError(f"symbol file must be a .py file: {text!r}")
    names = qualname.split(".")
    if not all(name.isidentifier() for name in names):
        raise ValueError(
            f"qualified name must be dot-separated Python identifiers: {text!r}"
        )
    return SymbolRef(path=path, qualname=qualname)


def validate_symbol(text: str) -> str:
    """Return ``text`` if it is a well-formed symbol string."""
    parse_symbol(text)
    return text


def _reject_duplicates(paths: list[str]) -> list[str]:
    """Return ``paths`` unless a path appears more than once."""
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise ValueError(f"duplicate paths: {', '.join(duplicates)}")
    return paths


RepoPath = Annotated[str, AfterValidator(validate_repo_path)]
UniquePaths = Annotated[list[RepoPath], AfterValidator(_reject_duplicates)]
Symbol = Annotated[str, AfterValidator(validate_symbol)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Generator(_Model):
    """Where a graph came from."""

    kind: Literal["manual", "planner"]
    planner_version: str | None = None
    model: str | None = None
    revision_mode: Literal["llm", "rule_assisted", "none"] | None = None


class RevisionEntry(_Model):
    """One recorded change made to a graph after generation."""

    action: Literal["merge", "split", "add_edge", "remove_edge", "add_node", "other"]
    nodes: list[str]
    into: str | None = None
    reason: str


class EditSet(_Model):
    """Files a node may change, plus optional informational symbols."""

    modify: UniquePaths
    create: UniquePaths
    symbols: list[Symbol] = Field(default_factory=list)

    @model_validator(mode="after")
    def _modify_and_create_disjoint(self) -> "EditSet":
        both = sorted(set(self.modify) & set(self.create))
        if both:
            raise ValueError(
                f"paths listed in both modify and create: {', '.join(both)}")
        return self


class Check(_Model):
    """Commands that verify one node."""

    commands: list[str]
    timeout_s: StrictInt = Field(default=300, gt=0)


class Node(_Model):
    """One sub-task handed to a worker."""

    id: str = Field(pattern=NODE_ID_PATTERN)
    title: str
    kind: Literal["contract", "implement"]
    goal: str
    edit_set: EditSet
    requires: list[Symbol] = Field(default_factory=list)
    provides: list[Symbol] = Field(default_factory=list)
    check: Check
    context_files: UniquePaths = Field(default_factory=list)
    size: Literal["small", "medium", "large"] | None = None

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal must not be empty")
        return value


class Edge(_Model):
    """A dependency or ordering constraint between two nodes."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    type: Literal["interface", "full", "order"]
    source: Literal["manual", "llm", "derived"]
    reason: str


class Graph(_Model):
    """A complete task graph for one request."""

    request_id: str
    request: str
    repo: str
    base_commit: str
    final_checks: list[str] = Field(default_factory=list)
    generator: Generator
    nodes: list[Node] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)
    revision_log: list[RevisionEntry] = Field(default_factory=list)


def graph_to_json(graph: Graph) -> str:
    """Serialize a graph to its canonical JSON text."""
    data = graph.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_graph(path: str | Path) -> Graph:
    """Load and validate a graph from a JSON file."""
    return Graph.model_validate_json(Path(path).read_text(encoding="utf-8"))


def dump_graph(graph: Graph, path: str | Path) -> None:
    """Write a graph to a JSON file."""
    Path(path).write_text(graph_to_json(graph), encoding="utf-8", newline="\n")


def graph_json_schema() -> dict:
    """Return the JSON Schema of :class:`Graph`."""
    return Graph.model_json_schema(by_alias=True)


DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "taskgraph.schema.json"
EXAMPLE_GRAPH_PATH = Path(__file__).resolve().parent / "examples" / "toy_graph.json"


def export_json_schema(path: str | Path = DEFAULT_SCHEMA_PATH) -> Path:
    """Write the Graph JSON Schema to ``path`` and return the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(graph_json_schema(), indent=2, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target
