"""A check that times out is run once more, and CRLF a worker introduced is undone.

Checks are scripted (no command runs), workers write fixed files, and no model
is called.
"""
from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.checks import RETRY_NOTE, run_checks, run_checks_with_retry
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.line_endings import LineEndings
from aqours_code.taskgraph.process import ProcessResult
from aqours_code.taskgraph.prompting import TIMEOUT_NOTE
from aqours_code.taskgraph.sandbox import SandboxConfig
from aqours_code.taskgraph.team import LEAD_WRAPUP, TeamOptions, run_team
from aqours_code.taskgraph.workers import WorkerRequest, WorkerResult
from taskgraph_support import commit_files, git, make_graph, make_node
from test_team import ScriptedWorker, answer, lead, task

CHECK = "python -m pytest -q tests"
STOPPED = "[timed out after 285 s inside the sandbox]"


class ScriptedChecks:
    """Checks whose results are scripted per label; what is not scripted passes.

    A step is ``"timeout"``, ``"fail"`` or ``"ok"``; one step is used for
    every command that runs.
    """

    kind = "none"
    image = None
    python = sys.executable

    def __init__(self, script: dict[str, list[str]] | None = None):
        self.script = {label: list(steps) for label, steps in (script or {}).items()}
        self.runs: list[str] = []  # the label of every command that ran
        self._lock = threading.Lock()

    @contextmanager
    def session(self, cwd: Path, timeout: float, label: str):
        def run(command) -> ProcessResult:
            line = command if isinstance(command, str) else " ".join(command)
            with self._lock:
                self.runs.append(label)
                steps = self.script.get(label) or ["ok"]
                step = steps.pop(0)
            if step == "timeout":
                return ProcessResult(line, None, f"{label} hung here\n{STOPPED}\n", True, 0.01)
            if step == "fail":
                return ProcessResult(line, 1, f"{label} assertion failed\n", False, 0.01)
            return ProcessResult(line, 0, f"{label} passed\n", False, 0.01)

        yield run


class FileWorker:
    """Writes ``<node>.txt`` (or the given files, as bytes) and keeps its prompts."""

    sandbox = SandboxConfig(kind="none")

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = files
        self.prompts: list[str] = []

    def describe(self) -> dict:
        return {"worker": "file"}

    def preflight(self) -> None:
        pass

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.prompts.append(request.prompt)
        files = self.files or {f"{request.node_id}.txt": request.node_id.encode()}
        for path, content in files.items():
            target = request.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return WorkerResult(ok=True)


def run(toy_repo, tmp_path, script: dict | None = None, worker=None, **options):
    graph = make_graph([make_node("A", create=("A.txt",), commands=(CHECK,))],
                       final_checks=(CHECK,))
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    checks = ScriptedChecks(script)
    worker = worker or FileWorker()
    result = run_graph(graph, toy_repo.path, worker,
                       RunOptions(out_dir=tmp_path / "runs", checks=checks, **options))
    return result, checks, worker


def events(result, event_type: str) -> list[dict]:
    lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [event for event in map(json.loads, lines) if event["type"] == event_type]


def retries(result) -> list[tuple]:
    return [(event["node"], event["kind"], event["ok"])
            for event in events(result, "check_timeout_retry")]


# ── a check that timed out is run once more ──

def test_run_checks_with_retry_runs_a_timed_out_check_once_more(tmp_path):
    checks = ScriptedChecks({"c": ["ok", "timeout"]})
    outcome = run_checks_with_retry(["first", "second", "third"], tmp_path, 300,
                                    checks=checks, label="c")
    assert outcome.ok and not outcome.timed_out and outcome.timeout_retries == 1
    assert checks.runs == ["c"] * 5  # first, second (timeout); then all three again
    assert [item["command"] for item in outcome.results] == ["first", "second", "third"]
    first_run, second_run = outcome.text.split(f"\n{RETRY_NOTE}\n")
    assert STOPPED in first_run and "[timeout, " in first_run
    assert second_run.count("c passed") == 3 and "timeout" not in second_run

    twice = run_checks_with_retry(["only"], tmp_path, 300, label="c",
                                  checks=ScriptedChecks({"c": ["timeout", "timeout", "ok"]}))
    assert not twice.ok and twice.timed_out and twice.timeout_retries == 1  # one rerun only
    assert twice.text.count(STOPPED) == 2

    plain = run_checks(["only"], tmp_path, 300, label="c",
                       checks=ScriptedChecks({"c": ["timeout"]}))
    assert plain.timed_out and plain.timeout_retries == 0  # run_checks itself never reruns


