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
# Exported to the JSON Schema only; parse_symbol() remains the validator.
SYMBOL_JSON_PATTERN = (
    r"^[^:\\]+\.py::[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
PATH_DESCRIPTION = (
    "Repository-relative POSIX path: '/' separators, no leading '/', no drive "
    "letter, no empty, '.' or '..' segments, no segment starting or ending with "
    "whitespace, and no control characters."
)
SYMBOL_DESCRIPTION = (
    "Symbol 'path/to/file.py::Qualified.name': a repository-relative path to a "
    ".py file, '::', then Python identifiers joined by '.'."
)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def validate_repo_path(value: str) -> str:
    """Return ``value`` if it is a normalized repository-relative POSIX path."""
    if not value:
        raise ValueError("path must not be empty")
    if "\\" in value:
        raise ValueError(f"path must use '/' separators: {value!r}")
    if value.startswith("/") or _DRIVE_PREFIX.match(value):
        raise ValueError(f"path must be relative to the repository root: {value!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"path must not contain control characters: {value!r}")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(
                f"path must not contain empty, '.' or '..' segments: {value!r}"
            )
        if part != part.strip():
            raise ValueError(
                f"path segments must not start or end with whitespace: {value!r}"
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


def _reject_duplicates(values: list[str], kind: str) -> list[str]:
    """Return ``values`` unless an entry appears more than once."""
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {kind}: {', '.join(duplicates)}")
    return values


def _reject_duplicate_paths(paths: list[str]) -> list[str]:
    return _reject_duplicates(paths, "paths")


def _reject_duplicate_symbols(symbols: list[str]) -> list[str]:
    return _reject_duplicates(symbols, "symbols")


RepoPath = Annotated[
    str,
    AfterValidator(validate_repo_path),
    Field(description=PATH_DESCRIPTION),
]
UniquePaths = Annotated[
    list[RepoPath],
    AfterValidator(_reject_duplicate_paths),
    Field(json_schema_extra={"uniqueItems": True}),
]
Symbol = Annotated[
    str,
    AfterValidator(validate_symbol),
    Field(description=SYMBOL_DESCRIPTION,
          json_schema_extra={"pattern": SYMBOL_JSON_PATTERN}),
]
UniqueSymbols = Annotated[
    list[Symbol],
    AfterValidator(_reject_duplicate_symbols),
    Field(json_schema_extra={"uniqueItems": True}),
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Generator(_Model):
    """Where a graph came from."""

    kind: Literal["manual", "planner"] = Field(
        description="'manual' for a hand-written graph, 'planner' for a generated one.")
    planner_version: str | None = Field(
        default=None, description="Version of the planner that produced the graph.")
    model: str | None = Field(default=None, description="Model used by the planner.")
    revision_mode: Literal["llm", "rule_assisted", "none"] | None = Field(
        default=None,
        description=("How the draft graph was revised: by an LLM ('llm'), by program "
                     "rules plus an LLM ('rule_assisted'), or not at all ('none')."))


class RevisionEntry(_Model):
    """One recorded change made to a graph after generation."""

    action: Literal["merge", "split", "add_edge", "remove_edge", "add_node", "other"] = Field(
        description="Kind of change.")
    nodes: list[str] = Field(description="Ids of the nodes involved.")
    into: str | None = Field(
        default=None, description="Id of the resulting node, for merge and split.")
    reason: str = Field(description="Why the change was made.")


class EditSet(_Model):
    """Files a node may change, plus optional informational symbols."""

    modify: UniquePaths = Field(
        description=("Files this node changes. Each must exist at the base commit or "
                     "be created by an ancestor node."))
    create: UniquePaths = Field(
        description=("New files this node creates. They must not exist at the base "
                     "commit, and each new file is created by exactly one node."))
    symbols: UniqueSymbols = Field(
        default_factory=list,
        description=("Informational only: functions or methods this node changes. "
                     "Each symbol's file must be in this node's modify or create."))

    @model_validator(mode="after")
    def _modify_and_create_disjoint(self) -> "EditSet":
        both = sorted(set(self.modify) & set(self.create))
        if both:
            raise ValueError(
                f"paths listed in both modify and create: {', '.join(both)}")
        return self


class Check(_Model):
    """Commands that verify one node."""

    commands: list[str] = Field(
        description=("Shell commands, run from the repository root, that verify the "
                     "node; at least one must be non-empty."))
    timeout_s: StrictInt = Field(
        default=300, gt=0, description="Timeout in seconds for the check commands.")


class Node(_Model):
    """One sub-task handed to a worker."""

    id: str = Field(pattern=NODE_ID_PATTERN,
                    description="Unique node id: letters, digits, '_' and '-'.")
    title: str = Field(description="Short name of the sub-task.")
    kind: Literal["contract", "implement"] = Field(
        description=("'contract' defines shared interfaces (new states, fields, "
                     "signatures, API formats) that other nodes build on; "
                     "'implement' implements behavior."))
    goal: str = Field(description="Concrete, non-empty goal handed to the worker.")
    edit_set: EditSet = Field(description="Files the node may change.")
    requires: UniqueSymbols = Field(
        default_factory=list,
        description=("Symbols this node uses. Each must exist at the base commit or "
                     "be provided by an ancestor node."))
    provides: UniqueSymbols = Field(
        default_factory=list,
        description="Symbols this node adds or changes for other nodes to use.")
    check: Check = Field(description="How to verify that the node is complete.")
    context_files: UniquePaths = Field(
        default_factory=list,
        description=("Files the worker should read first. Each must exist at the base "
                     "commit or be created by some node."))
    size: Literal["small", "medium", "large"] | None = Field(
        default=None, description="Expected size of the change.")

    @field_validator("goal")
    @classmethod
    def _goal_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("goal must not be empty")
        return value


class Edge(_Model):
    """A dependency or ordering constraint between two nodes."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", description="Id of the upstream node.")
    to: str = Field(description="Id of the downstream node.")
    type: Literal["interface", "full", "order"] = Field(
        description=("Every edge means the downstream node starts only after the "
                     "upstream node has finished and been merged. 'interface': the "
                     "downstream node uses an interface defined by the upstream "
                     "contract node; 'full': it depends on the upstream "
                     "implementation; 'order': only the order matters, for example "
                     "both nodes edit the same file."))
    source: Literal["manual", "llm", "derived"] = Field(
        description="Who added the edge: a person, an LLM, or a program rule.")
    reason: str = Field(description="Why the edge exists.")


class Graph(_Model):
    """A complete task graph for one request."""

    request_id: str = Field(description="Identifier of the request.")
    request: str = Field(description="Original request text.")
    repo: str = Field(description="Name or path of the target repository.")
    base_commit: str = Field(description="Commit the plan is based on.")
    final_checks: list[str] = Field(
        default_factory=list, description="Commands to run after all nodes are merged.")
    generator: Generator = Field(description="Where this graph came from.")
    nodes: list[Node] = Field(min_length=1, description="Sub-tasks; at least one.")
    edges: list[Edge] = Field(
        default_factory=list, description="Dependency and ordering constraints.")
    revision_log: list[RevisionEntry] = Field(
        default_factory=list, description="Changes made to the graph after generation.")


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
