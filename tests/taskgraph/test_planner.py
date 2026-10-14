"""Planner v1 with scripted model answers: draft, grounding, revision, isolation, CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code.taskgraph import build_index, derive_edges, load_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.outline import build_outline
from aqours_code.taskgraph.planner import (
    DRAFT_PROMPT_PATH,
    PLANNER_V2_VERSION,
    PROMPT_PATH,
    Draft,
    DraftError,
    DraftItem,
    PlanOptions,
    build_draft_prompt,
    build_prompt,
    contract_checks,
    extract_json_block,
    file_sizes,
    implementation_checks,
    item_checks,
    item_revisions,
    parse_draft,
    parse_items,
    planner_checks,
    report_path_for,
    run_plan,
    unmerged_path_for,
)
from aqours_code.taskgraph.planner_entry import (
    DRAFT_TOOL_POLICY,
    PLANNER_TOOL_POLICY,
    run_planner,
)
from aqours_code.taskgraph.single import single_graph
from aqours_code.taskgraph.validate import validate
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest, WorkerResult
from taskgraph_support import commit_files, git, make_graph, make_node

REQUEST = "Add job cancellation to the toy queue.\n"


def _node(node_id: str, kind: str = "implement", **fields) -> dict:
    """A draft node; an implement node belongs to the draft item named like it."""
    link = ({"item": node_id} if kind == "implement"
            else {"reason": f"{node_id} is shared by several nodes"})
    return {"id": node_id, "title": f"title {node_id}", "kind": kind,
            "goal": f"goal {node_id}", "check": ["python -m pytest -q tests"],
            **link, **fields}


VALID_DRAFT = {"nodes": [
    _node("C", "contract", modify=["models.py", "store.py"],
          provides=["models.py::JobStatus.CANCELLED", "store.py::JobStore.cancel"],
          context_files=["README.md"]),
    _node("S", modify=["store.py"], create=["tests/test_cancel.py"],
          provides=["store.py::JobStore.cancel"],
          requires=["models.py::JobStatus.CANCELLED"]),
    _node("R", modify=["runner.py"], requires=["store.py::JobStore.cancel"],
          requires_impl=["store.py::JobStore.list_unfinished"]),
]}
# Well-formed, but V6: the symbol is neither in the repository nor provided.
INVALID_DRAFT = {"nodes": [
    *VALID_DRAFT["nodes"][:2],
    _node("R", modify=["runner.py"], requires=["store.py::JobStore.nope"]),
]}

# P1: C2 requires a symbol C1 provides, so the derived edge C1 -> C2 orders them.
SERIAL_CONTRACTS_DRAFT = {"nodes": [
    _node("C1", "contract", modify=["models.py"],
          provides=["models.py::JobStatus.CANCELLED"]),
    _node("C2", "contract", modify=["store.py"], provides=["store.py::JobStore.cancel"],
          requires=["models.py::JobStatus.CANCELLED"]),
    _node("R", modify=["runner.py"], create=["tests/test_runner_cancel.py"],
          requires=["store.py::JobStore.cancel"]),
]}
INDEPENDENT_CONTRACTS_DRAFT = {"nodes": [
    _node("C1", "contract", modify=["models.py"],
          provides=["models.py::JobStatus.CANCELLED"]),
    _node("C2", "contract", modify=["store.py"], provides=["store.py::JobStore.cancel"]),
    _node("R", modify=["runner.py"], create=["tests/test_runner_cancel.py"],
          requires=["store.py::JobStore.cancel", "models.py::JobStatus.CANCELLED"]),
]}
SINGLE_NODE_DRAFT = {"nodes": [
    _node("A", modify=["runner.py"], create=["tests/test_runner_cancel.py"]),
]}


def answer(draft: dict, preface: str = "Plan below.") -> str:
    return f"{preface}\n\n```json\n{json.dumps(draft, indent=2)}\n```\n"


# ── a scripted model client driving the real planner entry in process ──

def final(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10))


def tool_use(name: str, call_id: str, **arguments):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=call_id,
                                                    name=name, input=arguments)],
                           stop_reason="tool_use",
                           usage=SimpleNamespace(input_tokens=50, output_tokens=5))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tool_names: list[list[str]] = []
        self.messages = self

    def create(self, **kwargs):
        self.tool_names.append([tool["name"] for tool in kwargs.get("tools") or []])
        return self.responses.pop(0) if self.responses else final("done")


class ScriptedPlanner:
    """A Worker whose rounds run ``run_planner`` with scripted final answers."""

    stage = "ground"

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.requests: list[WorkerRequest] = []
        self.clients: list[FakeClient] = []

    def describe(self) -> dict:
        return {"worker": "scripted", "model": "scripted-model"}

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        config = AqoursWorker().config_for(request)
        config.update(model_provider="scripted", model="scripted-model")
        Path(config["log_dir"]).mkdir(parents=True, exist_ok=True)
        self.clients.append(FakeClient([final(self.answers.pop(0))]))
        return run_planner(config, model_client=self.clients[-1], stage=self.stage)


class ScriptedDrafter(ScriptedPlanner):
    """The worker of Step 1: same runner, the draft tool policy."""

    stage = "draft"


def items_answer(ids, estimate: int = 800) -> str:
    """A Step 1 answer with one item per id."""
    return answer({"estimated_changed_lines": estimate,
                   "items": [{"id": item_id, "title": f"item {item_id}",
                              "description": f"achieve {item_id}"} for item_id in ids]})


def implement_ids(answers: list[str]) -> list[str]:
    """Ids of the implement nodes of the first answer that holds a draft."""
    for text in answers:
        try:
            nodes = json.loads(extract_json_block(text))["nodes"]
        except (DraftError, ValueError, KeyError, TypeError):
            continue
        return [node["id"] for node in nodes if node.get("kind") == "implement"]
    return ["1"]


def plan(toy_repo, tmp_path: Path, answers: list[str], *, items=None, estimate: int = 800,
         **options):
    """Run both model steps with scripted answers.

    By default Step 1 lists one item per implement node of the first draft and
    estimates enough lines to stay off the fast path.
    """
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    worker = ScriptedPlanner(answers)
    drafter = ScriptedDrafter([items if isinstance(items, str)
                               else items_answer(items or implement_ids(answers), estimate)])
    plan_options = PlanOptions(request_path=request, repo=toy_repo.path, planner="v2",
                               out=tmp_path / "out" / "graph.json",
                               final_checks=["python -m pytest -q tests"], timeout_s=60,
                               **options)
    result = run_plan(plan_options, worker, drafter)
    worker.drafter = drafter
    return result, worker, plan_options


def patch_workers(monkeypatch, worker: ScriptedPlanner, drafter_answers: list[str]):
    """Make the ``plan`` command use scripted workers for both steps."""
    import aqours_code.taskgraph.planner as planner_module

    drafter = ScriptedDrafter(drafter_answers)
    monkeypatch.setattr(planner_module, "planner_worker", lambda: worker)
    monkeypatch.setattr(planner_module, "draft_worker", lambda: drafter)
    return drafter


# ── draft parsing ──

def test_extract_takes_the_last_json_block_among_text_and_other_blocks():
    first = {"nodes": [_node("OLD", modify=["runner.py"])]}
    text = ("Thinking first.\n```python\nprint('not json')\n```\n"
            f"An early draft:\n```json\n{json.dumps(first)}\n```\n"
            f"The final one:\n```JSON\n{json.dumps(VALID_DRAFT)}\n```\n"
            "A closing note.\n```text\nnot a draft\n```\n")
    assert json.loads(extract_json_block(text)) == VALID_DRAFT
    assert [node.id for node in parse_draft(text).nodes] == ["C", "S", "R"]


def test_extract_fallbacks_and_errors():
    assert json.loads(extract_json_block(json.dumps(VALID_DRAFT))) == VALID_DRAFT
    assert json.loads(extract_json_block(f"```\n{json.dumps(VALID_DRAFT)}\n```")) == VALID_DRAFT
    with pytest.raises(DraftError, match="no ```json code block"):
        extract_json_block("I could not decide.")
    with pytest.raises(DraftError, match="not valid JSON"):
        parse_draft("```json\n{\"nodes\": [\n```")


def test_draft_defaults_and_format_errors():
    draft = parse_draft(answer({"nodes": [{"id": "A", "title": "t", "kind": "implement",
                                           "goal": "g", "modify": ["runner.py"]}]}))
    assert isinstance(draft, Draft)
    assert (draft.nodes[0].create, draft.nodes[0].requires, draft.nodes[0].check) == ([], [], [])
    with pytest.raises(DraftError) as excinfo:
        parse_draft(answer({"nodes": [{"id": "A", "title": "t", "kind": "task", "goal": "g",
                                       "edges": []}]}))
    messages = [issue.format() for issue in excinfo.value.issues]
    assert all(message.startswith("[FORMAT]") for message in messages)
    assert any("nodes[0] (id 'A').kind" in message for message in messages)
    assert any("edges" in message for message in messages)


# ── run_plan ──

def test_valid_draft_gives_a_valid_planner_graph_with_derived_edges(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)])
    assert result.success and len(worker.requests) == 1
    graph = load_graph(options.out)
    assert graph.generator.kind == "planner"
    assert graph.generator.planner_version == PLANNER_V2_VERSION
    assert graph.generator.model == "scripted-model"
    assert graph.generator.revision_mode == "none"
    assert graph.request == REQUEST
    assert graph.base_commit == toy_repo.commit
    assert graph.final_checks == ["python -m pytest -q tests"]
    assert graph.nodes[0].check.timeout_s == 300
    assert graph.nodes[1].edit_set.create == ["tests/test_cancel.py"]

    index = build_index(toy_repo.path, toy_repo.commit)
    bare = graph.model_copy(update={"edges": [], "revision_log": []}, deep=True)
    derived, _ = derive_edges(bare, index)
    assert graph.edges == derived.edges and graph.edges
    assert {(edge.from_, edge.to) for edge in graph.edges} >= {("C", "S"), ("C", "R")}

    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["success"] is True and report["revision_rounds"] == 0
    assert report["rounds"][0]["errors"] == []
    assert report["rounds"][0]["draft"] == answer(VALID_DRAFT)
    assert report["totals"] == {"model_calls": 2, "input_tokens": 200, "output_tokens": 20}
    assert report["stage_totals"] == {
        "stage1": {"model_calls": 1, "input_tokens": 100, "output_tokens": 10},
        "stage2": {"model_calls": 1, "input_tokens": 100, "output_tokens": 10}}
    assert report["nodes"] == 3 and report["edges"] == len(graph.edges)
    assert report["nodes_before_merge"] == 3 and report["merges"] == []
    assert load_graph(unmerged_path_for(options.out)).nodes == graph.nodes


def test_one_revision_fixes_validation_errors(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path,
                                   [answer(INVALID_DRAFT), answer(VALID_DRAFT)])
    assert result.success
    report = result.report
    assert report["revision_rounds"] == 1 and len(report["rounds"]) == 2
    assert report["rounds"][0]["draft"] == answer(INVALID_DRAFT)
    assert [issue["code"] for issue in report["rounds"][0]["errors"]] == ["V6"]
    assert report["rounds"][1]["errors"] == []
    assert report["totals"]["model_calls"] == 3
    assert report["stage_totals"]["stage2"]["model_calls"] == 2
    assert load_graph(options.out).generator.revision_mode == "llm"

    second_prompt = worker.requests[1].prompt
    assert "store.py::JobStore.nope" in second_prompt  # the previous draft
    assert "[V6] R: requires store.py::JobStore.nope" in second_prompt
    assert "Fix your previous draft" not in worker.requests[0].prompt


def test_persistent_errors_fail_after_two_revisions_but_still_write_outputs(
        toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    worker = ScriptedPlanner([answer(INVALID_DRAFT), "no draft at all", answer(INVALID_DRAFT)])
    patch_workers(monkeypatch, worker, [items_answer(["S", "R"])])
    code = main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out), "--final-check", "python -m pytest -q tests",
                 "--timeout", "60"])
    assert code != 0
    assert len(worker.requests) == 3
    graph = load_graph(out)
    assert graph.generator.revision_mode == "llm"
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    assert report["success"] is False and report["revision_rounds"] == 2
    assert [[issue["code"] for issue in r["errors"]] for r in report["rounds"]] == [
        ["V6"], ["FORMAT"], ["V6"]]
    assert "no draft at all" in worker.requests[2].prompt or \
        "store.py::JobStore.nope" in worker.requests[2].prompt
    output = capsys.readouterr().out
    assert "revision rounds: 2" in output and "success: no" in output
    assert "model calls: 4" in output
    assert "[V6] R:" in output and not unmerged_path_for(out).exists()


# ── planner-only checks P1-P3 ──

def _round_codes(report: dict) -> list[list[str]]:
    return [[issue["code"] for issue in r["errors"]] for r in report["rounds"]]


def test_p1_serial_contracts_are_an_error(toy_repo, tmp_path):
    result, _, _ = plan(toy_repo, tmp_path, [answer(SERIAL_CONTRACTS_DRAFT)] * 3)
    assert not result.success
    assert _round_codes(result.report) == [["P1"]] * 3
    issue = result.report["rounds"][0]["errors"][0]
    assert issue["nodes"] == ["C1", "C2"]
    assert "merge C1 and C2 into one contract" in issue["message"]


def test_p1_independent_contracts_are_fine(toy_repo, tmp_path):
    result, _, _ = plan(toy_repo, tmp_path, [answer(INDEPENDENT_CONTRACTS_DRAFT)])
    assert result.success and _round_codes(result.report) == [[]]


def test_p2_test_only_node_is_an_error(toy_repo, tmp_path):
    draft = {"nodes": [*VALID_DRAFT["nodes"], _node("T", create=["tests/test_x.py"])]}
    result, _, _ = plan(toy_repo, tmp_path, [answer(draft)] * 3)
    assert not result.success
    errors = result.report["rounds"][0]["errors"]
    assert [(issue["code"], issue["nodes"]) for issue in errors] == [("P2", ["T"])]
    assert "tests/test_x.py" in errors[0]["message"]
    assert "Remove this node" in errors[0]["message"]


def test_p3_contract_writing_tests_is_an_error(toy_repo, tmp_path):
    contract = {**VALID_DRAFT["nodes"][0], "create": ["tests/test_contract.py"]}
    draft = {"nodes": [contract, *VALID_DRAFT["nodes"][1:]]}
    result, _, _ = plan(toy_repo, tmp_path, [answer(draft)] * 3)
    assert not result.success
    errors = result.report["rounds"][0]["errors"]
    assert [(issue["code"], issue["nodes"]) for issue in errors] == [("P3", ["C"])]
    assert "tests/test_contract.py" in errors[0]["message"]


def test_revision_fixes_a_p1_error(toy_repo, tmp_path):
    result, worker, options = plan(
        toy_repo, tmp_path,
        [answer(SERIAL_CONTRACTS_DRAFT), answer(INDEPENDENT_CONTRACTS_DRAFT)])
    assert result.success
    assert result.report["revision_rounds"] == 1
    assert _round_codes(result.report) == [["P1"], []]
    assert "[P1] C1, C2: contract C1 comes before contract C2" in worker.requests[1].prompt
    assert load_graph(options.out).generator.revision_mode == "llm"


def test_single_node_draft_succeeds(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path, [answer(SINGLE_NODE_DRAFT)])
    assert result.success and len(worker.requests) == 1
    graph = load_graph(options.out)
    assert [node.id for node in graph.nodes] == ["A"] and graph.edges == []
    assert result.report["nodes"] == 1 and result.report["edges"] == 0


def test_planner_checks_stay_out_of_validate():
    # A hand-written-style graph that breaks P1, P2 and P3 at once.
    graph = make_graph([
        make_node("C1", kind="contract", modify=("models.py",)),
        make_node("C2", kind="contract", modify=("store.py",), create=("tests/test_c.py",)),
        make_node("T", create=("tests/test_all.py",)),
    ], [{"from": "C1", "to": "C2", "type": "interface", "source": "manual",
         "reason": "test"}])
    assert sorted(issue.code for issue in planner_checks(graph)) == ["P1", "P2", "P3"]
    report = validate(graph)
    assert not {"P1", "P2", "P3"} & set(report.codes() + report.warning_codes())


def test_agent_failure_is_a_round_error(toy_repo, tmp_path):
    class Failing(ScriptedPlanner):
        def run(self, request):
            self.requests.append(request)
            return WorkerResult(ok=False, reason="worker_timeout", error="killed")

    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    options = PlanOptions(request_path=request, repo=toy_repo.path, planner="v2",
                          out=tmp_path / "graph.json", max_revisions=0)
    result = run_plan(options, Failing([]), ScriptedDrafter([items_answer(["1"])]))
    assert not result.success and result.graph is None
    assert not options.out.exists()
    codes = [issue["code"] for issue in result.report["rounds"][0]["errors"]]
    assert codes == ["AGENT", "FORMAT"]


def _snapshot(repo: Path) -> dict:
    files = {}
    for path in sorted(repo.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(repo).parts:
            files[path.relative_to(repo).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return {
        "files": files,
        "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
        "refs": git(repo, "for-each-ref"),
        "head": git(repo, "rev-parse", "HEAD"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "config": (repo / ".git" / "config").read_text(encoding="utf-8"),
    }


def test_plan_leaves_the_original_repository_untouched(toy_repo, tmp_path):
    commit_files(toy_repo.path, {"extra.py": "X = 1\n"}, "second commit")
    (toy_repo.path / "runner.py").write_text("# uncommitted\n", encoding="utf-8")
    (toy_repo.path / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    before = _snapshot(toy_repo.path)
    result, worker, _ = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)])
    assert result.success
    assert _snapshot(toy_repo.path) == before
    workspace = worker.requests[0].workspace
    assert workspace.resolve() != toy_repo.path.resolve()
    assert not workspace.exists()  # the clone is removed afterwards
    assert result.graph.base_commit == before["head"]


# ── tool policy ──

def test_planner_tool_policy_is_read_only():
    allowed = PLANNER_TOOL_POLICY["allowed_tools"]
    for tool in ("bash", "write_file", "edit_file"):
        assert tool not in allowed
    assert {"read_file", "glob"} <= set(allowed)
    for key in ("allow_mcp", "allow_memory_context", "allow_skill_context",
                "allow_teammate_context", "background_tasks"):
        assert PLANNER_TOOL_POLICY[key] is False


def test_planner_agent_is_offered_no_write_tools_and_cannot_write(toy_repo, tmp_path):
    log_dir = tmp_path / "log"
    request = WorkerRequest(node_id="planner", attempt=1, prompt="plan", timeout_s=60,
                            workspace=toy_repo.path, log_dir=log_dir)
    config = AqoursWorker().config_for(request)
    config.update(model_provider="scripted", model="scripted-model")
    log_dir.mkdir()
    client = FakeClient([tool_use("write_file", "call_1", path="hello.txt", content="hi"),
                         tool_use("read_file", "call_2", path="README.md"),
                         final(answer(VALID_DRAFT))])
    result = run_planner(config, model_client=client)
    assert result.final_answer == answer(VALID_DRAFT)
    assert not (toy_repo.path / "hello.txt").exists()
    offered = set(client.tool_names[0])
    assert "read_file" in offered and "glob" in offered
    assert not offered & {"bash", "write_file", "edit_file"}


# ── prompt ──

def test_prompt_contains_request_files_and_revision(toy_index):
    from aqours_code.taskgraph.planner import Revision
    from aqours_code.taskgraph.validate import Issue

    first = build_prompt(REQUEST, toy_index, ["python -m pytest -q tests"])
    assert first.startswith(PROMPT_PATH.read_text(encoding="utf-8").rstrip())
    assert REQUEST.strip() in first and "- store.py" in first
    assert "- `python -m pytest -q tests`" in first
    revised = build_prompt(REQUEST, toy_index, [], Revision(
        number=1, draft='{"nodes": []}', errors=[Issue("V5", ["A", "B"], "both edit x")]))
    assert '{"nodes": []}' in revised and "[V5] A, B: both edit x" in revised


@pytest.mark.parametrize("prompt_path", [PROMPT_PATH, DRAFT_PROMPT_PATH])
def test_planner_prompts_mention_nothing_of_the_experiment_tasks(prompt_path):
    text = prompt_path.read_text(encoding="utf-8").lower()
    # "dashboard" and "runner" appear only in the generic paragraph about nodes that
    # expose other features ("dashboard pages", "a fake runner"), which is checked below.
    text = text.replace("dashboard pages", "").replace("a fake runner", "")
    for word in ("job", "runner", "tenant", "webhook", "recurring", "rate limit",
                 "ratelimit", "audit", "notification", "priorit", "dashboard",
                 "cancel", "retry", "retries", "scheduler"):
        assert word not in text, word


# ── repository conventions ──

CONVENTIONS = ["The current time comes only from the injected clock: functions that need "
               "it take a `now` argument.",
               "All storage access goes through JobStore."]


def test_conventions_are_appended_to_every_goal(toy_repo, tmp_path):
    draft = {"conventions": CONVENTIONS, **VALID_DRAFT}
    result, _, options = plan(toy_repo, tmp_path, [answer(draft)])
    assert result.success
    graph = load_graph(options.out)
    for node in graph.nodes:
        original = f"goal {node.id}"
        assert node.goal.startswith(original + "\n\nRepository conventions:\n")
        section = node.goal[node.goal.index("Repository conventions:"):]
        assert section.splitlines()[1:] == [f"- {item}" for item in CONVENTIONS]
    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["conventions"] == CONVENTIONS
    assert json.loads(report["draft"]) == draft
    assert report["rounds"][0]["conventions"] == CONVENTIONS
    assert json.loads(report["rounds"][0]["draft_json"]) == draft


def test_without_conventions_goals_are_unchanged(toy_repo, tmp_path):
    for draft in (VALID_DRAFT, {"conventions": [], **VALID_DRAFT}):
        out_dir = tmp_path / ("old" if "conventions" not in draft else "empty")
        out_dir.mkdir()
        result, _, options = plan(toy_repo, out_dir, [answer(draft)])
        assert result.success
        goals = {node.id: node.goal for node in load_graph(options.out).nodes}
        assert goals == {"C": "goal C", "S": "goal S", "R": "goal R"}
        report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
        assert report["conventions"] == []


def test_old_drafts_without_conventions_still_parse():
    draft = parse_draft(answer(VALID_DRAFT))
    assert draft.conventions == [] and [node.id for node in draft.nodes] == ["C", "S", "R"]
    with pytest.raises(DraftError):
        parse_draft(answer({"conventions": "not a list", **VALID_DRAFT}))


def test_prompt_asks_for_conventions_and_no_integration_node():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "**Repository conventions.**" in text and '"conventions"' in text
    assert "No test-only nodes and no integration node" in text
    rule = text[text.index("6. **No test-only"):text.index("7. **Every node tests itself")]
    assert "requires_impl" not in rule and "real implementation" not in rule
    assert "`now` argument" in text


GOAL_RULE = (
    "`goal`: the worker also receives the whole request, so do not repeat it. Name the "
    "part of the request this node implements (quote its heading or item title), then add "
    "only what the request does not say: where in the code the change goes, which "
    "existing functions or classes to use, and decisions this node shares with other "
    "nodes. Keep it under 800 characters.")
READING_RULE = (
    "The outline below lists every definition in the source files with its\n"
    "signature, line numbers and the first line of its docstring. Plan from the\n"
    "outline. Read a file only for a detail the outline does not show, such as\n"
    "how a function works inside, and read only the lines you need. Do not read\n"
    "whole source files to find out what they contain, and do not read test\n"
    "files: their names in the file list are enough to choose where new tests go.\n")


def test_prompt_examples_have_short_goals_that_name_the_part_of_the_request():
    import re
    text = PROMPT_PATH.read_text(encoding='utf-8')
    examples = [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', text, re.S)]
    assert len(examples) == 3
    for example in examples:
        for node in example['nodes']:
            assert len(node['goal']) <= 600, node['id']
    titles = ('"Star ratings"', '"Shopping-list export"', '"Scaled printout"')
    for example in examples[1:]:
        for node in example['nodes']:
            assert any(ref.startswith('SPEC.md#') for ref in node['context_files'])
            assert 'raise ValueError' not in node['goal']
            assert 'int | None = None' not in node['goal']
            assert any(title in node['goal'] for title in titles), node['id']  # quotes its item
            if node['kind'] == 'implement':
                assert node['goal'].startswith(titles) and len(node['goal']) < 200
    rule = text[text.index('8. **'):text.index('## Output format')]
    assert GOAL_RULE in " ".join(rule.split())
    assert 'SPEC.md#Heading' in rule and 'A specification file is not repeated either' in rule
    # the earlier wording is gone
    assert '600 characters' not in text
    assert 'Do not restate specification details' not in text
    assert 'State what feature this' not in " ".join(text.split())


def test_prompt_tells_the_planner_to_plan_from_the_outline():
    text = PROMPT_PATH.read_text(encoding='utf-8')
    assert READING_RULE in text
    head = text[:text.index("## Concepts")]
    assert READING_RULE in head
    # partial reads and several tool calls in one turn, as the runtime supports them
    assert ("`read_file` reads part of a file with `offset` (how many lines to skip) and\n"
            "`limit` (how many lines to return): for lines 120 to 160, pass `offset` 119\n"
            "and `limit` 41. When you need several files, ask for all of them in one turn.\n"
            ) in head
    assert "You have read-only tools (read_file, glob)." in head
    # the earlier instruction to read the modules and the tests is gone
    flat = " ".join(text.split())
    for old in ("Read before you decide", "Read the code and turn every draft item",
                "the modules the request touches, and the existing tests",
                "After reading the code", "Reading the code shows"):
        assert old not in flat, old
    assert "After looking at the code, split an item into several nodes only if" in flat


def test_planner_agent_can_read_part_of_a_file_and_several_files_in_one_turn(toy_repo,
                                                                            tmp_path):
    """What the prompt says about ``read_file`` holds in the runtime."""
    (toy_repo.path / "numbers.txt").write_text(
        "".join(f"line {number}\n" for number in range(1, 201)), encoding="utf-8")

    class RecordingClient(FakeClient):
        def __init__(self, responses):
            super().__init__(responses)
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append({"tools": kwargs.get("tools") or [],
                               "messages": json.dumps(kwargs.get("messages"), default=str)})
            return super().create(**kwargs)

    two_reads = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="call_a", name="read_file",
                                 input={"path": "numbers.txt", "offset": 119, "limit": 41}),
                 SimpleNamespace(type="tool_use", id="call_b", name="read_file",
                                 input={"path": "README.md"})],
        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=50, output_tokens=5))
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    config = AqoursWorker().config_for(WorkerRequest(
        node_id="planner", attempt=1, prompt="plan", timeout_s=60,
        workspace=toy_repo.path, log_dir=log_dir))
    config.update(model_provider="scripted", model="scripted-model")
    client = RecordingClient([two_reads, final(answer(VALID_DRAFT))])
    result = run_planner(config, model_client=client)
    assert result.ok and result.model_calls == 2  # both reads were answered in one turn

    schema = next(tool for tool in client.calls[0]["tools"] if tool["name"] == "read_file")
    assert {"path", "offset", "limit"} <= set(schema["input_schema"]["properties"])
    after = client.calls[1]["messages"]
    assert "call_a" in after and "call_b" in after
    assert "line 120" in after and "line 160" in after   # offset 119, limit 41: lines 120-160
    assert "line 119\\n" not in after and "line 161" not in after
    assert "# Toy job queue" in after                     # the second read, same turn


@pytest.mark.parametrize('length', [1500, 1501])
def test_long_goal_is_reported_without_failing_plan(toy_repo, tmp_path, monkeypatch, capsys, length):
    request = tmp_path / 'request.md'
    request.write_text(REQUEST, encoding='utf-8')
    draft = {'nodes': [_node('A', goal='x' * length, modify=['runner.py'])]}
    worker = ScriptedPlanner([answer(draft)])
    patch_workers(monkeypatch, worker, [items_answer(['A'])])
    out = tmp_path / 'graph.json'
    assert main(['plan', '--planner', 'v2', str(request), '--repo', str(toy_repo.path),
                 '--out', str(out)]) == 0
    report = json.loads(report_path_for(out).read_text(encoding='utf-8'))
    assert report['success'] and report['goal_chars'] == {'A': length}
    assert report['rounds'][0]['errors'] == []
    assert load_graph(out).nodes[0].goal == 'x' * length
    terminal = capsys.readouterr().out
    note = ('note: node A goal has 1501 characters (over 1500): goal repeats the request; '
            'the worker already receives it')
    assert (note in terminal) == (length > 1500)
    assert ('goal has' in terminal) == (length > 1500)
    assert 'SPEC.md#heading' not in terminal
    assert report['long_goals'] == ({'A': 1501} if length > 1500 else {})


def test_goal_character_report_includes_appended_conventions(toy_repo, tmp_path):
    draft = {'conventions': ['Use the injected clock.'], 'nodes': SINGLE_NODE_DRAFT['nodes']}
    result, _, _ = plan(toy_repo, tmp_path, [answer(draft)])
    assert result.success
    assert result.report['goal_chars'] == {n.id: len(n.goal) for n in result.graph.nodes}
    assert result.report['goal_chars']['A'] > len(draft['nodes'][0]['goal'])


# ── Step 1: draft items ──

def test_items_parse_and_format_errors():
    items = parse_items(answer({"estimated_changed_lines": 400, "notes": "ignored",
                                "items": [{"id": 1, "title": "First", "description": "d"},
                                          {"id": "2", "title": "Second"}]}))
    assert items.estimated_changed_lines == 400
    assert [(item.id, item.title, item.description) for item in items.items] == [
        ("1", "First", "d"), ("2", "Second", "")]
    one = [{"id": "1", "title": "t"}]
    for bad in ({"items": one},                                      # no estimate
                {"estimated_changed_lines": -1, "items": one},
                {"estimated_changed_lines": 12.5, "items": one},
                {"estimated_changed_lines": True, "items": one},
                {"estimated_changed_lines": "400", "items": one},
                {"estimated_changed_lines": 400, "items": []},
                {"estimated_changed_lines": 400, "items": [*one, *one]},
                {"estimated_changed_lines": 400, "items": [{"id": "1"}]}):
        with pytest.raises(DraftError) as excinfo:
            parse_items(answer(bad))
        assert all(issue.code == "FORMAT" for issue in excinfo.value.issues), bad
    with pytest.raises(DraftError, match="no ```json code block"):
        parse_items("three things, about 400 lines")


def test_draft_stage_sees_file_sizes_but_no_content_and_no_file_tools(toy_repo, tmp_path):
    result, worker, _ = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)])
    assert result.success
    drafter = worker.drafter
    assert len(drafter.requests) == 1 and drafter.requests[0].node_id == "planner-draft"
    prompt = drafter.requests[0].prompt
    assert prompt.startswith(DRAFT_PROMPT_PATH.read_text(encoding="utf-8").rstrip())
    assert REQUEST.strip() in prompt
    index = build_index(toy_repo.path, toy_repo.commit)
    for path, lines in file_sizes(toy_repo.path, index):
        assert f"- {path}: {lines} lines" in prompt
    assert "- runner.py: " in prompt
    for content in ("def run_loop", "class JobStore", "MAX_ATTEMPTS = 3"):
        assert content not in prompt
    assert DRAFT_TOOL_POLICY["allowed_tools"] == ["compact"]
    assert drafter.clients[0].tool_names[0] == ["compact"]
    assert set(worker.clients[0].tool_names[0]) >= {"read_file", "glob"}
    assert drafter.requests[0].log_dir != worker.requests[0].log_dir


def test_draft_prompt_repeats_the_errors_on_a_retry():
    from aqours_code.taskgraph.validate import Issue

    first = build_draft_prompt(REQUEST, [("a.py", 10), ("logo.png", None)])
    assert "- a.py: 10 lines" in first and "- logo.png: binary" in first
    assert "Fix your previous answer" not in first
    again = build_draft_prompt(REQUEST, [("a.py", 10)], "my bad answer",
                               [Issue("FORMAT", [], "estimated_changed_lines: required")])
    assert "Fix your previous answer" in again and "my bad answer" in again
    assert "[FORMAT] estimated_changed_lines: required" in again


def test_draft_stage_is_retried_once_after_a_format_error(toy_repo, tmp_path):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    worker = ScriptedPlanner([answer(VALID_DRAFT)])
    drafter = ScriptedDrafter([answer({"items": [{"id": "S", "title": "s"}]}),
                               items_answer(["S", "R"])])
    options = PlanOptions(request_path=request, repo=toy_repo.path, planner="v2",
                          out=tmp_path / "graph.json")
    result = run_plan(options, worker, drafter)
    assert result.success and len(drafter.requests) == 2 and len(worker.requests) == 1
    stage1 = result.report["stage1"]
    assert [[issue["code"] for issue in r["errors"]] for r in stage1] == [["FORMAT"], []]
    assert "estimated_changed_lines" in stage1[0]["errors"][0]["message"]
    assert stage1[0]["agent"]["model_calls"] == 1 and stage1[1]["round"] == 2
    assert "Fix your previous answer" in drafter.requests[1].prompt
    assert "estimated_changed_lines" in drafter.requests[1].prompt
    assert result.report["stage_totals"]["stage1"]["model_calls"] == 2
    assert result.report["totals"]["model_calls"] == 3


def test_plan_fails_when_the_draft_stage_fails_twice(toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    out.write_text("stale", encoding="utf-8")
    worker = ScriptedPlanner([answer(VALID_DRAFT)])
    drafter = patch_workers(monkeypatch, worker, ["no json", answer({"items": []})])
    code = main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out)])
    assert code == 1
    assert len(drafter.requests) == 2 and worker.requests == []
    assert not out.exists() and not unmerged_path_for(out).exists()
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    assert report["success"] is False and report["graph_written"] is False
    assert report["draft_items"] == [] and report["estimated_changed_lines"] is None
    assert report["fast_path"] == {"taken": False, "estimated_changed_lines": None,
                                   "threshold": 500}
    assert len(report["stage1"]) == 2 and report["rounds"] == []
    assert {issue["code"] for issue in report["errors"]} == {"FORMAT"}
    output = capsys.readouterr().out
    assert "draft items: 0" in output and "success: no" in output and "[FORMAT]" in output


# ── fast path ──

def test_small_estimate_takes_the_fast_path(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)], estimate=300)
    assert result.success
    assert worker.requests == []                       # Step 2 never ran
    assert len(worker.drafter.requests) == 1
    graph = load_graph(options.out)
    single = single_graph(REQUEST, toy_repo.path, ["python -m pytest -q tests"])
    ours = graph.model_dump(mode="json", by_alias=True)
    theirs = single.model_dump(mode="json", by_alias=True)
    for key in ("generator", "revision_log"):
        ours.pop(key)
        theirs.pop(key)
    assert ours == theirs
    assert graph.nodes[0].edit_set.any_file and graph.nodes[0].context_files == []
    assert graph.generator.model_dump() == {
        "kind": "planner", "planner_version": "planner-v2", "model": "scripted-model",
        "revision_mode": "none"}
    assert [(e.action, e.reason) for e in graph.revision_log] == [
        ("other", "fast path: estimated 300 changed lines < threshold 500")]
    assert validate(graph, build_index(toy_repo.path, toy_repo.commit)).ok
    report = result.report
    assert report["fast_path"] == {"taken": True, "estimated_changed_lines": 300,
                                   "threshold": 500}
    assert report["nodes"] == 1 and report["rounds"] == [] and report["merges"] == []
    assert report["totals"]["model_calls"] == 1
    assert not unmerged_path_for(options.out).exists()


@pytest.mark.parametrize(("estimate", "options", "taken", "threshold"), [
    (800, {}, False, 500),
    (500, {}, False, 500),                       # the estimate must be below the threshold
    (499, {}, True, 500),
    (300, {"fast_path_lines": None}, False, None),
    (800, {"fast_path_lines": 1000}, True, 1000),
])
def test_fast_path_threshold(toy_repo, tmp_path, estimate, options, taken, threshold):
    result, worker, _ = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)], estimate=estimate,
                             **options)
    assert result.success
    assert result.report["fast_path"] == {"taken": taken, "estimated_changed_lines": estimate,
                                          "threshold": threshold}
    assert len(worker.requests) == (0 if taken else 1)
    assert len(result.graph.nodes) == (1 if taken else 3)


@pytest.mark.parametrize(("flags", "estimate", "line", "nodes"), [
    ([], 300, "fast path: taken (300 < 500 lines)", 1),
    (["--no-fast-path"], 300, "fast path: off", 3),
    (["--fast-path-lines", "1000"], 800, "fast path: taken (800 < 1000 lines)", 1),
    (["--fast-path-lines", "200"], 300, "fast path: not taken (300 >= 200 lines)", 3),
])
def test_fast_path_options_of_the_plan_command(toy_repo, tmp_path, monkeypatch, capsys,
                                               flags, estimate, line, nodes):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    worker = ScriptedPlanner([answer(VALID_DRAFT)])
    patch_workers(monkeypatch, worker, [items_answer(["S", "R"], estimate)])
    assert main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out), "--final-check", "python -m pytest -q tests",
                 *flags]) == 0
    output = capsys.readouterr().out
    assert line in output and f"estimated changed lines: {estimate}" in output
    assert "draft items: 2" in output
    assert len(load_graph(out).nodes) == nodes
    assert ("nodes before merge" in output) == (nodes == 3)


# ── Step 2: grounding the draft items ──

def test_grounding_prompt_lists_the_draft_items_also_when_revising(toy_repo, tmp_path):
    result, worker, _ = plan(toy_repo, tmp_path,
                             [answer(INVALID_DRAFT), answer(VALID_DRAFT)])
    assert result.success and len(worker.requests) == 2
    for request in worker.requests:
        section = request.prompt[request.prompt.index("# Draft items"):]
        assert "- S: **item S**. achieve S" in section
        assert "- R: **item R**. achieve R" in section
    assert "planner checks (P1-P7)" in worker.requests[1].prompt
    first = worker.requests[0].prompt
    assert first.index("# The request") < first.index("# Draft items") \
        < first.index("# Repository")


ITEMS = [DraftItem(id="1", title="First"), DraftItem(id="2", title="Second")]


def _draft(*nodes: dict) -> Draft:
    return Draft.model_validate({"nodes": list(nodes)})


def _codes(issues) -> list[tuple[str, list[str]]]:
    return [(issue.code, issue.nodes) for issue in issues]


def test_p4_every_item_and_every_implement_node_are_linked():
    good = _draft(_node("A", item="1", modify=["a.py"]), _node("B", item=2, modify=["b.py"]))
    assert item_checks(good, ITEMS) == []
    missing_item = _draft(_node("A", item="1", modify=["a.py"]))
    assert _codes(item_checks(missing_item, ITEMS)) == [("P4", [])]
    assert "draft item 2 (Second) has no implement node" in \
        item_checks(missing_item, ITEMS)[0].message
    no_item = _draft(_node("A", item="1", modify=["a.py"]),
                     _node("B", item="2", modify=["b.py"]),
                     {**_node("X", modify=["x.py"]), "item": None})
    assert _codes(item_checks(no_item, ITEMS)) == [("P4", ["X"])]
    unknown = _draft(_node("A", item="1", modify=["a.py"]),
                     _node("B", item="2", modify=["b.py"]),
                     _node("X", item="9", modify=["x.py"]))
    assert _codes(item_checks(unknown, ITEMS)) == [("P4", ["X"])]
    assert "is not a draft item" in item_checks(unknown, ITEMS)[0].message
    contract_item = _draft(_node("A", item="1", modify=["a.py"]),
                           _node("B", item="2", modify=["b.py"]),
                           _node("C", "contract", item="1", modify=["c.py"]))
    assert _codes(item_checks(contract_item, ITEMS)) == [("P4", ["C"])]


def test_p5_a_split_and_a_contract_need_a_reason():
    split = _draft(_node("A1", item="1", modify=["a.py"]),
                   _node("A2", item="1", modify=["a2.py"]),
                   _node("B", item="2", modify=["b.py"]))
    assert _codes(item_checks(split, ITEMS)) == [("P5", ["A1", "A2"])]
    explained = _draft(_node("A1", item="1", modify=["a.py"]),
                       _node("A2", item="1", modify=["a2.py"],
                             reason="the exporter lives in its own module"),
                       _node("B", item="2", modify=["b.py"]))
    assert item_checks(explained, ITEMS) == []
    contract = _draft(_node("A", item="1", modify=["a.py"]),
                      _node("B", item="2", modify=["b.py"]),
                      _node("C", "contract", modify=["c.py"], reason="  "))
    assert _codes(item_checks(contract, ITEMS)) == [("P5", ["C"])]
    entries = item_revisions(explained, ITEMS)
    assert [(e.action, e.nodes) for e in entries] == [("split", ["A1", "A2"])]
    assert entries[0].reason == ("draft item 1 (First) was split: the exporter lives in "
                                 "its own module")


def test_p4_and_p5_are_reported_and_can_be_revised(toy_repo, tmp_path):
    nodes = VALID_DRAFT["nodes"]
    broken = {"nodes": [{**nodes[0], "reason": None}, {**nodes[1], "item": None}, nodes[2]]}
    result, worker, options = plan(toy_repo, tmp_path, [answer(broken), answer(VALID_DRAFT)],
                                   items=["S", "R"])
    assert result.success
    first = result.report["rounds"][0]["errors"]
    assert sorted((issue["code"], issue["nodes"]) for issue in first) == [
        ("P4", []), ("P4", ["S"]), ("P5", ["C"])]
    assert "[P4] S: S has no `item`" in worker.requests[1].prompt
    assert "[P5] C: contract C has no `reason`" in worker.requests[1].prompt
    assert load_graph(options.out).generator.revision_mode == "llm"


def test_split_and_contract_are_recorded_first_in_the_revision_log(toy_repo, tmp_path):
    draft = {"nodes": [
        _node("C", "contract", modify=["models.py"],
              provides=["models.py::JobStatus.CANCELLED"],
              reason="S1 and R both use the new status"),
        _node("S1", item="1", modify=["store.py"], create=["tests/test_s1.py"],
              requires=["models.py::JobStatus.CANCELLED"],
              reason="the store part is independent of the README"),
        _node("S2", item="1", modify=["README.md"], create=["tests/test_s2.py"]),
        _node("R", item="2", modify=["runner.py"], create=["tests/test_r.py"],
              requires=["models.py::JobStatus.CANCELLED"]),
    ]}
    result, _, options = plan(toy_repo, tmp_path, [answer(draft)], items=["1", "2"])
    assert result.success, result.report["errors"]
    log = load_graph(options.out).revision_log
    assert [(e.action, e.nodes) for e in log[:2]] == [("split", ["S1", "S2"]),
                                                      ("add_node", ["C"])]
    assert log[0].reason == ("draft item 1 (item 1) was split: the store part is "
                             "independent of the README")
    assert log[1].reason == "contract added: S1 and R both use the new status"
    assert all(entry.action == "add_edge" for entry in log[2:])
    assert result.report["node_items"] == {
        "C": {"item": None, "reason": "S1 and R both use the new status"},
        "S1": {"item": "1", "reason": "the store part is independent of the README"},
        "S2": {"item": "1", "reason": None},
        "R": {"item": "2", "reason": None}}


# ── the whole flow, with Step 3 ──

CONVENTION = "Statuses are members of JobStatus."
# P and Q both edit runner.py, so they can only queue; S is independent.
QUEUED_DRAFT = {"conventions": [CONVENTION], "nodes": [
    _node("C", "contract", modify=["models.py"], provides=["models.py::JobStatus.CANCELLED"]),
    _node("P", modify=["runner.py"], create=["tests/test_p.py"],
          requires=["models.py::JobStatus.CANCELLED"]),
    _node("Q", modify=["runner.py"], create=["tests/test_q.py"],
          requires=["models.py::JobStatus.CANCELLED"]),
    _node("S", modify=["store.py"], create=["tests/test_s.py"],
          requires=["models.py::JobStatus.CANCELLED"]),
]}


def test_full_flow_merges_queued_nodes_and_writes_all_outputs(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path, [answer(QUEUED_DRAFT)])
    assert result.success, result.report["errors"]
    index = build_index(toy_repo.path, toy_repo.commit)

    unmerged = load_graph(unmerged_path_for(options.out))
    assert [node.id for node in unmerged.nodes] == ["C", "P", "Q", "S"]
    assert {(e.from_, e.to, e.type) for e in unmerged.edges} == {
        ("C", "P", "interface"), ("C", "Q", "interface"), ("C", "S", "interface"),
        ("P", "Q", "order")}
    assert unmerged.generator.revision_mode == "none"
    assert validate(unmerged, index).ok

    graph = load_graph(options.out)
    assert [node.id for node in graph.nodes] == ["C", "P_Q", "S"]
    assert [(e.from_, e.to, e.type) for e in graph.edges] == [
        ("C", "P_Q", "interface"), ("C", "S", "interface")]
    assert graph.generator.revision_mode == "rule_assisted"
    assert graph.generator.planner_version == "planner-v2"
    assert validate(graph, index).ok and planner_checks(graph) == []
    merged = graph.nodes[1]
    assert merged.edit_set.modify == ["runner.py"]
    assert merged.edit_set.create == ["tests/test_p.py", "tests/test_q.py"]
    assert merged.goal == (
        "This sub-task combines 2 parts. Do them all, in this order.\n\n"
        "Part 1 (P: title P):\ngoal P\n\nPart 2 (Q: title Q):\ngoal Q\n\n"
        f"Repository conventions:\n- {CONVENTION}")
    actions = [entry.action for entry in graph.revision_log]
    assert actions[0] == "add_node" and actions[-1] == "merge"
    assert actions.count("merge") == 1 and "add_edge" in actions
    assert graph.revision_log[-1].into == "P_Q"

    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["success"] and report["planner_version"] == "planner-v2"
    assert report["draft_items"] == [
        {"id": name, "title": f"item {name}", "description": f"achieve {name}"}
        for name in ("P", "Q", "S")]
    assert report["estimated_changed_lines"] == 800
    assert len(report["stage1"]) == 1 and report["stage1"][0]["errors"] == []
    assert report["stage1"][0]["answer"] == items_answer(["P", "Q", "S"])
    assert report["node_items"]["P"] == {"item": "P", "reason": None}
    assert report["node_items"]["C"]["item"] is None
    assert report["merges"] == [{"rule": "M1", "members": ["P", "Q"], "into": "P_Q",
                                 "files": ["runner.py"]}]
    assert report["not_merged"] == []
    assert (report["nodes_before_merge"], report["nodes"]) == (4, 3)
    assert report["unmerged_graph"] == str(unmerged_path_for(options.out))
    assert report["totals"] == {"model_calls": 2, "input_tokens": 200, "output_tokens": 20}
    assert report["goal_chars"]["P_Q"] == len(merged.goal)


def test_no_merge_writes_the_unmerged_graph_only(toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    unmerged_path_for(out).write_text("stale", encoding="utf-8")
    worker = ScriptedPlanner([answer(QUEUED_DRAFT)])
    patch_workers(monkeypatch, worker, [items_answer(["P", "Q", "S"])])
    assert main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out), "--no-merge"]) == 0
    graph = load_graph(out)
    assert [node.id for node in graph.nodes] == ["C", "P", "Q", "S"]
    assert graph.generator.revision_mode == "none"
    assert not unmerged_path_for(out).exists()
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    assert report["merge"] is False and report["merges"] == []
    assert (report["nodes_before_merge"], report["nodes"]) == (4, 4)
    assert report["unmerged_graph"] is None
    output = capsys.readouterr().out
    assert "nodes before merge: 4  after: 4 (--no-merge)" in output
    assert "merged (" not in output


def test_plan_command_reports_the_merges(toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    invalid = {"nodes": [*QUEUED_DRAFT["nodes"][:3],
                         {**QUEUED_DRAFT["nodes"][3], "requires": ["store.py::JobStore.nope"]}]}
    worker = ScriptedPlanner([answer(invalid), answer(QUEUED_DRAFT)])
    patch_workers(monkeypatch, worker, [items_answer(["P", "Q", "S"])])
    assert main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "draft items: 3  estimated changed lines: 800" in output
    assert "fast path: not taken (800 >= 500 lines)" in output
    assert "revision rounds: 1" in output
    assert "nodes before merge: 4  after: 3" in output
    assert "merged (M1): P + Q -> P_Q" in output
    assert f"wrote {unmerged_path_for(out)}" in output
    # a revised draft that is then merged by rule is rule_assisted, not llm
    assert load_graph(out).generator.revision_mode == "rule_assisted"
    assert load_graph(unmerged_path_for(out)).generator.revision_mode == "llm"


def test_contract_left_with_one_downstream_node_is_merged_too(toy_repo, tmp_path):
    draft = {"nodes": QUEUED_DRAFT["nodes"][:3]}       # C, P, Q: all end up in one node
    result, _, options = plan(toy_repo, tmp_path, [answer(draft)])
    assert result.success, result.report["errors"]
    graph = load_graph(options.out)
    assert [node.id for node in graph.nodes] == ["C_P_Q"] and graph.edges == []
    assert graph.nodes[0].kind == "implement"
    assert [merge["rule"] for merge in result.report["merges"]] == ["M1", "M2"]
    assert validate(graph, build_index(toy_repo.path, toy_repo.commit)).ok


def test_invalid_graph_after_merging_is_a_program_error(toy_repo, tmp_path, monkeypatch):
    import aqours_code.taskgraph.planner as planner_module
    from aqours_code.taskgraph.revise import Merge, RevisedGraph

    def broken(graph, conventions=None):
        nodes = [node.model_copy(update={"id": "P"}) if node.id == "Q" else node
                 for node in graph.nodes]                # two nodes called P: V1
        return RevisedGraph(graph=graph.model_copy(update={"nodes": nodes}),
                            merges=[Merge("M1", ["P", "Q"], "P_Q", ["runner.py"])])

    monkeypatch.setattr(planner_module, "revise_graph", broken)
    result, worker, options = plan(toy_repo, tmp_path, [answer(QUEUED_DRAFT)])
    assert not result.success and len(worker.requests) == 1   # not sent back to the model
    assert {issue["code"] for issue in result.report["errors"]} == {"REVISE"}
    assert "invalid after merging" in result.report["errors"][0]["message"]
    graph = load_graph(options.out)
    assert [node.id for node in graph.nodes] == ["C", "P", "Q", "S"]
    assert graph == load_graph(unmerged_path_for(options.out))
    assert result.report["merges"] == [] and result.report["nodes"] == 4


def test_draft_prompt_shows_no_example_line_count():
    import re
    text = DRAFT_PROMPT_PATH.read_text(encoding="utf-8")
    assert '"estimated_changed_lines": <number>' in text
    assert ("The example shows the format only; replace <number> with your own "
            "estimate.") in text
    assert re.search(r"\d{2,}", text) is None      # no figure a model could copy


# ── P6: a node must not wait for the implementations of several other items ──

EXPOSURE = ("A node that exposes other nodes' features (REST routes, dashboard pages, a\n"
            "CLI, and so on) calls them through the interfaces the contract defines. Its\n"
            "dependencies are `requires`, not `requires_impl`, and its own tests use a\n"
            "small fake of the objects it calls (for example a fake runner that returns\n"
            "fixed values), so it runs in parallel with the features. Use\n"
            "`requires_impl` only when the node's own code, not its tests, cannot be\n"
            "written against an interface.")


def test_prompt_tells_exposing_nodes_to_depend_on_interfaces():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert EXPOSURE in text
    concepts = text[text.index("## Concepts"):text.index("## Splitting rules")]
    assert EXPOSURE in concepts
    assert "because its own code extends or wraps another node's real behaviour" in concepts
    assert "because its tests call the real behaviour" not in text


def _features(*extra: dict) -> Draft:
    """Three items with one implement node each, plus ``extra`` nodes."""
    return _draft(
        _node("C", "contract", modify=["facade.py"],
              provides=["facade.py::Facade.a", "facade.py::Facade.b", "facade.py::Facade.c"]),
        _node("A", item="1", modify=["a.py"], provides=["a.py::run"]),
        _node("B", item="2", modify=["b.py"], provides=["b.py::run"]),
        _node("B2", item="2", modify=["b2.py"], provides=["b2.py::extra"], reason="separate"),
        *extra)


def test_p6_waiting_for_two_other_items_is_an_error():
    draft = _features(_node("API", item="3", modify=["api.py"],
                            requires_impl=["a.py::run", "b.py::run", "b2.py::extra"]))
    issues = implementation_checks(draft)
    assert _codes(issues) == [("P6", ["API"])]
    assert issues[0].message == (
        "API waits for the implementations of items 1, 2 (requires_impl: a.py::run, "
        "b.py::run, b2.py::extra). A node that only exposes other features must depend "
        "on their interfaces: move these symbols to `requires` and test against a fake")


def test_p6_allows_one_other_item_the_own_item_and_interfaces():
    one_other = _features(_node("API", item="3", modify=["api.py"],
                                requires_impl=["b.py::run", "b2.py::extra"]))
    assert implementation_checks(one_other) == []          # both belong to item 2
    own_item = _features(_node("B3", item="2", modify=["b3.py"],
                               requires_impl=["b.py::run", "b2.py::extra", "a.py::run"]))
    assert implementation_checks(own_item) == []           # only item 1 is another item
    interfaces = _features(_node("API", item="3", modify=["api.py"],
                                 requires=["a.py::run", "b.py::run", "facade.py::Facade.c"]))
    assert implementation_checks(interfaces) == []
    contract_only = _features(_node("API", item="3", modify=["api.py"],
                                    requires_impl=["facade.py::Facade.a", "a.py::run",
                                                   "models.py::Existing"]))
    assert implementation_checks(contract_only) == []      # a contract is not an item


def test_p6_is_reported_and_fixed_by_a_revision_round(toy_repo, tmp_path):
    contract = _node("C", "contract", modify=["models.py"],
                     provides=["models.py::JobStatus.CANCELLED"])
    store = _node("S", modify=["store.py"], create=["tests/test_s.py"],
                  provides=["store.py::JobStore.cancel"],
                  requires=["models.py::JobStatus.CANCELLED"])
    loop = _node("R", modify=["runner.py"], create=["tests/test_r.py"],
                 provides=["runner.py::run_loop"],
                 requires=["models.py::JobStatus.CANCELLED"])
    front = {"modify": ["README.md"], "create": ["tests/test_front.py"]}
    waiting = {"nodes": [contract, store, loop, _node(
        "F", **front, requires_impl=["store.py::JobStore.cancel", "runner.py::run_loop"])]}
    parallel = {"nodes": [contract, store, loop, _node(
        "F", **front, requires=["models.py::JobStatus.CANCELLED"])]}
    result, worker, options = plan(toy_repo, tmp_path, [answer(waiting), answer(parallel)])
    assert result.success, result.report["errors"]
    first = result.report["rounds"][0]["errors"]
    assert [(issue["code"], issue["nodes"]) for issue in first] == [("P6", ["F"])]
    assert "[P6] F: F waits for the implementations of items S, R" in worker.requests[1].prompt
    graph = load_graph(options.out)
    assert {(edge.from_, edge.to) for edge in graph.edges} == {("C", "S"), ("C", "R"),
                                                                ("C", "F")}



# ── thin contracts: P7 and the prompt ──

INTERFACES_ONLY = (
    "A contract writes interfaces only: new fields, signatures with docstrings,\n"
    "   and bodies that run but do nothing useful (return an empty result, raise\n"
    "   NotImplementedError, or delegate to a stub). It never completes an\n"
    "   implementation, not even of a small shared helper. A helper that several\n"
    "   nodes need gets only its signature in the contract; one implement node\n"
    "   writes its body, and the other nodes list it in `requires`.")
ENTRY_POINTS = (
    "Existing entry points that\n"
    "   the request changes (an existing route, form, command or function that\n"
    "   must accept new fields or behave differently) must be in some node's\n"
    "   `modify`. Check every existing file the request affects: if no node lists\n"
    "   it, add it to the contract (for the signature change) or to the node\n"
    "   whose feature needs it.")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_prompt_keeps_the_contract_to_interfaces_and_asks_for_entry_point_owners():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "any small helper that every node needs complete" not in _flat(text)
    assert "helper that every node needs complete" not in _flat(text)
    rules = text[text.index("## Splitting rules"):text.index("## Output format")]
    assert INTERFACES_ONLY in rules and ENTRY_POINTS in rules
    assert _flat(INTERFACES_ONLY) in _flat(rules[rules.index("4. **"):rules.index("5. **")])
    assert _flat(ENTRY_POINTS) in _flat(rules[rules.index("5. **"):rules.index("6. **")])


def test_p7_a_file_the_contract_creates_needs_an_implement_node():
    orphan = _draft(
        _node("C", "contract", modify=["models.py"],
              create=["pkg/validation.py", "pkg/feature.py", "tests/test_c.py"]),
        _node("A", item="1", modify=["pkg/feature.py"]),
        _node("B", item="2", modify=["other.py"], create=["pkg/new.py"]))
    issues = contract_checks(orphan)
    assert _codes(issues) == [("P7", ["C"])]
    assert issues[0].message == (
        "contract C creates pkg/validation.py, but no implement node fills it in. A "
        "contract only writes stubs: give the body to one implement node (put the file in "
        "its `modify`) and let the others `require` its interface")
    filled = _draft(
        _node("C", "contract", modify=["models.py"],
              create=["pkg/validation.py", "pkg/feature.py"]),
        _node("A", item="1", modify=["pkg/feature.py", "pkg/validation.py"]),
        _node("B", item="2", modify=["other.py"]))
    assert contract_checks(filled) == []
    two = _draft(_node("C", "contract", create=["x.py", "y.py"]),
                 _node("A", item="1", modify=["a.py"]))
    assert "creates x.py, y.py, but no implement node fills them in" in \
        contract_checks(two)[0].message
    no_contract = _draft(_node("A", item="1", modify=["a.py"], create=["pkg/new.py"]),
                         _node("B", item="2", modify=["b.py"]))
    assert contract_checks(no_contract) == []
    modify_only = _draft(_node("C", "contract", modify=["models.py"]),
                         _node("A", item="1", modify=["a.py"]))
    assert contract_checks(modify_only) == []


def test_p7_is_reported_and_fixed_by_a_revision_round(toy_repo, tmp_path):
    def draft(owner_modify):
        return {"nodes": [
            _node("C", "contract", modify=["models.py"], create=["checks.py"],
                  provides=["models.py::JobStatus.CANCELLED", "checks.py::check"]),
            _node("S", modify=owner_modify, create=["tests/test_s.py"],
                  requires=["models.py::JobStatus.CANCELLED"]),
            _node("R", modify=["runner.py"], create=["tests/test_r.py"],
                  requires=["checks.py::check"]),
        ]}

    result, worker, options = plan(
        toy_repo, tmp_path,
        [answer(draft(["store.py"])), answer(draft(["store.py", "checks.py"]))])
    assert result.success, result.report["errors"]
    first = result.report["rounds"][0]["errors"]
    assert [(issue["code"], issue["nodes"]) for issue in first] == [("P7", ["C"])]
    assert "[P7] C: contract C creates checks.py, but no implement node fills it in" \
        in worker.requests[1].prompt
    graph = load_graph(options.out)
    assert "checks.py" in next(n for n in graph.nodes if n.id == "S").edit_set.modify


# ── planner v2: the outline in the prompt and the report, long goals ──

def test_prompt_has_the_outline_between_the_file_list_and_the_final_checks(toy_repo, toy_index):
    outline = build_outline(toy_repo.path, toy_index)
    prompt = build_prompt(REQUEST, toy_index, ["python -m pytest -q tests"],
                          outline=outline.text)
    files, section, checks = (prompt.index(heading) for heading in (
        "# Repository\n\n", "# Repository outline\n\n", "# Final checks\n\n"))
    assert files < section < checks
    body = prompt[section:checks]
    assert body == "# Repository outline\n\n" + outline.text.strip() + "\n\n"
    assert "## store.py (16 lines)\nclass JobStore (L4-L16): In-memory job store.\n" in body
    assert "  def add(self, job: Job) -> None (L11)\n" in body
    assert "## runner.py (11 lines)\nMAX_ATTEMPTS = ... (L3)\n" in body
    assert "  fields: PENDING, RUNNING, DONE (L6-L8)" in body
    assert "## broken.py (2 lines) (could not be parsed)" in body
    assert "test_basic" not in body                       # test files are not outlined
    assert "- tests/test_basic.py" in prompt[files:section]  # they are in the file list
    assert "# Repository outline" not in build_prompt(REQUEST, toy_index, [])


def test_every_grounding_round_gets_the_outline_and_the_report_records_it(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path,
                                   [answer(INVALID_DRAFT), answer(VALID_DRAFT)])
    assert result.success and len(worker.requests) == 2
    outline = build_outline(toy_repo.path, build_index(toy_repo.path, toy_repo.commit))
    for request in worker.requests:  # the first round and the revision
        assert "# Repository outline\n\n" + outline.text.strip() + "\n\n# Final checks" \
            in request.prompt
        assert READING_RULE in request.prompt and GOAL_RULE in " ".join(request.prompt.split())
    assert "# Repository outline" not in worker.drafter.requests[0].prompt  # Step 1 is unchanged
    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["planner_version"] == "planner-v2"
    assert (report["outline_chars"], report["outline_reduction"]) == (outline.chars, "none")
    assert report["outline_omitted_files"] == [] and report["long_goals"] == {}


def test_report_records_how_far_the_outline_was_shortened(toy_repo, tmp_path, monkeypatch):
    import aqours_code.taskgraph.planner as planner_module

    index = build_index(toy_repo.path, toy_repo.commit)
    full = build_outline(toy_repo.path, index)
    # In so small an outline the note costs more than the docstrings save, so
    # the first level that fits is the one without methods.
    short = build_outline(toy_repo.path, index, limit=full.chars - 1)
    assert short.reduction == "no_methods"
    monkeypatch.setattr(planner_module, "build_outline",
                        lambda root, index: build_outline(root, index, limit=full.chars - 1))
    result, worker, _ = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)])
    assert result.success
    assert result.report["outline_reduction"] == "no_methods"
    assert result.report["outline_chars"] == short.chars < full.chars
    prompt = worker.requests[0].prompt
    assert "(Shortened to fit: only classes and top-level functions are listed.)" in prompt
    assert "In-memory job store." not in prompt and "def add(self" not in prompt
    assert "class JobStore (L4-L16)\n" in prompt


def test_fast_path_report_has_the_outline_fields_too(toy_repo, tmp_path):
    result, worker, _ = plan(toy_repo, tmp_path, [answer(VALID_DRAFT)], estimate=300)
    assert result.success and worker.requests == []
    assert result.report["outline_chars"] > 0 and result.report["outline_reduction"] == "none"
    assert result.report["long_goals"] == {}


@pytest.mark.parametrize("length", [1500, 1501])
def test_goals_over_1500_characters_are_listed_in_the_report_only(toy_repo, tmp_path, length):
    draft = {"nodes": [_node("A", goal="x" * length, modify=["runner.py"]),
                       _node("B", goal="short", modify=["store.py"])]}
    result, _, options = plan(toy_repo, tmp_path, [answer(draft)])
    assert result.success and result.report["rounds"][0]["errors"] == []  # never an error
    report = json.loads(report_path_for(options.out).read_text(encoding="utf-8"))
    assert report["goal_chars"] == {"A": length, "B": 5}
    assert report["long_goals"] == ({"A": 1501} if length == 1501 else {})
    assert [node.goal for node in load_graph(options.out).nodes] == ["x" * length, "short"]


def test_long_goals_count_the_appended_conventions(toy_repo, tmp_path):
    draft = {"conventions": ["c" * 200], "nodes": [
        _node("A", goal="x" * 1400, modify=["runner.py"])]}
    result, _, _ = plan(toy_repo, tmp_path, [answer(draft)])
    assert result.success
    assert result.report["long_goals"] == {"A": result.report["goal_chars"]["A"]}
    assert result.report["goal_chars"]["A"] > 1600


def test_plan_command_prints_the_outline_size(toy_repo, tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    patch_workers(monkeypatch, ScriptedPlanner([answer(SINGLE_NODE_DRAFT)]),
                  [items_answer(["A"])])
    out = tmp_path / "graph.json"
    assert main(["plan", "--planner", "v2", str(request), "--repo", str(toy_repo.path),
                 "--out", str(out)]) == 0
    report = json.loads(report_path_for(out).read_text(encoding="utf-8"))
    assert (f"outline: {report['outline_chars']} characters (shortened: none)"
            in capsys.readouterr().out)
