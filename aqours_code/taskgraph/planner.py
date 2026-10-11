"""Planner v1: draft from the request, ground it in the code, revise by rules.

1. **Draft** (model, request only): one agent without file tools lists the
   pieces of work the request names and estimates the changed lines. A small
   estimate takes the *fast path*: the single-agent graph, no further steps.
2. **Ground** (model, read-only tools): a second agent reads the code and
   turns every draft item into nodes with concrete files and symbols. It may
   split an item or add a contract, but never merges items. The program
   completes the answer into a :class:`Graph`, derives its edges, validates
   it, and applies the planner checks P1-P5; on errors the agent gets its
   draft and the errors back for up to ``max_revisions`` more rounds.
3. **Revise** (program, :mod:`revise`): nodes that can only queue on the
   same file are merged (M1), and a contract with one downstream node is
   merged into it (M2).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import (BaseModel, ConfigDict, Field, StrictInt, ValidationError,
                      field_validator)

from . import gitops
from .compare import is_test_only, is_test_path
from .derive import derive_edges
from .repo_index import RepoIndex, build_index
from .revise import RevisedGraph, goal_with_conventions, revise_graph
from .schema import Graph, RevisionEntry, dump_graph
from .single import single_graph
from .validate import Issue, ancestors, edit_files, unique_nodes, validate
from .workers import AqoursWorker, Worker, WorkerRequest, WorkerResult, write_json_atomic

PLANNER_VERSION = "planner-v1"
LONG_GOAL_CHARS = 1200
PLANNER_ENTRY_MODULE = "aqours_code.taskgraph.planner_entry"
PROMPT_PATH = Path(__file__).resolve().parent / "planner_prompt.md"
DRAFT_PROMPT_PATH = Path(__file__).resolve().parent / "planner_draft_prompt.md"
DEFAULT_PLANNER_TIMEOUT_S = 1800.0
MAX_REVISIONS = 2
# Step 1 runs at most this many times: once, and once more after a format error.
DRAFT_ATTEMPTS = 2
# Below this estimate of changed lines the request is not split (fast path).
DEFAULT_FAST_PATH_LINES = 500
# Repository files listed in the prompt; the agent can glob for the rest.
FILE_LIST_LIMIT = 300
DRAFT_ECHO_CHARS = 40_000

_FENCE = re.compile(r"^```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)^```[ \t]*$",
                    re.MULTILINE | re.DOTALL)


class DraftError(Exception):
    """The draft could not be turned into a graph."""

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(issue.format() for issue in issues))
        self.issues = issues


def _format_issue(message: str) -> Issue:
    return Issue("FORMAT", [], message)


def _as_id(value):
    """Let a model write an item id as a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


# ── Step 1: draft items ──

class DraftItem(BaseModel):
    """One piece of work named by the request."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""

    _id = field_validator("id", mode="before")(_as_id)


class DraftItems(BaseModel):
    """The answer of Step 1."""

    model_config = ConfigDict(extra="ignore")

    estimated_changed_lines: StrictInt = Field(ge=0)
    items: list[DraftItem] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def _unique_ids(cls, items: list[DraftItem]) -> list[DraftItem]:
        ids = [item.id for item in items]
        repeated = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
        if repeated:
            raise ValueError(f"item ids are repeated: {', '.join(repeated)}")
        return items


# ── Step 2: draft graph ──

class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftNode(_Draft):
    """One node as the planner writes it: no edges, flat edit set and check.

    ``item`` (the draft item an implement node belongs to) and ``reason`` (why
    an item was split or a contract added) do not enter the graph's nodes;
    they become ``revision_log`` entries and report fields.
    """

    id: str
    title: str
    kind: Literal["contract", "implement"]
    goal: str
    item: str | None = None
    reason: str | None = None
    modify: list[str] = Field(default_factory=list)
    create: list[str] = Field(default_factory=list)
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    requires_impl: list[str] = Field(default_factory=list)
    check: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)

    _item = field_validator("item", mode="before")(_as_id)


class Draft(_Draft):
    """The planner's answer.

    ``conventions`` are repository rules every node must follow; drafts
    written before the field existed simply have none.
    """

    conventions: list[str] = Field(default_factory=list)
    nodes: list[DraftNode] = Field(min_length=1)


def extract_json_block(text: str) -> str:
    """Return the last ```json code block of ``text``.

    Without a block tagged ``json``, fall back to the last untagged block,
    then to the whole text if it is a bare JSON object.
    """
    blocks = [(tag.lower(), body) for tag, body in _FENCE.findall(text)]
    tagged = [body for tag, body in blocks if tag == "json"]
    if tagged:
        return tagged[-1]
    untagged = [body for tag, body in blocks if not tag]
    if untagged:
        return untagged[-1]
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    raise DraftError([_format_issue("the final answer has no ```json code block")])


