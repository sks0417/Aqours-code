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
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .affected import TestImports, scan_test_imports
from .cleanup import remove_tree
from .facts import Facts, check_facts, parse_facts
from .outline import Outline, build_outline
from .planner import (DRAFT_ECHO_CHARS, PLANNER_VERSION, REPORTED_GOAL_CHARS, DraftError,
                      DraftItem, DraftItems, GraphInfo, PlanOptions, PlanResult, _agent_dict,
                      _agent_failure, _file_list, _issue_dict, _totals, extract_json_block,
                      fast_path_graph, file_sizes, report_path_for, run_draft_stage)
from .repo_index import RepoIndex, build_index
from .schema import Graph, dump_graph
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


def run_plan_v3(options: PlanOptions, worker: Worker,
                draft_stage_worker: Worker | None = None) -> PlanResult:
    """Run planner v3; write the graph and the report."""
    started = time.monotonic()
    draft_stage_worker = draft_stage_worker or worker
    ablate = frozenset(options.ablate)
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
    synthesis: Synthesis | None = None
    graph_round: int | None = None
    facts: Facts | None = None
    errors: list[Issue] = []
    fast_path = False
    outline = Outline("")
    tests = TestImports()
    tmp = Path(tempfile.mkdtemp(prefix="tg-plan-"))
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, base_commit)
        index = build_index(clone, base_commit)
        outline = build_outline(clone, index)
        tests = scan_test_imports(clone, index)
        draft_items, stage1, errors = run_draft_stage(
            request, file_sizes(clone, index), draft_stage_worker, clone,
            log_dir / "draft", options.timeout_s)
        if draft_items is not None:
            estimate = draft_items.estimated_changed_lines
            fast_path = threshold is not None and estimate < threshold
        if fast_path:
            graph = fast_path_graph(info, repo, estimate, threshold, PLANNER_VERSION)
            errors = list(validate(graph, index).errors)
        elif draft_items is not None:
            revision: FactsRevision | None = None
            for number in range(options.max_revisions + 1):
                prompt = build_facts_prompt(request, index, draft_items.items, outline.text,
                                            revision)
                result = worker.run(WorkerRequest(
                    node_id="planner", attempt=number + 1, prompt=prompt, workspace=clone,
                    log_dir=log_dir, timeout_s=options.timeout_s))
                evaluation = evaluate_facts(result.final_answer, info, index,
                                            draft_items.items, tests, ablate,
                                            revised=number > 0)
                errors = list(evaluation.errors)
                if not result.ok:
                    errors = [_agent_failure(result), *errors]
                if evaluation.facts is not None:
                    facts = evaluation.facts
                if evaluation.synthesis is not None and result.ok:
                    synthesis, graph, graph_round = (evaluation.synthesis,
                                                     evaluation.synthesis.graph, number + 1)
                rounds.append(_round_record(number + 1, result.final_answer, evaluation,
                                            errors, _agent_dict(result)))
                if not errors or evaluation.program_error:
                    break
                revision = FactsRevision(number + 1, options.max_revisions,
                                         evaluation.facts_text, errors)
    finally:
        remove_tree(tmp)
    success = not errors and graph is not None
    if graph is not None:
        dump_graph(graph, out)
    else:
        out.unlink(missing_ok=True)  # never leave a stale graph from an earlier run
    stage_totals = {"stage1": _totals(stage1), "stage2": _totals(rounds)}
    goal_chars = {node.id: len(node.goal) for node in graph.nodes} if graph is not None else {}
    decisions = [decision.to_dict() for decision in synthesis.decisions] if synthesis else []
    report = {
        "planner": "v3",
        "planner_version": PLANNER_VERSION,
        "model": info.model,
        "request": str(options.request_path),
        "repo": str(repo),
        "base_commit": base_commit,
        "out": str(out),
        "success": success,
        "errors": [_issue_dict(issue) for issue in errors],
        "ablate": sorted(ablate),
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
        "revision_reasons": [{"round": entry["round"],
                              "codes": list(dict.fromkeys(issue["code"]
                                                          for issue in entry["errors"]))}
                             for entry in rounds[:-1]] if len(rounds) > 1 else [],
        "graph_written": graph is not None,
        "graph_round": graph_round,
        "facts": facts.model_dump(mode="json") if facts is not None else None,
        "conventions": list(facts.conventions) if facts is not None else [],
        "units": len(facts.units) if facts is not None else 0,
        "node_units": synthesis.node_units if synthesis is not None and success else {},
        "decisions": decisions if success else [],
        "decision_counts": synthesis.counts() if synthesis is not None and success else {},
        "pre_node": synthesis.pre if synthesis is not None and success else None,
        "reg_node": synthesis.reg if synthesis is not None and success else None,
        "nodes": len(graph.nodes) if graph is not None else 0,
        "edges": len(graph.edges) if graph is not None else 0,
        "goal_chars": goal_chars,
        "long_goals": {node_id: chars for node_id, chars in goal_chars.items()
                       if chars > REPORTED_GOAL_CHARS},
        "outline_chars": outline.chars,
        "outline_reduction": outline.reduction,
        "outline_omitted_files": outline.omitted_files,
        "test_imports": {"files": len(tests.imports), "unresolved": tests.unresolved},
        "rounds": rounds,
        "totals": {key: stage_totals["stage1"][key] + stage_totals["stage2"][key]
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "stage_totals": stage_totals,
        "duration_s": round(time.monotonic() - started, 3),
    }
    report_path = report_path_for(out)
    write_json_atomic(report_path, report)
    return PlanResult(success=success, graph=graph, report=report, report_path=report_path)
