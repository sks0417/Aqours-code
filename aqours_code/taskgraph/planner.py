"""Planner v0: let an agent split a request into a task graph.

The agent reads the request and a clone of the repository with read-only
tools and answers with a draft (nodes only, see ``planner_prompt.md``). The
program completes the draft into a :class:`Graph`, derives its edges, and
validates it; when the graph has errors, the agent gets the draft and the
errors back for up to ``max_revisions`` more rounds.
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import gitops
from .derive import derive_edges
from .repo_index import RepoIndex, build_index
from .schema import Graph, dump_graph
from .validate import Issue, validate
from .workers import AqoursWorker, Worker, WorkerRequest, WorkerResult, write_json_atomic

PLANNER_VERSION = "planner-v0"
PLANNER_ENTRY_MODULE = "aqours_code.taskgraph.planner_entry"
PROMPT_PATH = Path(__file__).resolve().parent / "planner_prompt.md"
DEFAULT_PLANNER_TIMEOUT_S = 1800.0
MAX_REVISIONS = 2
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


# ── Draft format ──

class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftNode(_Draft):
    """One node as the planner writes it: no edges, flat edit set and check."""

    id: str
    title: str
    kind: Literal["contract", "implement"]
    goal: str
    modify: list[str] = Field(default_factory=list)
    create: list[str] = Field(default_factory=list)
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    requires_impl: list[str] = Field(default_factory=list)
    check: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)


class Draft(_Draft):
    """The planner's answer."""

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


def parse_draft(text: str) -> Draft:
    """Parse the planner's final answer into a :class:`Draft`."""
    block = extract_json_block(text)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise DraftError([_format_issue(f"the last ```json block is not valid JSON: {exc}")]
                         ) from None
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


def draft_to_graph(draft: Draft, info: GraphInfo, *, revised: bool) -> Graph:
    """Complete ``draft`` into a graph without edges; schema errors raise DraftError."""
    nodes = [{
        "id": node.id,
        "title": node.title,
        "kind": node.kind,
        "goal": node.goal,
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
            "generator": {"kind": "planner", "planner_version": PLANNER_VERSION,
                          "model": info.model,
                          "revision_mode": "llm" if revised else "none"},
            "nodes": nodes,
            "edges": [],
        })
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, [node.id for node in draft.nodes])) from None


# ── Prompt ──

def load_prompt() -> str:
    """Return the planner instructions."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _file_list(index: RepoIndex) -> str:
    files = sorted(index.files)
    lines = [f"- {path}" for path in files[:FILE_LIST_LIMIT]]
    if len(files) > FILE_LIST_LIMIT:
        lines.append(f"- ... and {len(files) - FILE_LIST_LIMIT} more (use glob)")
    return "\n".join(lines) or "- (empty)"


@dataclass
class Revision:
    """What the next round must fix."""

    number: int
    draft: str
    errors: list[Issue]


def build_prompt(request: str, index: RepoIndex, final_checks: list[str],
                 revision: Revision | None = None) -> str:
    """Return the full prompt of one planner round."""
    checks = "\n".join(f"- `{command}`" for command in final_checks) or "- (none)"
    parts = [
        load_prompt().rstrip(),
        "# The request\n\n" + request.strip(),
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
            "the problems the program found after deriving the edges and validating "
            "the graph. Fix every problem and answer with the complete corrected draft "
            "(all nodes, not only the changed ones) as the last ```json block. Keep "
            "the parts that were fine.\n\n"
            f"## Previous draft\n\n```json\n{draft}\n```\n\n"
            f"## Problems\n\n{problems}")
    return "\n\n".join(parts) + "\n"


# ── Running ──

@dataclass
class PlanOptions:
    """Parameters of one ``plan`` run."""

    request_path: Path
    repo: Path
    out: Path
    final_checks: list[str] = field(default_factory=list)
    timeout_s: float = DEFAULT_PLANNER_TIMEOUT_S
    max_revisions: int = MAX_REVISIONS
    request_id: str | None = None


@dataclass
class PlanResult:
    """Outcome of :func:`run_plan`."""

    success: bool
    graph: Graph | None
    report: dict
    report_path: Path


def planner_worker() -> AqoursWorker:
    """The Aqours worker running the planner entry in a child process."""
    return AqoursWorker(entry_command=[sys.executable, "-m", PLANNER_ENTRY_MODULE])


def report_path_for(out: Path) -> Path:
    """``<out>.report.json`` next to ``out``."""
    return out.with_name(f"{out.name}.report.json")


def _issue_dict(issue: Issue) -> dict:
    return {"code": issue.code, "nodes": issue.nodes, "message": issue.message}


def _agent_dict(result: WorkerResult) -> dict:
    return {"ok": result.ok, "reason": result.reason, "error": result.error[-4000:],
            "model_calls": result.model_calls, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_s": round(result.duration_s, 3)}


def _make_writable(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete ``path``, including read-only git objects on Windows."""
    shutil.rmtree(path, onerror=_make_writable)


