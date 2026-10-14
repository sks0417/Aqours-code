"""The plan-then-parallel baseline (``team``), with scripted workers and no model."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph import team as team_module
from aqours_code.taskgraph.checks import HostChecks, rescore
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.escapes import is_run_dir
from aqours_code.taskgraph.sandbox import SandboxConfig
from aqours_code.taskgraph.team import (
    LEAD_PLAN,
    LEAD_WRAPUP,
    PlanError,
    TeamOptions,
    build_lead_prompt,
    parse_plan,
    run_team,
)
from aqours_code.taskgraph.workers import WorkerRequest, WorkerResult
from taskgraph_support import git

CHECK = 'python -c "print(\'CHECK-MARKER\')"'


def task(task_id: str, files: tuple = (), blocked_by: tuple = (), **extra) -> dict:
    return {"id": task_id, "title": f"title of {task_id}",
            "description": f"description of {task_id}",
            "files": list(files or (f"{task_id}.txt",)), "blocked_by": list(blocked_by),
            **extra}


def answer(*tasks: dict) -> str:
    return "Here is the plan.\n\n```json\n" + json.dumps({"tasks": list(tasks)}) + "\n```\n"


class ScriptedWorker:
    """A worker that follows a script, by node id and attempt.

    A step is a dict (``answer``, ``files`` to write, ``ok``, ``reason``,
    ``error``, token counts) or a function of the request returning one.
    Without a step for a task, it writes ``<node>.txt``.
    """

    sandbox = SandboxConfig(kind="none")

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.requests: list[WorkerRequest] = []
        self._lock = threading.Lock()

    def describe(self) -> dict:
        return {"worker": "scripted"}

    def preflight(self) -> None:
        pass

    def prompts(self, node_id: str) -> list[str]:
        return [request.prompt for request in self.requests if request.node_id == node_id]

    def run(self, request: WorkerRequest) -> WorkerResult:
        with self._lock:
            self.requests.append(request)
        step = self.script.get((request.node_id, request.attempt),
                               self.script.get(request.node_id))
        if callable(step):
            step = step(request)
        if step is None:
            step = ({} if request.node_id in (LEAD_PLAN, LEAD_WRAPUP)
                    else {"files": {f"{request.node_id}.txt": request.node_id}})
        for path, content in step.get("files", {}).items():
            target = request.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        return WorkerResult(
            ok=step.get("ok", True), reason=step.get("reason", ""),
            error=step.get("error", ""),
            final_answer=step.get("answer", f"report of {request.node_id}"),
            model_calls=1, input_tokens=step.get("input_tokens", 10),
            output_tokens=step.get("output_tokens", 1), duration_s=0.01)


def lead(*answers: str, **tokens) -> ScriptedWorker:
    """A lead whose planning rounds give ``answers`` in turn."""
    return ScriptedWorker({(LEAD_PLAN, number): {"answer": text, **tokens}
                           for number, text in enumerate(answers, 1)})


def together(parties: int = 2):
    """A step that returns only once ``parties`` workers have reached it."""
    barrier = threading.Barrier(parties, timeout=30)

    def wait(files: dict):
        def step(request: WorkerRequest) -> dict:
            barrier.wait()
            return {"files": files}
        return step

    return wait


def run(toy_repo, tmp_path, lead_worker, worker=None, **options):
    request = tmp_path / "request.md"
    request.write_text("# The request\n\nBuild the thing.\n", encoding="utf-8")
    worker = worker or ScriptedWorker()
    result = run_team(TeamOptions(request_path=request, repo=toy_repo.path,
                                  out_dir=tmp_path / "runs",
                                  **{"final_checks": [CHECK], **options}),
                      worker, lead_worker)
    return result, worker


def events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def position(log: list[dict], event_type: str, node: str) -> int:
    return next(index for index, event in enumerate(log)
                if event["type"] == event_type and event.get("node") == node)


# ── 1. the lead's plan ──

def test_parse_plan_accepts_a_valid_plan():
    plan = parse_plan(answer(task("T1", files=("src/a.py", "tests/test_a.py")),
                             task(2, blocked_by=("T1",), ignored="x")))
    assert [(t.id, t.files, t.blocked_by) for t in plan.tasks] == [
        ("T1", ["src/a.py", "tests/test_a.py"], []), ("2", ["2.txt"], ["T1"])]
    assert plan.tasks[0].description == "description of T1"


@pytest.mark.parametrize(("tasks", "problem"), [
    ([task("T1"), task("T1")], "task id T1 is used by 2 tasks"),
    ([task("T1", blocked_by=("T9",))], "task T1 is blocked by T9, which is not a task"),
    ([task("T1", blocked_by=("T1",))], "task T1 lists itself in blocked_by"),
    ([task("T1", blocked_by=("T3",)), task("T2", blocked_by=("T1",)),
      task("T3", blocked_by=("T2",))], "blocked_by forms a cycle: T1 -> T3 -> T2 -> T1"),
    ([], "tasks: List should have at least 1 item"),
    ([task(f"T{n}") for n in range(21)], "tasks: List should have at most 20 items"),
    ([{**task("T1"), "description": "  "}], "description must not be empty"),
    ([task("T1", files=("/etc/passwd",))], "path must be relative to the repository root"),
    ([task("T1", files=("../outside.py",))], "'..' segments"),
    ([task("bad id")], "tasks.0.id"),
    ([task("lead_wrapup")], "task id lead_wrapup is reserved"),
])
def test_parse_plan_reports_what_is_wrong(tasks, problem):
    with pytest.raises(PlanError) as excinfo:
        parse_plan(answer(*tasks))
    assert problem in "; ".join(excinfo.value.problems)


def test_parse_plan_needs_a_json_block():
    with pytest.raises(PlanError, match="no ```json code block"):
        parse_plan("I would split it in two.")
    with pytest.raises(PlanError, match="not valid JSON"):
        parse_plan("```json\n{\"tasks\": [\n```")


def test_lead_prompt_is_the_published_one(toy_repo, tmp_path):
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1"))), workers=3)
    prompt = (result.run_dir / "nodes" / LEAD_PLAN / "prompt_1.md").read_text(encoding="utf-8")
    assert prompt.startswith("# Request\n\n# The request\n\nBuild the thing.\n\n"
                             "# Repository files\n\n- README.md\n")
    assert "- tests/test_basic.py\n\n# Your role\n\nYou are the lead of a team of 3 teammates." \
        in prompt
    assert "Give each teammate a different set of files." in prompt
    assert prompt.endswith('```json\n{"tasks": [\n  {"id": "T1", "title": "...", '
                           '"description": "...",\n   "files": ["path/one.py", '
                           '"tests/test_one.py"], "blocked_by": []}\n]}\n```\n')
    assert "# Fix your previous answer" not in prompt
    assert "$" not in build_lead_prompt("r", "- a.py", 4)


@pytest.mark.parametrize(("bad", "problem"), [
    (answer(task("T1"), task("T1")), "task id T1 is used by 2 tasks"),
    (answer(task("T1", blocked_by=("T9",))), "is blocked by T9, which is not a task"),
    (answer(task("T1", blocked_by=("T1",))), "lists itself in blocked_by"),
    (answer(task("T1", blocked_by=("T2",)), task("T2", blocked_by=("T1",))), "forms a cycle"),
    (answer(), "at least 1 item"),
])
def test_an_invalid_plan_is_asked_for_again(toy_repo, tmp_path, bad, problem):
    lead_worker = lead(bad, answer(task("T1")))
    result, worker = run(toy_repo, tmp_path, lead_worker)
    summary = result.summary
    assert summary["status"] == "success" and summary["reason"] == ""
    assert summary["nodes"][LEAD_PLAN]["attempts"] == 2
    second = lead_worker.prompts(LEAD_PLAN)[1]
    assert second.startswith(lead_worker.prompts(LEAD_PLAN)[0])
    assert "# Fix your previous answer" in second and problem in second
    assert "## Previous answer\n\nHere is the plan." in second
    assert [request.node_id for request in worker.requests] == ["T1", LEAD_WRAPUP]
    plan = json.loads((result.run_dir / "lead_plan.json").read_text(encoding="utf-8"))
    assert plan["valid"] and [t["id"] for t in plan["tasks"]] == ["T1"]
    assert len(plan["rounds"]) == 2 and problem in "; ".join(plan["rounds"][0]["problems"])


def test_two_invalid_plans_fail_the_run_before_any_teammate(toy_repo, tmp_path):
    lead_worker = lead(answer(), "still no plan")
    result, worker = run(toy_repo, tmp_path, lead_worker,
                         hidden_tests=tmp_path)  # never reached
    summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary == result.summary
    assert (summary["status"], summary["reason"]) == ("failed", "lead_plan_invalid")
    assert worker.requests == []  # no teammate and no wrap-up ran
    assert list(summary["nodes"]) == [LEAD_PLAN]
    assert summary["nodes"][LEAD_PLAN]["status"] == "failed"
    assert summary["nodes"][LEAD_PLAN]["reason"] == "lead_plan_invalid"
    assert summary["team"]["n_tasks"] == 0 and summary["team"]["tasks"] == []
    assert "no ```json code block" in summary["team"]["plan_problems"][0]
    assert summary["final_checks"] == [] and summary["hidden_tests"] is None
    assert summary["totals"]["model_calls"] == 2
    assert not (result.run_dir / "wt").exists()


def test_a_lead_that_fails_does_not_give_a_plan(toy_repo, tmp_path):
    lead_worker = ScriptedWorker({
        (LEAD_PLAN, 1): {"answer": answer(task("T1")), "ok": False,
                         "reason": "worker_timeout", "error": "killed"},
        (LEAD_PLAN, 2): {"answer": answer(task("T2"))}})
    result, worker = run(toy_repo, tmp_path, lead_worker)
    assert [t["id"] for t in result.summary["team"]["tasks"]] == ["T2"]
    assert "the lead agent failed (worker_timeout): killed" in lead_worker.prompts(LEAD_PLAN)[1]


# ── 2. scheduling ──

def test_blocked_tasks_wait_and_independent_tasks_run_together(toy_repo, tmp_path):
    wait = together(2)
    worker = ScriptedWorker({"T1": wait({"T1.txt": "1"}), "T3": wait({"T3.txt": "3"})})
    plan = answer(task("T1"), task("T2", blocked_by=("T1",)), task("T3"))
    result, _ = run(toy_repo, tmp_path, lead(plan), worker, workers=3)
    summary = result.summary
    assert summary["status"] == "success"
    assert summary["team"]["max_parallel"] >= 2  # T1 and T3 were inside run() together
    log = events(result.run_dir)
    assert position(log, "node_start", "T2") > position(log, "node_completed", "T1")
    assert position(log, "node_start", "T3") < position(log, "node_completed", "T1")
    assert position(log, "node_start", "T1") > position(log, "node_completed", LEAD_PLAN)
    assert {t["id"]: t["status"] for t in summary["team"]["tasks"]} == {
        "T1": "completed", "T2": "completed", "T3": "completed"}
    assert "- T1 [completed] title of T1" in worker.prompts("T2")[0]


def test_one_worker_runs_tasks_in_list_order(toy_repo, tmp_path):
    result, worker = run(toy_repo, tmp_path, lead(answer(task("B"), task("A"), task("C"))),
                         workers=1)
    assert [request.node_id for request in worker.requests] == ["B", "A", "C", LEAD_WRAPUP]
    assert result.summary["team"]["max_parallel"] == 1


def test_a_task_runs_although_the_task_it_waits_for_failed(toy_repo, tmp_path):
    worker = ScriptedWorker({"T1": {"answer": "nothing to do"}})  # never changes a file
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1"), task("T2", blocked_by=("T1",)))),
                    worker)
    summary = result.summary
    assert summary["status"] == "partial"
    assert summary["nodes"]["T1"]["status"] == "failed"
    assert (summary["nodes"]["T1"]["reason"], summary["nodes"]["T1"]["attempts"]) == (
        "no_changes", 2)
    assert summary["nodes"]["T2"]["status"] == "completed"
    assert summary["team"]["failed_tasks"] == ["T1"]
    assert "- T1 [failed] title of T1" in worker.prompts("T2")[0]
    assert "# Previous attempt failed" in worker.prompts("T1")[1]
    assert "Reason: no_changes" in worker.prompts("T1")[1]
    log = events(result.run_dir)
    assert position(log, "node_start", "T2") > position(log, "node_failed", "T1")


def test_a_failed_teammate_is_replaced_once(toy_repo, tmp_path):
    worker = ScriptedWorker({
        ("T1", 1): {"ok": False, "reason": "worker_error", "error": "model unavailable"},
        ("T1", 2): {"files": {"T1.txt": "done"}, "answer": "done on the second try"}})
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1"))), worker)
    node = result.summary["nodes"]["T1"]
    assert (node["status"], node["attempts"], node["worker_reasons"]) == (
        "completed", 2, ["worker_error", ""])
    assert "Reason: worker_error" in worker.prompts("T1")[1]
    assert "model unavailable" in worker.prompts("T1")[1]


# ── 3. the teammate prompt ──

def test_teammate_prompt_has_the_list_the_reports_and_the_task(toy_repo, tmp_path):
    worker = ScriptedWorker({"T1": {"files": {"T1.txt": "1"},
                                    "answer": "I built T1.\n" + "x" * 2000}})
    plan = answer(task("T1", files=("T1.txt", "tests/test_t1.py")),
                  task("T2", blocked_by=("T1",)))
    result, _ = run(toy_repo, tmp_path, lead(plan), worker)
    first, second = worker.prompts("T1")[0], worker.prompts("T2")[0]
    assert first.startswith("# Overall request\n\n# The request\n\nBuild the thing.\n\n"
                            "# Team task list\n\n"
                            "- T1 [in_progress] title of T1 — files: T1.txt, tests/test_t1.py "
                            "— blocked by: (none)\n"
                            "- T2 [pending] title of T2 — files: T2.txt — blocked by: T1\n\n"
                            "# Reports from teammates who finished\n\n(none yet)\n\n"
                            "# Your task: T1 title of T1\n\ndescription of T1\n\n"
                            "Files assigned to you: T1.txt, tests/test_t1.py\n\n# Rules\n")
    assert first.endswith("Work only inside your workspace. Files outside it are not "
                          "available to you.\n")
    assert "- T1 [completed] title of T1" in second
    assert "- T2 [in_progress] title of T2" in second
    report = second[second.index("# Reports from teammates who finished"):
                    second.index("# Your task: T2")]
    assert "## T1 title of T1\n\nI built T1.\nxxx" in report
    assert report.rstrip().endswith("[...]") and len(report) < 1700  # at most 1500 characters
    assert "Your bash commands run in a Linux container" not in second  # --sandbox none
    # no soft wall and no context pack
    for request in worker.requests:
        assert request.soft_wall_enabled is False
        assert request.own_files == [] and request.full_files == []
    node_files = {path.name for path in (result.run_dir / "nodes").rglob("*") if path.is_file()}
    assert not [name for name in node_files if name.startswith("context")]
    assert {"prompt_1.md", "worker_1.json", "diff.patch"} <= node_files


def test_sandboxed_teammates_get_the_sandbox_note(toy_repo, tmp_path):
    worker = ScriptedWorker()
    worker.sandbox = SandboxConfig(kind="docker", image="img:test")
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1"))), worker, checks=HostChecks())
    for node in ("T1", LEAD_WRAPUP):
        assert worker.prompts(node)[0].rstrip().endswith(
            "(ls, cat, grep), not Windows ones (dir, type,\nfindstr).")
    assert (result.summary["sandbox"], result.summary["checks_sandbox"]) == ("docker", "none")


# ── 4. conflicts ──

def conflicting_plan():
    return answer(task("T1", files=("README.md",)), task("T2", files=("README.md",)))


def resolve(request: WorkerRequest) -> dict:
    """Keep both sides of the conflict in README.md."""
    text = (request.workspace / "README.md").read_text(encoding="utf-8")
    assert "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text
    kept = [line for line in text.splitlines()
            if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
    return {"files": {"README.md": "\n".join(kept) + "\n"}, "answer": "kept both"}


def test_a_conflict_goes_back_to_the_task_that_merged_second(toy_repo, tmp_path):
    wait = together(2)  # both start from the same commit
    worker = ScriptedWorker({
        ("T1", 1): wait({"README.md": "# Toy job queue by T1\n"}),
        ("T2", 1): wait({"README.md": "# Toy job queue by T2\n"}),
        ("T1", 2): resolve, ("T2", 2): resolve})
    result, _ = run(toy_repo, tmp_path, lead(conflicting_plan()), worker, workers=2,
                    hidden_tests=None)
    summary = result.summary
    team = summary["team"]
    assert summary["status"] == "success"
    assert (team["conflicts"], team["conflicts_resolved"]) == (1, 1)
    second = next(t for t in team["tasks"] if t["conflict"])
    assert second["conflict_resolved"] and second["conflict_files"] == ["README.md"]
    assert second["status"] == "completed"
    node = summary["nodes"][second["id"]]
    assert node["attempts"] == 2 and node["changed_files"] == ["README.md"]
    prompt = worker.prompts(second["id"])[1]
    assert "# Your work conflicts with teammates' work" in prompt
    assert "conflict markers (<<<<<<<, =======, >>>>>>>): README.md." in prompt
    assert "# Previous attempt failed" not in prompt
    merged = git(result.run_dir / "repo", "show", "HEAD:README.md")
    assert "by T1" in merged and "by T2" in merged and "<<<<<<<" not in merged
    log = events(result.run_dir)
    assert [event["node"] for event in log if event["type"] == "merge_conflict"] == [
        second["id"]]
    assert [event["node"] for event in log if event["type"] == "merge_conflict_resolved"] == [
        second["id"]]


def test_a_conflict_left_in_the_files_fails_the_task(toy_repo, tmp_path):
    wait = together(2)
    worker = ScriptedWorker({
        ("T1", 1): wait({"README.md": "# Toy job queue by T1\n"}),
        ("T2", 1): wait({"README.md": "# Toy job queue by T2\n"}),
        ("T1", 2): {"answer": "could not resolve"}, ("T2", 2): {"answer": "could not resolve"}})
    result, _ = run(toy_repo, tmp_path, lead(conflicting_plan()), worker, workers=2)
    summary = result.summary
    team = summary["team"]
    assert summary["status"] == "partial"
    assert (team["conflicts"], team["conflicts_resolved"]) == (1, 0)
    failed = next(t for t in team["tasks"] if t["status"] == "failed")
    assert failed["reason"] == "merge_conflict" and team["failed_tasks"] == [failed["id"]]
    assert summary["nodes"][failed["id"]]["attempts"] == 2  # one resolution round only
    winner = "T2" if failed["id"] == "T1" else "T1"
    merged = git(result.run_dir / "repo", "show", "HEAD:README.md")
    assert merged == f"# Toy job queue by {winner}"
    # the lead is told
    assert f"## {failed['id']} [failed] title of {failed['id']} (failed: merge_conflict)" \
        in worker.prompts(LEAD_WRAPUP)[0]


def test_merge_keep_conflicts_leaves_markers_and_commit_merge_concludes(toy_repo, tmp_path):
    repo = tmp_path / "clone"
    base = gitops.clone_for_run(toy_repo.path, repo, toy_repo.commit)
    worktree = tmp_path / "wt"
    gitops.add_node_worktree(repo, "A", worktree, base)
    (worktree / "README.md").write_text("# mine\n", encoding="utf-8", newline="\n")
    (worktree / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    gitops.commit_all(worktree, "mine")
    (repo / "README.md").write_text("# theirs\n", encoding="utf-8", newline="\n")
    (repo / "store.py").write_text("# changed\n", encoding="utf-8", newline="\n")
    gitops.commit_all(repo, "theirs")
    theirs = gitops.head(repo)
    assert gitops.squash_merge(repo, "tg/node/A", "squash") is None

    assert gitops.merge_keep_conflicts(worktree, theirs) == ["README.md"]
    text = (worktree / "README.md").read_text(encoding="utf-8")
    assert "<<<<<<<" in text and "# mine" in text and "# theirs" in text
    assert (worktree / "store.py").read_text(encoding="utf-8") == "# changed\n"
    (worktree / "README.md").write_text("# mine\n# theirs\n", encoding="utf-8", newline="\n")
    gitops.commit_merge(worktree, "resolved")
    assert gitops.is_clean(worktree)
    assert gitops.squash_merge(repo, "tg/node/A", "squash") is not None
    assert git(repo, "show", "HEAD:README.md") == "# mine\n# theirs"
    assert gitops.changed_files(repo, theirs) == ["README.md", "new.txt"]


def test_merge_keep_conflicts_is_empty_for_a_clean_merge(toy_repo, tmp_path):
    repo = tmp_path / "clone"
    base = gitops.clone_for_run(toy_repo.path, repo, toy_repo.commit)
    worktree = tmp_path / "wt"
    gitops.add_node_worktree(repo, "A", worktree, base)
    (worktree / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    gitops.commit_all(worktree, "mine")
    (repo / "store.py").write_text("# changed\n", encoding="utf-8", newline="\n")
    gitops.commit_all(repo, "theirs")
    assert gitops.merge_keep_conflicts(worktree, gitops.head(repo)) == []
    gitops.commit_merge(worktree, "merged")
    assert (worktree / "store.py").read_text(encoding="utf-8") == "# changed\n"
    with pytest.raises(gitops.GitError):
        gitops.merge_keep_conflicts(worktree, "0" * 40)


# ── 5. files outside the task ──

def test_files_outside_the_task_are_recorded_not_refused(toy_repo, tmp_path):
    worker = ScriptedWorker({"T1": {"files": {"a.txt": "a", "docs/b.txt": "b"}}})
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1", files=("a.txt",)))), worker)
    summary = result.summary
    node = summary["nodes"]["T1"]
    assert node["status"] == "completed"
    assert node["changed_files"] == ["a.txt", "docs/b.txt"]
    assert node["out_of_scope_files"] == ["docs/b.txt"]
    assert summary["team"]["out_of_scope_total"] == 1
    assert git(result.run_dir / "repo", "show", "HEAD:docs/b.txt") == "b"


# ── 6. the lead's wrap-up ──

def test_wrapup_runs_last_sees_the_checks_and_its_changes_are_merged(toy_repo, tmp_path):
    worker = ScriptedWorker({
        "T1": {"files": {"T1.txt": "1"}, "answer": "T1 is built"},
        "T2": {"answer": "T2 found nothing to do"},
        LEAD_WRAPUP: {"files": {"T2.txt": "done by the lead", "glue.txt": "glue"},
                      "answer": "finished T2"}})
    result, _ = run(toy_repo, tmp_path, lead(answer(task("T1"), task("T2"))), worker)
    summary = result.summary
    log = events(result.run_dir)
    start = position(log, "node_start", LEAD_WRAPUP)
    assert start > position(log, "node_completed", "T1")
    assert start > position(log, "node_failed", "T2")
    assert position(log, "final_checks_end", None) > position(log, "node_completed", LEAD_WRAPUP)
    prompt = worker.prompts(LEAD_WRAPUP)[0]
    assert prompt.startswith("# Overall request\n\n# The request\n\nBuild the thing.\n\n"
                             "# What the team did\n\n- T1 [completed] title of T1")
    assert "- T2 [failed] title of T2" in prompt
    assert "## T1 [completed] title of T1\n\nT1 is built" in prompt
    assert "## T2 [failed] title of T2 (failed: no_changes)\n\nT2 found nothing to do" in prompt
    checks = prompt[prompt.index("# Checks on the merged work"):prompt.index("# Your task")]
    assert f"$ {CHECK}" in checks and "[exit 0," in checks and "CHECK-MARKER" in checks
    assert "You are the team lead." in prompt and "If everything is complete,\nchange nothing." \
        in prompt
    assert "CHECK-MARKER" in (result.run_dir / "wrapup_checks.txt").read_text(encoding="utf-8")
    # its work is in the integration branch
    assert summary["team"]["wrapup_changed_files"] == ["T2.txt", "glue.txt"]
    assert summary["nodes"][LEAD_WRAPUP]["status"] == "completed"
    assert summary["nodes"][LEAD_WRAPUP]["changed_files"] == ["T2.txt", "glue.txt"]
    assert git(result.run_dir / "repo", "show", "HEAD:T2.txt") == "done by the lead"
    assert summary["integration_commit"] == summary["nodes"][LEAD_WRAPUP]["merge_commit"]
    assert summary["status"] == "partial"  # T2 itself failed
    assert summary["final_checks"] == [
        {**summary["final_checks"][0], "command": CHECK, "ok": True}]


def test_wrapup_that_changes_nothing_merges_nothing(toy_repo, tmp_path):
    result, worker = run(toy_repo, tmp_path, lead(answer(task("T1"))))
    summary = result.summary
    assert summary["team"]["wrapup_changed_files"] == []
    assert summary["nodes"][LEAD_WRAPUP]["merge_commit"] is None
    assert summary["integration_commit"] == summary["nodes"]["T1"]["merge_commit"]
    assert len(worker.prompts(LEAD_WRAPUP)) == 1  # the wrap-up is not retried


# ── 7. summary, rescore, audit ──

def test_summary_counts_the_lead_and_rescore_works_on_a_team_run(toy_repo, tmp_path, capsys):
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text(
        "from pathlib import Path\n\n\ndef test_t1():\n"
        "    assert Path('T1.txt').read_text() == 'T1'\n", encoding="utf-8")
    worker = ScriptedWorker({LEAD_WRAPUP: {"input_tokens": 30, "output_tokens": 3}})
    result, _ = run(toy_repo, tmp_path,
                    lead(answer(task("T1")), input_tokens=1000, output_tokens=100),
                    worker, hidden_tests=hidden)
    summary = result.summary
    run_dir = result.run_dir
    assert summary["status"] == "success" and summary["method"] == "team"
    assert list(summary["nodes"]) == [LEAD_PLAN, "T1", LEAD_WRAPUP]
    assert summary["totals"] == {"model_calls": 3, "input_tokens": 1040, "output_tokens": 104}
    assert summary["wall_time_s"] >= summary["nodes"][LEAD_WRAPUP]["end_t"]
    assert summary["hidden_tests"]["passed"] == 1
    assert {"run_id", "config", "integration_commit", "final_checks", "sandbox",
            "checks_sandbox", "escape_attempts_total", "team"} <= set(summary)
    assert {"tasks", "n_tasks", "max_parallel", "conflicts", "conflicts_resolved",
            "out_of_scope_total", "failed_tasks", "wrapup_changed_files"} <= set(summary["team"])
    assert summary["team"]["tasks"][0] == {
        "id": "T1", "title": "title of T1", "files": ["T1.txt"], "blocked_by": [],
        "status": "completed", "reason": "", "conflict": False, "conflict_resolved": False,
        "conflict_files": []}
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert config["method"] == "team" and config["request_id"] == "request"
    assert (config["workers"], config["checks_sandbox"]) == (4, "none")
    # the run directory has the layout of a graph run
    for name in ("events.jsonl", "lead_plan.json", "final_checks.txt", "hidden_tests.xml",
                 "hidden_tests.txt", "wrapup_checks.txt", "summary.json"):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "final" / "T1.txt").is_file()
    assert not (run_dir / "final" / "_hidden_tests").exists()
    assert not (run_dir / "wt").exists()
    assert is_run_dir(run_dir)
    assert main(["audit", str(run_dir)]) == 0
    # rescore
    stats = rescore(run_dir, hidden, HostChecks())
    assert stats["passed"] == 1
    again = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert again["hidden_tests_rescored"]["passed"] == 1
    assert again["hidden_tests"] == summary["hidden_tests"]
    assert again["team"] == summary["team"]


def test_team_command_prints_the_run(toy_repo, tmp_path, capsys, monkeypatch):
    request = tmp_path / "request.md"
    request.write_text("Build it.\n", encoding="utf-8")
    worker = ScriptedWorker({"T2": {"answer": "nothing"}})
    real = team_module.run_team

    def scripted(options, _worker, _lead):
        assert (options.workers, options.final_checks) == (3, [CHECK])
        assert (options.max_attempts, options.lead_timeout_s) == (1, 60.0)
        return real(options, worker, lead(answer(task("T1"), task("T2"))))

    monkeypatch.setattr(team_module, "run_team", scripted)
    code = main(["team", str(request), "--repo", str(toy_repo.path),
                 "--out", str(tmp_path / "runs"), "--workers", "3", "--max-attempts", "1",
                 "--lead-timeout", "60", "--final-check", CHECK, "--sandbox", "none"])
    captured = capsys.readouterr()
    assert code == 1, captured.out  # partial: T2 failed
    for line in ("tasks: 2  max parallel:", "T1: completed attempts=1  title of T1",
                 "T2: failed (no_changes) attempts=1  title of T2",
                 "conflicts: 0 (resolved: 0)", "status: partial", f"final check: ok  {CHECK}",
                 "checks ran in: the host (no sandbox)", "run directory:"):
        assert line in captured.out, line
    assert "--sandbox none" in captured.err
    assert main(["team", str(tmp_path / "missing.md"), "--repo", str(toy_repo.path)]) == 2
    assert main(["team", str(request), "--repo", str(toy_repo.path), "--workers", "0"]) == 2