def _location(loc: tuple, node_ids: list[str]) -> str:
    parts: list[str] = []
    for i, item in enumerate(loc):
        if isinstance(item, int):
            if not parts:
                parts.append(f"[{item}]")
            elif loc[i - 1] == "nodes" and item < len(node_ids):
                parts[-1] = f"nodes[{item}] (id {node_ids[item]!r})"
            else:
                parts[-1] = f"{parts[-1]}[{item}]"
        elif item != "edit_set":
            parts.append(str(item))
    return ".".join(parts) or "draft"


def _pydantic_issues(exc: ValidationError, node_ids: list[str]) -> list[Issue]:
    return [_format_issue(f"{_location(tuple(error['loc']), node_ids)}: {error['msg']}")
            for error in exc.errors()]


def _load_json(text: str):
    block = extract_json_block(text)
    try:
        return json.loads(block)
    except json.JSONDecodeError as exc:
        raise DraftError([_format_issue(f"the last ```json block is not valid JSON: {exc}")]
                         ) from None


def parse_items(text: str) -> DraftItems:
    """Parse the answer of Step 1; format problems raise :class:`DraftError`."""
    data = _load_json(text)
    try:
        return DraftItems.model_validate(data)
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, [])) from None


def parse_draft(text: str) -> Draft:
    """Parse the planner's final answer into a :class:`Draft`."""
    data = _load_json(text)
    node_ids = [str(node.get("id", "?")) if isinstance(node, dict) else "?"
                for node in (data.get("nodes") if isinstance(data, dict) else None) or []]
    try:
        return Draft.model_validate(data)
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, node_ids)) from None


@dataclass
class GraphInfo:
    """Everything a graph needs besides its nodes."""

    request_id: str
    request: str
    repo: str
    base_commit: str
    final_checks: list[str]
    model: str | None


def _generator(info: GraphInfo, revision_mode: str) -> dict:
    return {"kind": "planner", "planner_version": PLANNER_VERSION, "model": info.model,
            "revision_mode": revision_mode}


def draft_to_graph(draft: Draft, info: GraphInfo, *, revised: bool) -> Graph:
    """Complete ``draft`` into a graph without edges; schema errors raise DraftError.

    The draft's ``conventions`` are appended to every node's goal.
    """
    nodes = [{
        "id": node.id,
        "title": node.title,
        "kind": node.kind,
        "goal": goal_with_conventions(node.goal, draft.conventions),
        "edit_set": {"modify": node.modify, "create": node.create},
        "requires": node.requires,
        "requires_impl": node.requires_impl,
        "provides": node.provides,
        "check": {"commands": node.check},
        "context_files": node.context_files,
    } for node in draft.nodes]
    try:
        return Graph.model_validate({
            "request_id": info.request_id,
            "request": info.request,
            "repo": info.repo,
            "base_commit": info.base_commit,
            "final_checks": info.final_checks,
            "generator": _generator(info, "llm" if revised else "none"),
            "nodes": nodes,
            "edges": [],
        })
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, [node.id for node in draft.nodes])) from None


def fast_path_graph(info: GraphInfo, repo: Path, estimate: int, threshold: int) -> Graph:
    """The single-agent graph of the ``single`` command, marked as the planner's choice."""
    graph = single_graph(info.request, repo, info.final_checks)
    data = graph.model_dump(mode="json", by_alias=True)
    data["generator"] = _generator(info, "none")
    data["revision_log"] = [{
        "action": "other", "nodes": [node["id"] for node in data["nodes"]],
        "reason": f"fast path: estimated {estimate} changed lines < threshold {threshold}"}]
    return Graph.model_validate(data)