def _evaluate_answer(answer: str, info: GraphInfo, index: RepoIndex, revised: bool
                     ) -> tuple[Graph | None, str, list[Issue], list[Issue]]:
    """Return (derived graph, draft text, errors, warnings) for one answer."""
    try:
        draft_text = extract_json_block(answer)
        graph = draft_to_graph(parse_draft(answer), info, revised=revised)
    except DraftError as exc:
        return None, answer, exc.issues, []
    derived, _entries = derive_edges(graph, index)
    report = validate(derived, index)
    return derived, draft_text, report.errors, report.warnings


def run_plan(options: PlanOptions, worker: Worker) -> PlanResult:
    """Generate, complete, validate and revise a graph; write it and its report."""
    started = time.monotonic()
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
    rounds: list[dict] = []
    graph: Graph | None = None
    graph_round: int | None = None
    errors: list[Issue] = []
    revision: Revision | None = None
    tmp = Path(tempfile.mkdtemp(prefix="tg-plan-"))
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, base_commit)
        index = build_index(clone, base_commit)
        for number in range(options.max_revisions + 1):
            prompt = build_prompt(request, index, info.final_checks, revision)
            result = worker.run(WorkerRequest(
                node_id="planner", attempt=number + 1, prompt=prompt, workspace=clone,
                log_dir=log_dir, timeout_s=options.timeout_s))
            candidate, draft_text, errors, warnings = _evaluate_answer(
                result.final_answer, info, index, revised=number > 0)
            if not result.ok:
                errors = [Issue("AGENT", [], f"planner agent failed ({result.reason}): "
                                f"{result.error[-2000:]}"), *errors]
            if candidate is not None:
                graph, graph_round = candidate, number + 1
            rounds.append({
                "round": number + 1,
                "draft": result.final_answer,
                "errors": [_issue_dict(issue) for issue in errors],
                "warnings": [_issue_dict(issue) for issue in warnings],
                "agent": _agent_dict(result),
            })
            if not errors:
                break
            revision = Revision(number=number + 1, draft=draft_text, errors=errors)
    finally:
        remove_tree(tmp)
    success = not errors and graph is not None
    if graph is not None:
        dump_graph(graph, out)
    else:
        out.unlink(missing_ok=True)  # never leave a stale graph from an earlier run
    report = {
        "planner_version": PLANNER_VERSION,
        "model": info.model,
        "request": str(options.request_path),
        "repo": str(repo),
        "base_commit": base_commit,
        "out": str(out),
        "success": success,
        "revision_rounds": len(rounds) - 1,
        "graph_written": graph is not None,
        "graph_round": graph_round,
        "nodes": len(graph.nodes) if graph is not None else 0,
        "edges": len(graph.edges) if graph is not None else 0,
        "rounds": rounds,
        "totals": {key: sum(entry["agent"][key] for entry in rounds)
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "duration_s": round(time.monotonic() - started, 3),
    }
    report_path = report_path_for(out)
    write_json_atomic(report_path, report)
    return PlanResult(success=success, graph=graph, report=report,
                      report_path=report_path)