def test_a_failed_check_is_not_run_again(tmp_path):
    checks = ScriptedChecks({"c": ["fail"]})
    outcome = run_checks_with_retry(["first", "second"], tmp_path, 300, checks=checks,
                                    label="c")
    assert not outcome.ok and not outcome.timed_out and outcome.timeout_retries == 0
    assert checks.runs == ["c"] and RETRY_NOTE not in outcome.text


def test_node_check_that_times_out_once_lets_the_node_continue(toy_repo, tmp_path):
    result, checks, worker = run(toy_repo, tmp_path, {"check-A": ["timeout"]})
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["attempts"], node["reason"]) == ("merged", 1, "")
    assert node["check_timeout_retries"] == 1 and len(worker.prompts) == 1  # no new attempt
    assert checks.runs.count("check-A") == 2
    assert retries(result) == [("A", "node", True)]
    assert result.summary["check_timeout_retries_total"] == 1
    log = (result.run_dir / "nodes" / "A" / "check_1.txt").read_text(encoding="utf-8")
    assert STOPPED in log and RETRY_NOTE in log and log.rstrip().endswith("check-A passed")


def test_post_merge_check_that_times_out_once_does_not_start_a_repair(toy_repo, tmp_path):
    result, checks, worker = run(toy_repo, tmp_path, {"merge-A": ["timeout"]})
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["attempts"]) == ("merged", 1)
    assert (node["merge_repairs"], node["merge_repair_ok"]) == (0, None)
    assert node["check_timeout_retries"] == 1 and len(worker.prompts) == 1
    assert checks.runs.count("merge-A") == 2
    assert retries(result) == [("A", "post_merge", True)]
    assert events(result, "merge_repair_start") == []
    assert git(result.run_dir / "repo", "show", "HEAD:A.txt") == "A"
    assert result.summary["status"] == "success"


def test_node_check_that_times_out_twice_is_retried_and_told_so(toy_repo, tmp_path):
    result, checks, worker = run(toy_repo, tmp_path, {"check-A": ["timeout", "timeout"]})
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["attempts"]) == ("merged", 2)  # the usual second attempt
    assert node["check_timeout_retries"] == 1
    assert checks.runs.count("check-A") == 3  # two for attempt 1, one for attempt 2
    assert retries(result) == [("A", "node", False)]
    retry = worker.prompts[1]
    assert "# Previous attempt failed" in retry and "Reason: check_failed" in retry
    assert TIMEOUT_NOTE in retry and "it timed out" in retry
    assert f"check-A hung here\n{STOPPED}" in retry  # the output it printed
    assert retry.index(TIMEOUT_NOTE) < retry.index("Output excerpt:")


def test_post_merge_check_that_times_out_twice_gets_a_repair_that_says_timeout(toy_repo,
                                                                              tmp_path):
    result, checks, worker = run(toy_repo, tmp_path, {"merge-A": ["timeout", "timeout"]})
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["attempts"]) == ("merged", 2)
    assert (node["merge_repairs"], node["merge_repair_ok"]) == (1, True)
    assert node["check_timeout_retries"] == 1
    assert checks.runs.count("merge-A") == 3  # two before the repair, one after it
    assert retries(result) == [("A", "post_merge", False)]
    repair = worker.prompts[1]
    assert "# Your work broke after merging" in repair
    assert TIMEOUT_NOTE in repair
    assert f"merge-A hung here\n{STOPPED}" in repair
    assert repair.index("change nothing and say why") < repair.index(TIMEOUT_NOTE) \
        < repair.index("Output excerpt:")
    log = (result.run_dir / "nodes" / "A" / "post_merge_check.txt").read_text(encoding="utf-8")
    assert log.count(STOPPED) == 2 and RETRY_NOTE in log


def test_two_timeouts_without_repairs_fail_the_node_as_before(toy_repo, tmp_path):
    result, _, _ = run(toy_repo, tmp_path, {"merge-A": ["timeout", "timeout"]},
                       merge_repairs=0)
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["reason"]) == ("failed", "post_merge_check_failed")
    assert node["check_timeout_retries"] == 1
    assert result.summary["check_timeout_retries_total"] == 1