# ── Planner-only checks ──

def _check_contract_order(graph: Graph) -> list[Issue]:
    """P1: no contract node may be an ancestor of another contract node."""
    contracts = [node.id for node in unique_nodes(graph) if node.kind == "contract"]
    ancestor_map = ancestors(graph)
    return [Issue("P1", [first, second],
                  f"contract {first} comes before contract {second} (after deriving the "
                  f"edges, {second} depends on {first}). Contracts must not be ordered: "
                  f"merge {first} and {second} into one contract node that makes all "
                  "their interface changes")
            for second in contracts for first in contracts
            if first != second and first in ancestor_map[second]]


def _check_test_only(graph: Graph) -> list[Issue]:
    """P2: no node may edit only files under ``tests/``."""
    return [Issue("P2", [node.id],
                  f"{node.id} only writes tests ({', '.join(sorted(edit_files(node)))}). "
                  "Remove this node, or move its work into the nodes it tests: each node "
                  "writes the tests for its own work, and the final checks run all tests "
                  "after the merge")
            for node in unique_nodes(graph) if is_test_only(node)]


def _check_contract_tests(graph: Graph) -> list[Issue]:
    """P3: contract nodes neither create nor modify files under ``tests/``."""
    issues = []
    for node in unique_nodes(graph):
        tests = sorted(path for path in edit_files(node) if is_test_path(path))
        if node.kind == "contract" and tests:
            issues.append(Issue(
                "P3", [node.id],
                f"contract {node.id} edits test files ({', '.join(tests)}). A contract "
                "writes no test files: remove them from its modify/create (the implement "
                "nodes write the tests for their behaviour); its check only runs the "
                "existing tests"))
    return issues


def planner_checks(graph: Graph) -> list[Issue]:
    """Errors P1-P3 for a planner graph whose edges are derived.

    Not part of :func:`validate`: hand-written graphs are not held to them.
    """
    return [*_check_contract_order(graph), *_check_test_only(graph),
            *_check_contract_tests(graph)]


def _has_reason(node: DraftNode) -> bool:
    return bool(node.reason and node.reason.strip())


def _nodes_of(draft: Draft, item_id: str) -> list[DraftNode]:
    return [node for node in draft.nodes if node.kind == "implement" and node.item == item_id]


def item_checks(draft: Draft, items: list[DraftItem]) -> list[Issue]:
    """Errors P4 and P5: how the draft graph's nodes relate to the draft items."""
    known = {item.id: item for item in items}
    issues = []
    for node in draft.nodes:
        if node.kind == "contract" and node.item is not None:
            issues.append(Issue(
                "P4", [node.id],
                f"contract {node.id} has item {node.item!r}. A contract belongs to no "
                "draft item: leave its `item` out"))
        elif node.kind == "implement" and node.item is None:
            issues.append(Issue(
                "P4", [node.id],
                f"{node.id} has no `item`. Every implement node belongs to exactly one "
                f"draft item: set `item` to one of {', '.join(known)}"))
        elif node.kind == "implement" and node.item not in known:
            issues.append(Issue(
                "P4", [node.id],
                f"{node.id} has item {node.item!r}, which is not a draft item. Use one "
                f"of {', '.join(known)}"))
    for item in items:
        nodes = _nodes_of(draft, item.id)
        if not nodes:
            issues.append(Issue(
                "P4", [],
                f"draft item {item.id} ({item.title}) has no implement node. Every draft "
                "item needs at least one implement node with that `item`"))
        elif len(nodes) > 1 and not any(_has_reason(node) for node in nodes):
            issues.append(Issue(
                "P5", [node.id for node in nodes],
                f"draft item {item.id} ({item.title}) is split into {len(nodes)} nodes "
                "without a `reason`. Say in `reason` why the code requires the split, or "
                "make it one node"))
    for node in draft.nodes:
        if node.kind == "contract" and not _has_reason(node):
            issues.append(Issue(
                "P5", [node.id],
                f"contract {node.id} has no `reason`. Say in `reason` which nodes share "
                "the interface it adds, or remove the contract"))
    return issues


