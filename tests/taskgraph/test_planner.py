"""Planner v0 with scripted model answers: completion, revision, isolation, CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqours_code.taskgraph import build_index, derive_edges, load_graph
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.planner import (
    PLANNER_VERSION,
    PROMPT_PATH,
    Draft,
    DraftError,
    PlanOptions,
    build_prompt,
    extract_json_block,
    parse_draft,
    report_path_for,
    run_plan,
)
from aqours_code.taskgraph.planner_entry import PLANNER_TOOL_POLICY, run_planner
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest, WorkerResult
from taskgraph_support import commit_files, git

REQUEST = "Add job cancellation to the toy queue.\n"


def _node(node_id: str, kind: str = "implement", **fields) -> dict:
    return {"id": node_id, "title": f"title {node_id}", "kind": kind,
            "goal": f"goal {node_id}", "check": ["python -m pytest -q tests"], **fields}


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
    _node("C", "contract", modify=["models.py"]),
    _node("R", modify=["runner.py"], requires=["store.py::JobStore.nope"]),
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

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.requests: list[WorkerRequest] = []

    def describe(self) -> dict:
        return {"worker": "scripted", "model": "scripted-model"}

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        config = AqoursWorker().config_for(request)
        config.update(model_provider="scripted", model="scripted-model")
        Path(config["log_dir"]).mkdir(parents=True, exist_ok=True)
        return run_planner(config, model_client=FakeClient([final(self.answers.pop(0))]))


def plan(toy_repo, tmp_path: Path, answers: list[str]):
    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    worker = ScriptedPlanner(answers)
    options = PlanOptions(request_path=request, repo=toy_repo.path,
                          out=tmp_path / "out" / "graph.json",
                          final_checks=["python -m pytest -q tests"], timeout_s=60)
    return run_plan(options, worker), worker, options


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
    assert graph.generator.planner_version == PLANNER_VERSION
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
    assert report["totals"] == {"model_calls": 1, "input_tokens": 100, "output_tokens": 10}
    assert report["nodes"] == 3 and report["edges"] == len(graph.edges)


def test_one_revision_fixes_validation_errors(toy_repo, tmp_path):
    result, worker, options = plan(toy_repo, tmp_path,
                                   [answer(INVALID_DRAFT), answer(VALID_DRAFT)])
    assert result.success
    report = result.report
    assert report["revision_rounds"] == 1 and len(report["rounds"]) == 2
    assert report["rounds"][0]["draft"] == answer(INVALID_DRAFT)
    assert [issue["code"] for issue in report["rounds"][0]["errors"]] == ["V6"]
    assert report["rounds"][1]["errors"] == []
    assert report["totals"]["model_calls"] == 2
    assert load_graph(options.out).generator.revision_mode == "llm"

    second_prompt = worker.requests[1].prompt
    assert "store.py::JobStore.nope" in second_prompt  # the previous draft
    assert "[V6] R: requires store.py::JobStore.nope" in second_prompt
    assert "Fix your previous draft" not in worker.requests[0].prompt


def test_persistent_errors_fail_after_two_revisions_but_still_write_outputs(
        toy_repo, tmp_path, monkeypatch, capsys):
    import aqours_code.taskgraph.planner as planner_module

    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = tmp_path / "graph.json"
    worker = ScriptedPlanner([answer(INVALID_DRAFT), "no draft at all", answer(INVALID_DRAFT)])
    monkeypatch.setattr(planner_module, "planner_worker", lambda: worker)
    code = main(["plan", str(request), "--repo", str(toy_repo.path), "--out", str(out),
                 "--final-check", "python -m pytest -q tests", "--timeout", "60"])
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
    assert "model calls: 3" in output


def test_agent_failure_is_a_round_error(toy_repo, tmp_path):
    class Failing(ScriptedPlanner):
        def run(self, request):
            self.requests.append(request)
            return WorkerResult(ok=False, reason="worker_timeout", error="killed")

    request = tmp_path / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    options = PlanOptions(request_path=request, repo=toy_repo.path,
                          out=tmp_path / "graph.json", max_revisions=0)
    result = run_plan(options, Failing([]))
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


def test_planner_prompt_mentions_nothing_of_the_experiment_tasks():
    text = PROMPT_PATH.read_text(encoding="utf-8").lower()
    for word in ("job", "runner", "tenant", "webhook", "recurring", "rate limit",
                 "ratelimit", "audit", "notification", "priorit", "dashboard",
                 "cancel", "retry", "retries", "scheduler"):
        assert word not in text, word