def test_failed_checks_are_never_run_again_and_get_no_timeout_note(toy_repo, tmp_path):
    result, checks, worker = run(toy_repo, tmp_path, {"check-A": ["fail"]})
    node = result.summary["nodes"]["A"]
    assert (node["status"], node["attempts"]) == ("merged", 2)
    assert node["check_timeout_retries"] == 0 and retries(result) == []
    assert checks.runs.count("check-A") == 2  # once per attempt
    assert "check-A assertion failed" in worker.prompts[1]
    assert TIMEOUT_NOTE not in worker.prompts[1] and "timed out" not in worker.prompts[1]

    other = tmp_path / "other"
    other.mkdir()
    result, checks, worker = run(toy_repo, other, {"merge-A": ["fail"], "final": ["fail"]})
    assert checks.runs.count("merge-A") == 2 and checks.runs.count("final") == 1
    assert result.summary["nodes"]["A"]["merge_repairs"] == 1
    assert "# Your work broke after merging" in worker.prompts[1]
    assert TIMEOUT_NOTE not in worker.prompts[1]
    assert result.summary["check_timeout_retries_total"] == 0 and retries(result) == []
    assert result.summary["final_checks"][0]["ok"] is False


def test_final_check_that_times_out_once_passes(toy_repo, tmp_path):
    result, checks, _ = run(toy_repo, tmp_path, {"final": ["timeout"]})
    summary = result.summary
    assert summary["status"] == "success" and summary["final_checks"][0]["ok"] is True
    assert checks.runs.count("final") == 2
    assert retries(result) == [(None, "final", True)]
    assert summary["check_timeout_retries_total"] == 1
    assert summary["nodes"]["A"]["check_timeout_retries"] == 0
    log = (result.run_dir / "final_checks.txt").read_text(encoding="utf-8")
    assert STOPPED in log and RETRY_NOTE in log

    twice = tmp_path / "twice"
    twice.mkdir()
    result, checks, _ = run(toy_repo, twice, {"final": ["timeout", "timeout"]})
    assert result.summary["status"] == "partial" and checks.runs.count("final") == 2
    assert result.summary["final_checks"][0]["timed_out"] is True
    assert retries(result) == [(None, "final", False)]
    assert result.summary["check_timeout_retries_total"] == 1


def test_run_command_prints_the_timeout_retries(toy_repo, tmp_path, capsys):
    graph = make_graph([make_node("A", create=("A.txt",), commands=("python -c 1",))],
                       final_checks=("python -c 1",))
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps({"A": "python -c \"open('A.txt', 'w').write('A')\""}),
                           encoding="utf-8")
    assert main(["run", str(graph_path), "--repo", str(toy_repo.path), "--worker", "command",
                 "--command-map", str(command_map), "--out", str(tmp_path / "runs")]) == 0
    assert "check timeout retries: 0" in capsys.readouterr().out


# ── the team baseline follows the same rule ──

def run_a_team(toy_repo, tmp_path, script, worker=None):
    request = tmp_path / "request.md"
    request.write_text("Build the thing.\n", encoding="utf-8")
    checks = ScriptedChecks(script)
    worker = worker or ScriptedWorker()
    result = run_team(TeamOptions(request_path=request, repo=toy_repo.path,
                                  out_dir=tmp_path / "runs", final_checks=[CHECK],
                                  checks=checks),
                      worker, lead(answer(task("T1"))))
    return result, checks, worker


def test_team_checks_that_time_out_once_are_run_again(toy_repo, tmp_path):
    result, checks, worker = run_a_team(toy_repo, tmp_path,
                                        {"wrapup": ["timeout"], "final": ["timeout"]})
    summary = result.summary
    assert summary["status"] == "success" and summary["final_checks"][0]["ok"] is True
    assert checks.runs == ["wrapup", "wrapup", "final", "final"]
    assert retries(result) == [(LEAD_WRAPUP, "wrapup", True), (None, "final", True)]
    assert summary["check_timeout_retries_total"] == 2
    assert summary["nodes"][LEAD_WRAPUP]["check_timeout_retries"] == 1
    assert summary["nodes"]["T1"]["check_timeout_retries"] == 0
    wrapup = worker.prompts(LEAD_WRAPUP)[0]  # the lead sees both runs
    assert STOPPED in wrapup and RETRY_NOTE in wrapup and "wrapup passed" in wrapup