def item_revisions(draft: Draft, items: list[DraftItem]) -> list[RevisionEntry]:
    """``split`` and ``add_node`` entries describing how Step 2 changed the draft."""
    entries = []
    for item in items:
        nodes = _nodes_of(draft, item.id)
        if len(nodes) > 1:
            reasons = list(dict.fromkeys(node.reason.strip() for node in nodes
                                         if _has_reason(node)))
            entries.append(RevisionEntry(
                action="split", nodes=[node.id for node in nodes],
                reason=f"draft item {item.id} ({item.title}) was split: "
                       + "; ".join(reasons)))
    for node in draft.nodes:
        if node.kind == "contract":
            entries.append(RevisionEntry(
                action="add_node", nodes=[node.id],
                reason=f"contract added: {(node.reason or '').strip()}"))
    return entries


# ── Prompts ──

def load_prompt() -> str:
    """Return the instructions of Step 2."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def load_draft_prompt() -> str:
    """Return the instructions of Step 1."""
    return DRAFT_PROMPT_PATH.read_text(encoding="utf-8")


def _file_list(index: RepoIndex) -> str:
    files = sorted(index.files)
    lines = [f"- {path}" for path in files[:FILE_LIST_LIMIT]]
    if len(files) > FILE_LIST_LIMIT:
        lines.append(f"- ... and {len(files) - FILE_LIST_LIMIT} more (use glob)")
    return "\n".join(lines) or "- (empty)"


def file_sizes(root: Path, index: RepoIndex) -> list[tuple[str, int | None]]:
    """Every indexed file with its number of lines (None for binary or unreadable)."""
    sizes = []
    for path in sorted(index.files):
        try:
            data = (root / path).read_bytes()
        except OSError:
            sizes.append((path, None))
            continue
        if b"\0" in data:
            sizes.append((path, None))
        else:
            sizes.append((path, data.count(b"\n") + (1 if data and not data.endswith(b"\n")
                                                     else 0)))
    return sizes


def _size_list(sizes: list[tuple[str, int | None]]) -> str:
    lines = [f"- {path}: {count} lines" if count is not None else f"- {path}: binary"
             for path, count in sizes[:FILE_LIST_LIMIT]]
    if len(sizes) > FILE_LIST_LIMIT:
        rest = sum(count or 0 for _, count in sizes[FILE_LIST_LIMIT:])
        lines.append(f"- ... and {len(sizes) - FILE_LIST_LIMIT} more files ({rest} lines)")
    return "\n".join(lines) or "- (empty)"


def build_draft_prompt(request: str, sizes: list[tuple[str, int | None]],
                       previous: str | None = None,
                       errors: list[Issue] | None = None) -> str:
    """Return the prompt of Step 1: the request and file sizes, never file content."""
    parts = [
        load_draft_prompt().rstrip(),
        "# The request\n\n" + request.strip(),
        "# Repository files\n\n" + _size_list(sizes),
    ]
    if errors:
        problems = "\n".join(f"- {issue.format()}" for issue in errors)
        answer = (previous or "")[-DRAFT_ECHO_CHARS:].strip() or "(empty answer)"
        parts.append(
            "# Fix your previous answer\n\nYour previous answer could not be read. "
            "Answer again with the complete list as the last ```json block.\n\n"
            f"## Previous answer\n\n{answer}\n\n## Problems\n\n{problems}")
    return "\n\n".join(parts) + "\n"


def _items_section(items: list[DraftItem]) -> str:
    lines = [f"- {item.id}: **{item.title}**" + (f". {item.description.strip()}"
                                                 if item.description.strip() else "")
             for item in items]
    return ("# Draft items\n\nThe pieces of work in the request, listed from the request "
            "text alone. Every implement node belongs to one of them (`item`).\n\n"
            + "\n".join(lines))


@dataclass
class Revision:
    """What the next round must fix."""

    number: int
    draft: str
    errors: list[Issue]


def build_prompt(request: str, index: RepoIndex, final_checks: list[str],
                 revision: Revision | None = None,
                 items: list[DraftItem] | None = None) -> str:
    """Return the full prompt of one round of Step 2."""
    checks = "\n".join(f"- `{command}`" for command in final_checks) or "- (none)"
    parts = [
        load_prompt().rstrip(),
        "# The request\n\n" + request.strip(),
        _items_section(items or []),
        ("# Repository\n\nThe repository is checked out in your working directory. "
         f"Its files:\n\n{_file_list(index)}"),
        ("# Final checks\n\nAfter all sub-tasks are merged, these commands check the "
         f"whole result:\n\n{checks}"),
    ]
    if revision is not None:
        problems = "\n".join(f"- {issue.format()}" for issue in revision.errors)
        draft = revision.draft[-DRAFT_ECHO_CHARS:].strip() or "(empty answer)"
        parts.append(
            f"# Fix your previous draft (revision {revision.number} of {MAX_REVISIONS})\n\n"
            "You already planned this request once; your draft is below, followed by "
            "the problems the program found after deriving the edges, validating "
            "the graph and applying the planner checks (P1-P5). Fix every problem "
            "and answer with the complete corrected draft (all nodes, not only the "
            "changed ones) as the last ```json block. Keep the parts that were "
            "fine.\n\n"
            f"## Previous draft\n\n```json\n{draft}\n```\n\n"
            f"## Problems\n\n{problems}")
    return "\n\n".join(parts) + "\n"


# ── Running ──

@dataclass
class PlanOptions:
    """Parameters of one ``plan`` run.

    ``fast_path_lines`` is the fast-path threshold (None turns the fast path
    off); ``merge`` is False for ``--no-merge``.
    """

    request_path: Path
    repo: Path
    out: Path
    final_checks: list[str] = field(default_factory=list)
    timeout_s: float = DEFAULT_PLANNER_TIMEOUT_S
    max_revisions: int = MAX_REVISIONS
    request_id: str | None = None
    fast_path_lines: int | None = DEFAULT_FAST_PATH_LINES
    merge: bool = True


@dataclass
class PlanResult:
    """Outcome of :func:`run_plan`."""

    success: bool
    graph: Graph | None
    report: dict
    report_path: Path


def planner_worker() -> AqoursWorker:
    """The Aqours worker running Step 2 (read-only tools) in a child process."""
    return AqoursWorker(entry_command=[sys.executable, "-m", PLANNER_ENTRY_MODULE])


def draft_worker() -> AqoursWorker:
    """The Aqours worker running Step 1 (no file tools) in a child process."""
    return AqoursWorker(entry_command=[sys.executable, "-m", PLANNER_ENTRY_MODULE,
                                       "--stage", "draft"])


def report_path_for(out: Path) -> Path:
    """``<out>.report.json`` next to ``out``."""
    return out.with_name(f"{out.name}.report.json")


def unmerged_path_for(out: Path) -> Path:
    """``<out>.unmerged.json`` next to ``out``: the graph before Step 3."""
    return out.with_name(f"{out.name}.unmerged.json")


def _issue_dict(issue: Issue) -> dict:
    return {"code": issue.code, "nodes": issue.nodes, "message": issue.message}


def _agent_dict(result: WorkerResult) -> dict:
    return {"ok": result.ok, "reason": result.reason, "error": result.error[-4000:],
            "model_calls": result.model_calls, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_s": round(result.duration_s, 3)}


def _totals(rounds: list[dict]) -> dict:
    return {key: sum(entry["agent"][key] for entry in rounds)
            for key in ("model_calls", "input_tokens", "output_tokens")}


def _make_writable(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete ``path``, including read-only git objects on Windows."""
    shutil.rmtree(path, onerror=_make_writable)


