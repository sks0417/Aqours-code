"""Planner v3: the model reports task facts, the program decides the structure.

1. **Draft** (model, request only): exactly as in v2 (:mod:`planner`),
   including the fast path.
2. **Facts** (model, read-only tools): the agent gets the outline of the code
   and :data:`FACTS_PROMPT_PATH`, and describes the work as units
   (:mod:`facts`). The program checks the facts (F1-F9); errors go back to
   the agent for up to ``max_revisions`` more rounds.
3. **Synthesis** (program, :mod:`synthesize`): the graph is built from the
   facts by the rules of the four properties. A contradiction in the facts
   (``CONFLICT``) goes back to the agent like a check error; a broken
   invariant of the program (``SYNTH``) does not, and fails the run.

v3 does not run the merges M1 and M2 of v2 (:mod:`revise`).

:func:`run_resynth` (the ``resynth`` command) runs Step 3 again on the facts
saved in a v3 report, without a model: the same facts under other rules or
another ``--ablate``, at no planning cost.
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from . import gitops
from .affected import TestImports, scan_test_imports
from .cleanup import remove_tree
from .facts import Facts, check_facts, parse_facts
from .outline import Outline, build_outline
from .planner import (DRAFT_ECHO_CHARS, PLANNER_VERSION, REPORTED_GOAL_CHARS, DraftError,
                      DraftItem, GraphInfo, PlanOptions, PlanResult, _agent_dict,
                      _agent_failure, _file_list, _issue_dict, _totals, extract_json_block,
                      fast_path_graph, file_sizes, report_path_for, run_draft_stage)
from .repo_index import RepoIndex, build_index
from .schema import Graph, dump_graph, load_graph
from .synthesize import FactsConflict, Synthesis, SynthesisError, synthesize
from .validate import Issue, validate
from .workers import Worker, WorkerRequest, write_json_atomic

FACTS_PROMPT_PATH = Path(__file__).resolve().parent / "planner_facts_prompt.md"


def load_facts_prompt() -> str:
    """Return the instructions of Step 2 in v3."""
    return FACTS_PROMPT_PATH.read_text(encoding="utf-8")


def _items_section(items: list[DraftItem]) -> str:
    lines = [f"- {item.id}: **{item.title}**" + (f". {item.description.strip()}"
                                                 if item.description.strip() else "")
             for item in items]
    return ("# Draft items\n\nThe pieces of work in the request, listed from the request "
            "text alone. Every unit belongs to one of them (`item`).\n\n" + "\n".join(lines))


@dataclass
class FactsRevision:
    """What the next round must fix."""

    number: int
    max_revisions: int
    facts: str
    errors: list[Issue]


def build_facts_prompt(request: str, index: RepoIndex, items: list[DraftItem],
                       outline: str | None = None,
                       revision: FactsRevision | None = None) -> str:
    """Return the full prompt of one round of Step 2 in v3."""
    parts = [
        load_facts_prompt().rstrip(),
        "# The request\n\n" + request.strip(),
        _items_section(items),
        ("# Repository\n\nThe repository is checked out in your working directory. "
         f"Its files:\n\n{_file_list(index)}"),
    ]
    if outline is not None:
        parts.append("# Repository outline\n\n" + outline.strip())
    if revision is not None:
        problems = "\n".join(f"- {issue.format()}" for issue in revision.errors)
        facts = revision.facts[-DRAFT_ECHO_CHARS:].strip() or "(empty answer)"
        parts.append(
            f"# Fix your previous answer (revision {revision.number} of "
            f"{revision.max_revisions})\n\n"
            "You already described this request once; your answer is below, followed by "
            "the problems the program found in it. Fix every problem and answer with the "
            "complete corrected description (all units, not only the changed ones) as the "
            "last ```json block. Keep the parts that were fine.\n\n"
            f"## Previous answer\n\n```json\n{facts}\n```\n\n"
            f"## Problems\n\n{problems}")
    return "\n\n".join(parts) + "\n"


@dataclass
class FactsEvaluation:
    """One answer of Step 2, checked and synthesized."""

    facts_text: str
    facts: Facts | None = None
    synthesis: Synthesis | None = None
    errors: list[Issue] = field(default_factory=list)
    # True when the program, not the facts, failed: another round cannot help.
    program_error: bool = False


def evaluate_facts(answer: str, info: GraphInfo, index: RepoIndex, items: list[DraftItem],
                   tests: TestImports, ablate: frozenset[str],
                   revised: bool) -> FactsEvaluation:
    """Parse, check and synthesize one answer of Step 2."""
    try:
        facts_text = extract_json_block(answer)
        facts = parse_facts(answer)
    except DraftError as exc:
        return FactsEvaluation(answer, errors=exc.issues)
    evaluation = FactsEvaluation(facts_text, facts)
    evaluation.errors = check_facts(facts, items, index)
    if evaluation.errors:
        return evaluation
    try:
        evaluation.synthesis = synthesize(facts, index, info, tests, ablate=ablate,
                                          revised=revised)
    except FactsConflict as exc:
        evaluation.errors = exc.issues
    except SynthesisError as exc:
        evaluation.errors = [Issue("SYNTH", [], f"the program broke an invariant {exc}")]
        evaluation.program_error = True
    return evaluation


def _round_record(number: int, answer: str, evaluation: FactsEvaluation,
                  errors: list[Issue], agent: dict) -> dict:
    return {"round": number, "answer": answer, "facts_json": evaluation.facts_text,
            "errors": [_issue_dict(issue) for issue in errors], "agent": agent}


@dataclass
class _V3Run:
    """What one v3 run (``plan`` or ``resynth``) produced, for the graph and report."""

    info: GraphInfo
    request: str  # what the report names as the request
    repo: Path
    out: Path
    ablate: frozenset[str]
    draft_items: list[dict] = field(default_factory=list)
    estimate: int | None = None
    fast_path: bool = False
    threshold: int | None = None
    stage1: list[dict] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    graph: Graph | None = None
    graph_round: int | None = None
    facts: Facts | None = None
    synthesis: Synthesis | None = None
    errors: list[Issue] = field(default_factory=list)
    outline: Outline = field(default_factory=lambda: Outline(""))
    tests: TestImports = field(default_factory=TestImports)


def _finish(run: _V3Run, started: float, extra: dict | None = None) -> PlanResult:
    """Write the graph (or remove a stale one) and the report."""
    graph, synthesis, facts = run.graph, run.synthesis, run.facts
    success = not run.errors and graph is not None
    if graph is not None:
        dump_graph(graph, run.out)
    else:
        run.out.unlink(missing_ok=True)  # never leave a stale graph from an earlier run
    stage_totals = {"stage1": _totals(run.stage1), "stage2": _totals(run.rounds)}
    goal_chars = {node.id: len(node.goal) for node in graph.nodes} if graph is not None else {}
    built = synthesis is not None and success
    report = {
        "planner": "v3",
        "planner_version": PLANNER_VERSION,
        "model": run.info.model,
        "request": run.request,
        "repo": str(run.repo),
        "base_commit": run.info.base_commit,
        "out": str(run.out),
        "success": success,
        "errors": [_issue_dict(issue) for issue in run.errors],
        "ablate": sorted(run.ablate),
        "draft_items": run.draft_items,
        "estimated_changed_lines": run.estimate,
        "fast_path": {"taken": run.fast_path, "estimated_changed_lines": run.estimate,
                      "threshold": run.threshold},
        "stage1": run.stage1,
        "revision_rounds": max(len(run.rounds) - 1, 0),
        "revision_reasons": [{"round": entry["round"],
                              "codes": list(dict.fromkeys(issue["code"]
                                                          for issue in entry["errors"]))}
                             for entry in run.rounds[:-1]],
        "graph_written": graph is not None,
        "graph_round": run.graph_round,
        "facts": facts.model_dump(mode="json") if facts is not None else None,
        "conventions": list(facts.conventions) if facts is not None else [],
        "units": len(facts.units) if facts is not None else 0,
        # uses the model marked "behavior" without naming the result (recorded only)
        "behavior_without_why": [{"unit": unit.id, "symbol": use.symbol}
                                 for unit in (facts.units if facts is not None else [])
                                 for use in unit.uses
                                 if use.need == "behavior" and not use.why.strip()],
        "node_units": synthesis.node_units if built else {},
        "decisions": [decision.to_dict() for decision in synthesis.decisions] if built else [],
        "decision_counts": synthesis.counts() if built else {},
        "pre_node": synthesis.pre if built else None,
        "reg_node": synthesis.reg if built else None,
        "parallelism": synthesis.parallelism if built else None,
        "nodes": len(graph.nodes) if graph is not None else 0,
        "edges": len(graph.edges) if graph is not None else 0,
        "goal_chars": goal_chars,
        "long_goals": {node_id: chars for node_id, chars in goal_chars.items()
                       if chars > REPORTED_GOAL_CHARS},
        "outline_chars": run.outline.chars,
        "outline_reduction": run.outline.reduction,
        "outline_omitted_files": run.outline.omitted_files,
        "test_imports": {"files": len(run.tests.imports), "sources": len(run.tests.sources),
                         "unresolved": run.tests.unresolved},
        "rounds": run.rounds,
        "totals": {key: stage_totals["stage1"][key] + stage_totals["stage2"][key]
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "stage_totals": stage_totals,
        "duration_s": round(time.monotonic() - started, 3),
        **(extra or {}),
    }
    report_path = report_path_for(run.out)
    write_json_atomic(report_path, report)
    return PlanResult(success=success, graph=graph, report=report, report_path=report_path)


def run_plan_v3(options: PlanOptions, worker: Worker,
                draft_stage_worker: Worker | None = None) -> PlanResult:
    """Run planner v3; write the graph and the report."""
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
    run = _V3Run(info, str(options.request_path), repo, out, frozenset(options.ablate),
                 threshold=options.fast_path_lines)
    tmp = Path(tempfile.mkdtemp(prefix="tg-plan-"))
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, base_commit)
        index = build_index(clone, base_commit)
        run.outline = build_outline(clone, index)
        run.tests = scan_test_imports(clone, index)
        draft_items, run.stage1, run.errors = run_draft_stage(
            request, file_sizes(clone, index), draft_stage_worker, clone,
            log_dir / "draft", options.timeout_s)
        if draft_items is not None:
            run.draft_items = [item.model_dump() for item in draft_items.items]
            run.estimate = draft_items.estimated_changed_lines
            run.fast_path = run.threshold is not None and run.estimate < run.threshold
        if run.fast_path:
            run.graph = fast_path_graph(info, repo, run.estimate, run.threshold,
                                        PLANNER_VERSION)
            run.errors = list(validate(run.graph, index).errors)
        elif draft_items is not None:
            revision: FactsRevision | None = None
            for number in range(options.max_revisions + 1):
                prompt = build_facts_prompt(request, index, draft_items.items,
                                            run.outline.text, revision)
                result = worker.run(WorkerRequest(
                    node_id="planner", attempt=number + 1, prompt=prompt, workspace=clone,
                    log_dir=log_dir, timeout_s=options.timeout_s))
                evaluation = evaluate_facts(result.final_answer, info, index,
                                            draft_items.items, run.tests, run.ablate,
                                            revised=number > 0)
                run.errors = list(evaluation.errors)
                if not result.ok:
                    run.errors = [_agent_failure(result), *run.errors]
                if evaluation.facts is not None:
                    run.facts = evaluation.facts
                if evaluation.synthesis is not None and result.ok:
                    run.synthesis, run.graph_round = evaluation.synthesis, number + 1
                    run.graph = evaluation.synthesis.graph
                run.rounds.append(_round_record(number + 1, result.final_answer, evaluation,
                                                run.errors, _agent_dict(result)))
                if not run.errors or evaluation.program_error:
                    break
                revision = FactsRevision(number + 1, options.max_revisions,
                                         evaluation.facts_text, run.errors)
    finally:
        remove_tree(tmp)
    return _finish(run, started)


class ResynthError(Exception):
    """The saved graph or report cannot be read back."""


def _saved_report(graph_path: Path) -> dict:
    report_path = report_path_for(graph_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ResynthError(f"plan report not found: {report_path}") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ResynthError(f"cannot read the plan report {report_path}: {exc}") from None
    if not isinstance(report, dict) or not report.get("facts"):
        raise ResynthError(f"{report_path} has no facts: only a planner v3 report that "
                           "built a graph from facts can be synthesized again")
    return report


def run_resynth(graph_path: Path, out: Path,
                ablate: frozenset[str] = frozenset()) -> PlanResult:
    """Build the graph again from the facts saved with ``graph_path``; no model is called.

    The facts come from ``<graph>.report.json``; the request, repository,
    base commit and final checks come from the graph. The repository is
    cloned at the base commit as in ``plan``. The facts are checked again
    (F1-F9, against the saved draft items) before the current rules
    synthesize them. Raises :class:`ResynthError` when the inputs cannot be
    read, and ``gitops.GitError`` / ``RuntimeError`` on git failures.
    """
    started = time.monotonic()
    try:
        old = load_graph(graph_path)
    except FileNotFoundError:
        raise ResynthError(f"graph file not found: {graph_path}") from None
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise ResynthError(f"cannot read the graph {graph_path}: {exc}") from None
    saved = _saved_report(graph_path)
    try:
        facts = Facts.model_validate(saved["facts"])
    except ValidationError as exc:
        raise ResynthError(f"the saved facts do not match the facts format: {exc}") from None
    items = [DraftItem.model_validate(item) for item in saved.get("draft_items") or []]
    repo = Path(old.repo).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    info = GraphInfo(request_id=old.request_id, request=old.request, repo=str(repo),
                     base_commit=old.base_commit, final_checks=list(old.final_checks),
                     model=old.generator.model)
    run = _V3Run(info, saved.get("request") or str(graph_path), repo, out,
                 frozenset(ablate), draft_items=[item.model_dump() for item in items],
                 estimate=saved.get("estimated_changed_lines"), facts=facts,
                 threshold=(saved.get("fast_path") or {}).get("threshold"))
    revised = old.generator.revision_mode == "llm"
    tmp = Path(tempfile.mkdtemp(prefix="tg-resynth-"))
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, old.base_commit)
        index = build_index(clone, old.base_commit)
        run.outline = build_outline(clone, index)
        run.tests = scan_test_imports(clone, index)
        run.errors = [issue for issue in check_facts(facts, items, index)
                      if items or issue.code != "F1"]
        if not run.errors:
            try:
                run.synthesis = synthesize(facts, index, info, run.tests, ablate=run.ablate,
                                           revised=revised)
                run.graph = run.synthesis.graph
            except FactsConflict as exc:
                run.errors = exc.issues
            except SynthesisError as exc:
                run.errors = [Issue("SYNTH", [], f"the program broke an invariant {exc}")]
    finally:
        remove_tree(tmp)
    return _finish(run, started, {"resynth_of": str(graph_path)})