def test_team_final_check_that_times_out_twice_fails(toy_repo, tmp_path):
    result, checks, _ = run_a_team(toy_repo, tmp_path, {"final": ["timeout", "timeout"]})
    assert result.summary["status"] == "partial"
    assert checks.runs == ["wrapup", "final", "final"]
    assert retries(result) == [(None, "final", False)]
    assert result.summary["check_timeout_retries_total"] == 1


def test_team_failed_check_is_not_run_again(toy_repo, tmp_path):
    result, checks, _ = run_a_team(toy_repo, tmp_path, {"wrapup": ["fail"], "final": ["fail"]})
    assert checks.runs == ["wrapup", "final"] and retries(result) == []
    assert result.summary["check_timeout_retries_total"] == 0


# ── line endings ──

CRLF_README = b"# Toy job queue\r\n\r\nA second line.\r\n"


def worktree_of(toy_repo, tmp_path, commit: str | None = None) -> tuple[Path, Path, str]:
    repo = tmp_path / "clone"
    base = gitops.clone_for_run(toy_repo.path, repo, commit or toy_repo.commit)
    worktree = tmp_path / "wt"
    gitops.add_node_worktree(repo, "A", worktree, base)
    return repo, worktree, base


def test_crlf_a_worker_introduced_is_put_back_to_lf(toy_repo, tmp_path):
    repo, worktree, base = worktree_of(toy_repo, tmp_path)
    (worktree / "README.md").write_bytes(CRLF_README)               # an LF file, now CRLF
    (worktree / "store.py").write_bytes(
        (worktree / "store.py").read_bytes() + b"# one line\r\n# another\n")  # partly CRLF
    (worktree / "pkg").mkdir()
    (worktree / "pkg" / "new.py").write_bytes(b"x = 1\r\ny = 2\r\n")  # new, in an LF repository
    (worktree / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n\0\0data\r\n")  # binary: has NUL
    (worktree / "models.py").write_bytes(
        (worktree / "models.py").read_bytes() + b"# changed, still LF\n")
    (worktree / "runner.py").unlink()                                # deleted
    (worktree / "__pycache__").mkdir()
    (worktree / "__pycache__" / "x.pyc").write_bytes(b"text\r\n")    # ignored by the clone
    endings = LineEndings(repo)
    assert endings.repository_is_lf(base)
    assert endings.restore(worktree, base) == ["README.md", "pkg/new.py", "store.py"]
    assert (worktree / "README.md").read_bytes() == b"# Toy job queue\n\nA second line.\n"
    assert (worktree / "pkg" / "new.py").read_bytes() == b"x = 1\ny = 2\n"
    assert (worktree / "store.py").read_bytes().endswith(b"# one line\n# another\n")
    assert b"\r" not in (worktree / "store.py").read_bytes()
    assert (worktree / "image.bin").read_bytes() == b"\x89PNG\r\n\x1a\n\0\0data\r\n"
    assert (worktree / "__pycache__" / "x.pyc").read_bytes() == b"text\r\n"
    assert endings.restore(worktree, base) == []  # nothing left to do
    # the diff is the change, not the whole file
    gitops.commit_all(worktree, "attempt 1")
    assert git(worktree, "diff", "--numstat", base, "HEAD", "--", "README.md").split()[:2] == [
        "2", "0"]


def test_files_that_were_crlf_stay_crlf(toy_repo, tmp_path):
    commit = commit_files(toy_repo.path, {"legacy.txt": "one\r\ntwo\r\n"}, "a CRLF file")
    repo, worktree, base = worktree_of(toy_repo, tmp_path, commit)
    assert gitops.text_files_have_crlf(repo, base)
    assert not gitops.text_files_have_crlf(repo, toy_repo.commit)   # the commit before it
    (worktree / "legacy.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")  # was CRLF: untouched
    (worktree / "new.txt").write_bytes(b"new\r\n")   # new, but the repository is not all LF
    (worktree / "README.md").write_bytes(CRLF_README)                # was LF: restored
    endings = LineEndings(repo)
    assert not endings.repository_is_lf(base)
    assert endings.restore(worktree, base) == ["README.md"]
    assert (worktree / "legacy.txt").read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert (worktree / "new.txt").read_bytes() == b"new\r\n"
    assert b"\r" not in (worktree / "README.md").read_bytes()


def test_binary_files_in_the_repository_do_not_make_it_crlf(toy_repo, tmp_path):
    (toy_repo.path / "blob.bin").write_bytes(b"\0binary\r\n")
    git(toy_repo.path, "add", "-A")
    git(toy_repo.path, "commit", "-q", "-m", "a binary file")
    commit = git(toy_repo.path, "rev-parse", "HEAD")
    repo, worktree, base = worktree_of(toy_repo, tmp_path, commit)
    assert not gitops.text_files_have_crlf(repo, base)
    assert gitops.blob_at(repo, base, "blob.bin") == b"\0binary\r\n"
    assert gitops.blob_at(repo, base, "missing.txt") is None
    (worktree / "blob.bin").write_bytes(b"\0binary\r\nmore\r\n")
    (worktree / "new.txt").write_bytes(b"new\r\n")
    assert LineEndings(repo).restore(worktree, base) == ["new.txt"]
    assert (worktree / "blob.bin").read_bytes() == b"\0binary\r\nmore\r\n"


def test_coordinator_restores_line_endings_before_the_commit(toy_repo, tmp_path):
    worker = FileWorker({"README.md": CRLF_README, "A.txt": b"new\r\nfile\r\n",
                         "A.bin": b"\0\r\n", "models.py": b"VALUE = 1\n"})
    graph = make_graph([make_node("A", modify=("README.md", "models.py"),
                                  create=("A.txt", "A.bin"), commands=(CHECK,))],
                       final_checks=(CHECK,))
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    result = run_graph(graph, toy_repo.path, worker,
                       RunOptions(out_dir=tmp_path / "runs", checks=ScriptedChecks()))
    node = result.summary["nodes"]["A"]
    assert node["status"] == "merged"
    assert node["normalized_line_endings"] == ["A.txt", "README.md"]
    assert events(result, "line_endings_normalized") == [
        {**events(result, "line_endings_normalized")[0], "node": "A", "attempt": 1,
         "files": ["A.txt", "README.md"]}]
    repo = result.run_dir / "repo"
    assert gitops.blob_at(repo, "HEAD", "README.md") == b"# Toy job queue\n\nA second line.\n"
    assert gitops.blob_at(repo, "HEAD", "A.txt") == b"new\nfile\n"
    assert gitops.blob_at(repo, "HEAD", "A.bin") == b"\0\r\n"
    patch = (result.run_dir / "nodes" / "A" / "diff.patch").read_text(encoding="utf-8")
    assert "\r" not in patch and "+A second line." in patch
    assert "-# Toy job queue" not in patch  # the first line is unchanged again


def test_a_node_that_leaves_line_endings_alone_records_nothing(toy_repo, tmp_path):
    result, _, _ = run(toy_repo, tmp_path)
    assert result.summary["nodes"]["A"]["normalized_line_endings"] == []
    assert events(result, "line_endings_normalized") == []


def test_team_restores_line_endings_for_teammates_and_the_lead(toy_repo, tmp_path):
    worker = ScriptedWorker({
        "T1": lambda request: write_bytes(request, {"README.md": CRLF_README,
                                                     "T1.txt": b"new\r\n"}),
        LEAD_WRAPUP: lambda request: write_bytes(request, {"glue.txt": b"glue\r\n"})})
    result, _, _ = run_a_team(toy_repo, tmp_path, {}, worker)
    nodes = result.summary["nodes"]
    assert nodes["T1"]["normalized_line_endings"] == ["README.md", "T1.txt"]
    assert nodes[LEAD_WRAPUP]["normalized_line_endings"] == ["glue.txt"]
    assert [(event["node"], event["files"])
            for event in events(result, "line_endings_normalized")] == [
        ("T1", ["README.md", "T1.txt"]), (LEAD_WRAPUP, ["glue.txt"])]
    repo = result.run_dir / "repo"
    for path in ("README.md", "T1.txt", "glue.txt"):
        assert b"\r" not in gitops.blob_at(repo, "HEAD", path), path
    assert nodes["T1"]["changed_files"] == ["README.md", "T1.txt"]


def write_bytes(request: WorkerRequest, files: dict[str, bytes]) -> dict:
    for path, content in files.items():
        (request.workspace / path).write_bytes(content)
    return {}


@pytest.mark.parametrize("field", ["check_timeout_retries", "normalized_line_endings"])
def test_every_node_record_has_the_new_fields(toy_repo, tmp_path, field):
    result, _, _ = run(toy_repo, tmp_path)
    assert field in result.summary["nodes"]["A"]
    assert "check_timeout_retries_total" in result.summary