def _agent_failure(result: WorkerResult) -> Issue:
    return Issue("AGENT", [], f"planner agent failed ({result.reason}): "
                 f"{result.error[-2000:]}")


def run_draft_stage(request: str, sizes: list[tuple[str, int | None]], worker: Worker,
                    clone: Path, log_dir: Path, timeout_s: float
                    ) -> tuple[DraftItems | None, list[dict], list[Issue]]:
    """Step 1: ask for the draft items, once more after a format error."""
    rounds: list[dict] = []
    errors: list[Issue] = []
    previous: str | None = None
    for attempt in range(1, DRAFT_ATTEMPTS + 1):
        prompt = build_draft_prompt(request, sizes, previous, errors)
        result = worker.run(WorkerRequest(
            node_id="planner-draft", attempt=attempt, prompt=prompt, workspace=clone,
            log_dir=log_dir, timeout_s=timeout_s))
        items: DraftItems | None = None
        try:
            items = parse_items(result.final_answer)
            errors = []
        except DraftError as exc:
            errors = exc.issues
        if not result.ok:
            errors = [_agent_failure(result), *errors]
            items = None
        rounds.append({"round": attempt, "answer": result.final_answer,
                       "errors": [_issue_dict(issue) for issue in errors],
                       "agent": _agent_dict(result)})
        if items is not None:
            return items, rounds, []
        previous = result.final_answer
    return None, rounds, errors


@dataclass
class Evaluation:
    """One answer of Step 2, completed and checked."""

    graph: Graph | None
    draft_text: str
    conventions: list[str]
    errors: list[Issue]
    warnings: list[Issue]
    node_items: dict[str, dict] = field(default_factory=dict)


def _evaluate_answer(answer: str, info: GraphInfo, index: RepoIndex, revised: bool,
                     items: list[DraftItem] | None = None) -> Evaluation:
    """Complete, derive, validate and check one answer."""
    try:
        draft_text = extract_json_block(answer)
        draft = parse_draft(answer)
        graph = draft_to_graph(draft, info, revised=revised)
    except DraftError as exc:
        return Evaluation(None, answer, [], exc.issues, [])
    derived, _entries = derive_edges(graph, index)
    report = validate(derived, index)
    errors = [*report.errors, *planner_checks(derived)]
    if items is not None:
        errors += item_checks(draft, items)
        if not errors:
            derived = derived.model_copy(update={"revision_log": [
                *item_revisions(draft, items), *derived.revision_log]})
    node_items = {node.id: {"item": node.item, "reason": node.reason}
                  for node in draft.nodes}
    return Evaluation(derived, draft_text, list(draft.conventions), errors,
                      report.warnings, node_items)


def _with_revision_mode(graph: Graph, mode: str) -> Graph:
    return graph.model_copy(update={
        "generator": graph.generator.model_copy(update={"revision_mode": mode})})


def revise_checked(graph: Graph, conventions: list[str], index: RepoIndex
                   ) -> tuple[RevisedGraph, list[Issue]]:
    """Step 3 plus the checks that guard it; errors mean a bug in the rules."""
    revised = revise_graph(graph, conventions)
    if revised.merges:
        revised.graph = _with_revision_mode(revised.graph, "rule_assisted")
    report = validate(revised.graph, index)
    problems = [*report.errors, *planner_checks(revised.graph)]
    errors = [Issue("REVISE", issue.nodes,
                    f"the graph is invalid after merging ({issue.format()}); the graph "
                    "from before the merge was written instead")
              for issue in problems]
    return revised, errors


def run_plan(options: PlanOptions, worker: Worker,
             draft_stage_worker: Worker | None = None) -> PlanResult:
    """Run the three planner steps; write the graph, the unmerged graph and the report.

    ``worker`` runs Step 2; ``draft_stage_worker`` runs Step 1 (default: ``worker``).
    """
    started = time.monotonic()
    draft_stage_worker = draft_stage_worker or worker
    request = options.request_path.read_text(encoding="utf-8")
    repo = options.repo.resolve()
    out = options.out
    out.parent.mkdir(parents=True, exist_ok=True)
    log_dir = out.with_name(f"{out.name}.logs")
    base_commit = gitops.head(repo)
    model = worker.describe().get("model")
    info = GraphInfo(request_id=options.request_id or out.stem, request=request,
                     repo=str(repo), base_commit=base_commit,
                     final_checks=list(options.final_checks),
                     model=str(model) if model is not None else None)
    threshold = options.fast_path_lines
    draft_items: DraftItems | None = None
    stage1: list[dict] = []
    rounds: list[dict] = []
    graph: Graph | None = None
    unmerged: Graph | None = None
    graph_round: int | None = None
    graph_draft: str | None = None
    conventions: list[str] = []
    node_items: dict[str, dict] = {}
    errors: list[Issue] = []
    revised: RevisedGraph | None = None
    fast_path = False
    tmp = Path(tempfile.mkdtemp(prefix="tg-plan-"))
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, base_commit)
        index = build_index(clone, base_commit)
        draft_items, stage1, errors = run_draft_stage(
            request, file_sizes(clone, index), draft_stage_worker, clone,
            log_dir / "draft", options.timeout_s)
        if draft_items is not None:
            estimate = draft_items.estimated_changed_lines
            fast_path = threshold is not None and estimate < threshold
        if fast_path:
            graph = fast_path_graph(info, repo, estimate, threshold)
            errors = list(validate(graph, index).errors)
        elif draft_items is not None:
            revision: Revision | None = None
            for number in range(options.max_revisions + 1):
                prompt = build_prompt(request, index, info.final_checks, revision,
                                      draft_items.items)
                result = worker.run(WorkerRequest(
                    node_id="planner", attempt=number + 1, prompt=prompt, workspace=clone,
                    log_dir=log_dir, timeout_s=options.timeout_s))
                evaluation = _evaluate_answer(result.final_answer, info, index,
                                              revised=number > 0, items=draft_items.items)
                errors, warnings = evaluation.errors, evaluation.warnings
                if not result.ok:
                    errors = [_agent_failure(result), *errors]
                if evaluation.graph is not None:
                    graph, graph_round = evaluation.graph, number + 1
                    graph_draft, conventions = evaluation.draft_text, evaluation.conventions
                    node_items = evaluation.node_items
                rounds.append({
                    "round": number + 1,
                    "draft": result.final_answer,
                    "draft_json": evaluation.draft_text,
                    "conventions": evaluation.conventions,
                    "errors": [_issue_dict(issue) for issue in errors],
                    "warnings": [_issue_dict(issue) for issue in warnings],
                    "agent": _agent_dict(result),
                })
                if not errors:
                    break
                revision = Revision(number=number + 1, draft=evaluation.draft_text,
                                    errors=errors)
            if not errors and graph is not None and options.merge:
                unmerged = graph
                revised, errors = revise_checked(graph, conventions, index)
                if not errors:
                    graph = revised.graph
    finally:
        remove_tree(tmp)
    success = not errors and graph is not None
    unmerged_path = unmerged_path_for(out)
    if graph is not None:
        dump_graph(graph, out)
    else:
        out.unlink(missing_ok=True)  # never leave a stale graph from an earlier run
    if unmerged is not None:
        dump_graph(unmerged, unmerged_path)
    else:
        unmerged_path.unlink(missing_ok=True)
    before = unmerged if unmerged is not None else graph
    merged = revised is not None and success
    stage_totals = {"stage1": _totals(stage1), "stage2": _totals(rounds)}
    report = {
        "planner_version": PLANNER_VERSION,
        "model": info.model,
        "request": str(options.request_path),
        "repo": str(repo),
        "base_commit": base_commit,
        "out": str(out),
        "success": success,
        "errors": [_issue_dict(issue) for issue in errors],
        "draft_items": ([item.model_dump() for item in draft_items.items]
                        if draft_items is not None else []),
        "estimated_changed_lines": (draft_items.estimated_changed_lines
                                    if draft_items is not None else None),
        "fast_path": {"taken": fast_path,
                      "estimated_changed_lines": (draft_items.estimated_changed_lines
                                                  if draft_items is not None else None),
                      "threshold": threshold},
        "stage1": stage1,
        "revision_rounds": max(len(rounds) - 1, 0),
        "graph_written": graph is not None,
        "graph_round": graph_round,
        "draft": graph_draft,
        "conventions": conventions,
        "node_items": node_items,
        "merge": options.merge,
        "merges": [merge.to_dict() for merge in revised.merges] if merged else [],
        "not_merged": revised.not_merged if merged else [],
        "unmerged_graph": str(unmerged_path) if unmerged is not None else None,
        "nodes_before_merge": len(before.nodes) if before is not None else 0,
        "nodes": len(graph.nodes) if graph is not None else 0,
        "edges": len(graph.edges) if graph is not None else 0,
        "goal_chars": {node.id: len(node.goal) for node in graph.nodes} if graph is not None else {},
        "rounds": rounds,
        "totals": {key: stage_totals["stage1"][key] + stage_totals["stage2"][key]
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "stage_totals": stage_totals,
        "duration_s": round(time.monotonic() - started, 3),
    }
    report_path = report_path_for(out)
    write_json_atomic(report_path, report)
    return PlanResult(success=success, graph=graph, report=report,
                      report_path=report_path)
