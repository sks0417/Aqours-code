"""Checks and hidden tests in the sandbox, UTF-8 child processes, and ``rescore``."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from aqours_code.taskgraph import checks as checks_module
from aqours_code.taskgraph import coordinator as coordinator_module
from aqours_code.taskgraph import planner
from aqours_code.taskgraph import workers as workers_module
from aqours_code.taskgraph.checks import (
    HostChecks,
    SandboxChecks,
    checks_for,
    rescore,
    run_checks,
    sandbox_timeout,
    with_sandbox_timeout,
)
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.process import ProcessResult, run_process, utf8_env
from aqours_code.taskgraph.sandbox import (
    DEFAULT_IMAGE,
    SandboxConfig,
    SandboxUnavailable,
    check_docker,
    utf8_run,
)
from aqours_code.taskgraph.workers import (
    AqoursWorker,
    CommandWorker,
    WorkerRequest,
    WorkerResult,
)
from taskgraph_support import make_graph, make_node

NODE_CHECK = 'python -c "print(\'node check\')"'
FINAL_CHECK = 'python -c "print(\'final check\')"'
HIDDEN_COMMAND = ("python -m pytest -q _hidden_tests -p no:cacheprovider "
                  "--junitxml=_hidden_tests.xml")


def docker_ready() -> bool:
    try:
        check_docker(DEFAULT_IMAGE)
    except SandboxUnavailable:
        return False
    return True


needs_docker = pytest.mark.skipif(
    not docker_ready(), reason="needs Docker and the aqours-code-eval image")


class FakeExecutor:
    """What ``docker_executor`` returns, without Docker: the command runs on the host."""

    def __init__(self, container: dict):
        self.container = container

    def start(self) -> None:
        self.container["started"] = True

    def unwrap(self, command: str, timeout: float) -> str:
        """The check command inside ``timeout -k 5 <seconds> /bin/sh -c '<command>'``."""
        wrapper = shlex.split(command)
        assert wrapper[:3] == ["timeout", "-k", "5"] and wrapper[4:6] == ["/bin/sh", "-c"]
        assert len(wrapper) == 7, wrapper
        self.container["inner_timeouts"].append(float(wrapper[3]))
        self.container["host_timeouts"].append(timeout)
        return wrapper[6]

    def execute(self, command: str, cwd, timeout) -> dict:
        assert self.container["started"] and not self.container["stopped"]
        assert Path(cwd) == self.container["workspace"]  # /workspace is the mount itself
        command = self.unwrap(command, timeout)
        self.container["commands"].append(command)
        args = shlex.split(command)
        if args[0] == "python":
            args[0] = sys.executable
        result = run_process(args, cwd=Path(cwd), timeout=timeout)
        return {"command": command, "exit_code": result.exit_code, "stdout": result.output,
                "stderr": "", "timed_out": result.timed_out, "duration_ms": 1}

    def stop(self) -> None:
        self.container["stopped"] = True


class FakeSandbox:
    """Records every container the checks ask for."""

    def __init__(self, executor=FakeExecutor):
        self.containers: list[dict] = []
        self.removed: list[str] = []
        self.executor = executor

    def factory(self, workspace, image, name, *, command_timeout, deadline=None):
        container = {"workspace": Path(workspace), "image": image, "name": name,
                     "command_timeout": command_timeout, "commands": [],
                     "inner_timeouts": [], "host_timeouts": [],
                     "started": False, "stopped": False}
        self.containers.append(container)
        return self.executor(container)

    def checks(self, image: str = "img:test") -> SandboxChecks:
        return SandboxChecks(image, executor_factory=self.factory, remove=self.removed.append)

    def by_label(self) -> dict[str, dict]:
        """Container per label (the name without its prefix and random token)."""
        return {container["name"].removeprefix("aqours-tg-").rsplit("-", 1)[0]: container
                for container in self.containers}


class FileWorker:
    """Creates ``<node>.txt``; says its shell commands run in ``sandbox``."""

    def __init__(self, sandbox: SandboxConfig):
        self.sandbox = sandbox

    def describe(self) -> dict:
        return {"worker": "file"}

    def preflight(self) -> None:
        pass

    def run(self, request: WorkerRequest) -> WorkerResult:
        (request.workspace / f"{request.node_id}.txt").write_text("done\n", encoding="utf-8")
        return WorkerResult(ok=True)


def summary_timeouts(summary: dict) -> int:
    return summary["check_timeout_retries_total"]


def one_node_graph(toy_repo, node_check: str = NODE_CHECK, final_check: str = FINAL_CHECK):
    graph = make_graph([make_node("A", create=("A.txt",), commands=(node_check,))],
                       final_checks=(final_check,))
    return graph.model_copy(update={"base_commit": toy_repo.commit})


def hidden_dir(tmp_path: Path, body: str = "def test_ok():\n    assert True\n") -> Path:
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text(body, encoding="utf-8")
    return hidden


# ── 1. every check runs where the workers' commands run ──

def test_with_a_docker_sandbox_all_four_checks_run_in_the_sandbox(toy_repo, tmp_path,
                                                                  monkeypatch):
    fake = FakeSandbox()
    real_checks_for = coordinator_module.checks_for

    def checks_with_fake_docker(sandbox):
        assert isinstance(real_checks_for(sandbox), SandboxChecks)
        return fake.checks(sandbox.image)

    def on_the_host(*args, **kwargs):
        raise AssertionError(f"a check ran on the host: {args}")

    monkeypatch.setattr(coordinator_module, "checks_for", checks_with_fake_docker)
    monkeypatch.setattr(checks_module, "run_shell", on_the_host)
    monkeypatch.setattr(checks_module, "run_process", on_the_host)
    worker = FileWorker(SandboxConfig(kind="docker", image="img:test"))
    result = run_graph(one_node_graph(toy_repo), toy_repo.path, worker,
                       RunOptions(out_dir=tmp_path / "runs", hidden_tests=hidden_dir(tmp_path)))

    run_dir = result.run_dir
    containers = fake.by_label()
    assert {label: (c["workspace"], c["commands"]) for label, c in containers.items()} == {
        "check-a": (run_dir / "wt" / "A", [NODE_CHECK]),       # node check
        "merge-a": (run_dir / "repo", [NODE_CHECK]),            # post-merge check
        "final": (run_dir / "repo", [FINAL_CHECK]),             # final checks
        "hidden-tests": (run_dir / "final", [HIDDEN_COMMAND]),  # hidden tests
    }
    assert len(fake.containers) == 4
    assert all(c["image"] == "img:test" and c["started"] and c["stopped"]
               for c in fake.containers)
    assert fake.removed == [c["name"] for c in fake.containers]
    assert [containers[label]["command_timeout"]
            for label in ("check-a", "merge-a", "final", "hidden-tests")] == [
        300, 300, coordinator_module.FINAL_CHECK_TIMEOUT_S,
        checks_module.HIDDEN_TESTS_TIMEOUT_S]
    # inside the container each command stops 15 s before the host gives up
    assert [(containers[label]["inner_timeouts"], containers[label]["host_timeouts"])
            for label in ("check-a", "merge-a", "final", "hidden-tests")] == [
        ([285.0], [300]), ([285.0], [300]), ([1785.0], [1800.0]), ([1785.0], [1800.0])]
    assert summary_timeouts(result.summary) == 0
    summary = result.summary
    assert summary["status"] == "success"
    assert summary["final_checks"][0]["ok"]
    assert summary["hidden_tests"]["passed"] == 1
    assert (summary["sandbox"], summary["checks_sandbox"]) == ("docker", "docker")
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert config["checks_sandbox"] == "docker"
    assert "node check" in (run_dir / "nodes" / "A" / "check_1.txt").read_text(encoding="utf-8")
    assert (run_dir / "hidden_tests.xml").is_file()
    assert not (run_dir / "final" / "_hidden_tests.xml").exists()
    assert not (run_dir / "final" / "_hidden_tests").exists()


def test_without_a_sandbox_all_four_checks_run_on_the_host(toy_repo, tmp_path, monkeypatch):
    calls = []
    real_shell, real_process = checks_module.run_shell, checks_module.run_process

    def recording_shell(command, *, cwd, **kwargs):
        calls.append((command, cwd))
        return real_shell(command, cwd=cwd, **kwargs)

    def recording_process(args, *, cwd, **kwargs):
        calls.append((list(args), cwd))
        return real_process(args, cwd=cwd, **kwargs)

    def in_the_sandbox(*args, **kwargs):
        raise AssertionError("a check asked for a sandbox")

    monkeypatch.setattr(checks_module, "run_shell", recording_shell)
    monkeypatch.setattr(checks_module, "run_process", recording_process)
    monkeypatch.setattr(SandboxChecks, "session", in_the_sandbox)
    result = run_graph(one_node_graph(toy_repo), toy_repo.path,
                       FileWorker(SandboxConfig(kind="none")),
                       RunOptions(out_dir=tmp_path / "runs", hidden_tests=hidden_dir(tmp_path)))

    run_dir = result.run_dir
    assert calls == [
        (NODE_CHECK, run_dir / "wt" / "A"),
        (NODE_CHECK, run_dir / "repo"),
        (FINAL_CHECK, run_dir / "repo"),
        ([sys.executable, "-m", "pytest", "-q", "_hidden_tests", "-p", "no:cacheprovider",
          "--junitxml=_hidden_tests.xml"], run_dir / "final"),
    ]
    summary = result.summary
    assert summary["status"] == "success" and summary["hidden_tests"]["passed"] == 1
    assert (summary["sandbox"], summary["checks_sandbox"]) == ("none", "none")
    assert (run_dir / "hidden_tests.xml").is_file()


def test_checks_follow_the_sandbox_and_can_be_given():
    docker = checks_for(SandboxConfig(kind="docker", image="img:x"))
    assert isinstance(docker, SandboxChecks) and (docker.kind, docker.image) == ("docker", "img:x")
    host = checks_for(SandboxConfig(kind="none"))
    assert isinstance(host, HostChecks) and (host.kind, host.image) == ("none", None)


def test_cli_says_where_the_checks_ran(toy_repo, tmp_path, capsys):
    graph_path = tmp_path / "graph.json"
    graph = one_node_graph(toy_repo)
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps({"A": "python -c \"open('A.txt', 'w').write('A')\""}),
                           encoding="utf-8")
    code = main(["run", str(graph_path), "--repo", str(toy_repo.path), "--worker", "command",
                 "--command-map", str(command_map), "--out", str(tmp_path / "runs")])
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert "checks ran in: the host (no sandbox)" in captured.out
    assert "all checks run on this machine" in captured.err


def test_one_container_serves_the_commands_of_one_check(tmp_path):
    fake = FakeSandbox()
    outcome = run_checks(['python -c "print(1)"', "", 'python -c "import sys; sys.exit(3)"',
                          'python -c "print(2)"'], tmp_path, 60, checks=fake.checks(),
                         label="check-My Node")
    assert not outcome.ok
    assert [item["exit_code"] for item in outcome.results] == [0, 3]  # stops at the failure
    assert len(fake.containers) == 1
    container = fake.containers[0]
    assert container["name"].startswith("aqours-tg-check-my-node-")
    assert container["stopped"] and fake.removed == [container["name"]]
    assert "[exit 3," in outcome.text

    every = run_checks(['python -c "import sys; sys.exit(3)"', 'python -c "print(2)"'],
                       tmp_path, 60, stop_on_failure=False, checks=fake.checks())
    assert [item["ok"] for item in every.results] == [False, True]


def test_a_sandbox_that_fails_gives_a_failed_check_not_an_exception(tmp_path):
    class Broken(FakeExecutor):
        def start(self) -> None:
            raise RuntimeError("Docker sandbox failed to start: no daemon")

    fake = FakeSandbox(Broken)
    outcome = run_checks(["python -c 1"], tmp_path, 60, checks=fake.checks())
    assert not outcome.ok
    assert outcome.results[0]["exit_code"] is None and not outcome.results[0]["timed_out"]
    assert "[sandbox error] RuntimeError: Docker sandbox failed to start" in outcome.text
    assert fake.removed == [fake.containers[0]["name"]]


def test_sandbox_results_keep_stderr_and_timeouts(tmp_path):
    class Scripted(FakeExecutor):
        def execute(self, command: str, cwd, timeout) -> dict:
            if shlex.split(command)[-1] == "slow":
                return {"exit_code": None, "stdout": "partial", "stderr": "",
                        "timed_out": True}
            return {"exit_code": 1, "stdout": "out", "stderr": "err\n", "timed_out": False}

    fake = FakeSandbox(Scripted)
    outcome = run_checks(["quick", "slow"], tmp_path, 60, stop_on_failure=False,
                         checks=fake.checks())
    assert outcome.results[0]["exit_code"] == 1 and not outcome.results[0]["timed_out"]
    assert outcome.results[1]["timed_out"] and outcome.results[1]["exit_code"] is None
    assert "out\nerr\n" in outcome.text and "[timeout," in outcome.text


@needs_docker
def test_all_four_checks_pass_only_inside_a_real_container(toy_repo, tmp_path):
    # Only true in the container: the checked directory is /workspace.
    in_container = 'test "$(pwd)" = /workspace && test -f A.txt'
    hidden = hidden_dir(tmp_path, "import os\n\n\ndef test_in_container():\n"
                                  "    assert os.getcwd() == '/workspace'\n")
    result = run_graph(one_node_graph(toy_repo, in_container, in_container), toy_repo.path,
                       FileWorker(SandboxConfig(kind="docker", image=DEFAULT_IMAGE)),
                       RunOptions(out_dir=tmp_path / "runs", hidden_tests=hidden,
                                  max_attempts=1))
    summary = result.summary
    assert summary["nodes"]["A"]["status"] == "merged", summary["nodes"]["A"]
    assert summary["status"] == "success", summary["final_checks"]
    assert summary["hidden_tests"]["passed"] == 1, (
        result.run_dir / "hidden_tests.txt").read_text(encoding="utf-8")
    assert summary["checks_sandbox"] == "docker"
    left = subprocess.run(["docker", "ps", "--all", "--filter", "name=aqours-tg-",
                           "--format", "{{.Names}}"], capture_output=True, text=True)
    assert not [name for name in left.stdout.split()
                if "-check-a-" in name or "-merge-a-" in name]


@needs_docker
def test_real_sandbox_check_has_no_network_and_sees_only_the_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    label = f"probe-{uuid.uuid4().hex[:8]}"
    outcome = run_checks(
        ["pwd && echo made > made.txt", "cat ../secret.txt",
         "python -c \"import urllib.request; "
         "urllib.request.urlopen('http://example.com', timeout=5)\""],
        workspace, 60, stop_on_failure=False, checks=SandboxChecks(DEFAULT_IMAGE), label=label)
    assert [item["ok"] for item in outcome.results] == [True, False, False]
    assert "/workspace" in outcome.text and "SECRET" not in outcome.text
    assert (workspace / "made.txt").read_text(encoding="utf-8").strip() == "made"
    left = subprocess.run(["docker", "ps", "--all", "--filter", f"name={label}",
                           "--format", "{{.Names}}"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


# ── 2. child processes use UTF-8 ──

def test_utf8_env_adds_the_two_variables():
    env = utf8_env({"KEEP": "x", "PYTHONIOENCODING": "cp1252"})
    assert env == {"KEEP": "x", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    assert utf8_env()["PYTHONUTF8"] == "1"


def test_worker_and_planner_children_start_with_utf8_environment(tmp_path, monkeypatch):
    seen = []

    def recording_run_process(args, *, cwd, timeout, env=None, shell=False):
        seen.append(dict(env or {}))
        return ProcessResult(command="", exit_code=0, output="{}", timed_out=False,
                             duration_s=0.0)

    monkeypatch.setattr(workers_module, "run_process", recording_run_process)
    for name, worker in (("worker", AqoursWorker()), ("planner", planner.planner_worker()),
                         ("draft", planner.draft_worker())):
        worker.describe()
        worker.run(WorkerRequest(node_id="A", attempt=1, prompt="p", workspace=tmp_path,
                                 log_dir=tmp_path / name, timeout_s=5))
    assert len(seen) == 6
    assert all((env["PYTHONUTF8"], env["PYTHONIOENCODING"]) == ("1", "utf-8") for env in seen)
    assert all("AQOURS_CODE_WORKDIR" in env for env in seen[1::2])


# A worker entry that prints what the Windows default encoding cannot encode
# (as the planner did) and bytes that are not UTF-8.
ENTRY_SCRIPT = r'''
import json, os, sys

config = json.load(open(sys.argv[sys.argv.index("--config") + 1], encoding="utf-8"))
print("replacement � and 你好")
sys.stdout.flush()
sys.stdout.buffer.write(b"bad bytes \xff\xfe\n")
sys.stdout.buffer.flush()
answer = "|".join([os.environ.get("PYTHONUTF8", ""), os.environ.get("PYTHONIOENCODING", ""),
                   sys.stdout.encoding.lower()])
with open(config["result_path"], "w", encoding="utf-8") as handle:
    json.dump({"ok": True, "final_answer": answer}, handle)
'''


def test_worker_child_prints_any_character_and_undecodable_bytes(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text(ENTRY_SCRIPT, encoding="utf-8")
    worker = AqoursWorker(entry_command=[sys.executable, str(script)])
    log_dir = tmp_path / "logs"
    result = worker.run(WorkerRequest(node_id="A", attempt=1, prompt="p", workspace=tmp_path,
                                      log_dir=log_dir, timeout_s=60))
    assert result.ok, result.error
    assert result.final_answer == "1|utf-8|utf-8"
    stdout = (log_dir / "worker_1_stdout.txt").read_text(encoding="utf-8")
    assert "replacement � and 你好" in stdout
    assert "bad bytes ��" in stdout


def test_command_worker_child_has_utf8_environment(tmp_path):
    command = (f'"{sys.executable}" -c "import os, sys; '
               "print(os.environ['PYTHONUTF8'], os.environ['PYTHONIOENCODING'], "
               "os.environ['TG_NODE_ID'], sys.stdout.encoding.lower())\"")
    result = CommandWorker({"A": command}).run(
        WorkerRequest(node_id="A", attempt=1, prompt="p", workspace=tmp_path,
                      log_dir=tmp_path, timeout_s=60))
    assert result.ok, result.error
    assert result.final_answer.split() == ["1", "utf-8", "A", "utf-8"]


def test_undecodable_output_is_replaced_not_raised(tmp_path):
    emit = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end')"
    result = run_process([sys.executable, "-c", emit], cwd=tmp_path, timeout=60)
    assert result.ok and result.output == "ok �� end"
    # the runner behind every docker call (text=True would raise with a strict codec)
    proc = utf8_run([sys.executable, "-c", emit], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout == "ok �� end"
    raw = utf8_run([sys.executable, "-c", emit], capture_output=True, timeout=60)
    assert raw.stdout == b"ok \xff\xfe end"  # bytes stay bytes


# ── 3. rescore ──

HIDDEN_TESTS = {
    "conftest.py": ("import sys\nfrom pathlib import Path\n\nimport pytest\n\n"
                    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n\n\n"
                    "@pytest.fixture\ndef broken():\n    raise RuntimeError('fixture')\n"),
    "test_calc.py": ("from calc import add\n\n\ndef test_passes():\n    assert add(1, 2) == 3\n\n\n"
                     "def test_fails():\n    assert add(1, 2) == 4\n\n\n"
                     "def test_errors(broken):\n    pass\n"),
}
ORIGINAL = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "parse_error": "no element found", "exit_code": 3221225477, "timed_out": False,
            "duration_s": 1.5}


def fake_run(tmp_path: Path, summary: dict | None = None) -> tuple[Path, Path]:
    """A finished run (final code, summary, the first result) and a hidden test directory."""
    run_dir = tmp_path / "runs" / "20260101T000000Z-abcd"
    (run_dir / "final").mkdir(parents=True)
    (run_dir / "final" / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                               encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(
        {"run_id": run_dir.name, "status": "success", "hidden_tests": ORIGINAL,
         "sandbox": "docker", "sandbox_image": None} if summary is None else summary),
        encoding="utf-8")
    (run_dir / "hidden_tests.txt").write_text("Windows fatal exception\n", encoding="utf-8")
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    for name, content in HIDDEN_TESTS.items():
        (hidden / name).write_text(content, encoding="utf-8")
    return run_dir, hidden


def assert_rescored(run_dir: Path, sandbox: str) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rescored = summary["hidden_tests_rescored"]
    assert {key: rescored[key] for key in ("passed", "failed", "errors", "skipped")} == {
        "passed": 1, "failed": 1, "errors": 1, "skipped": 0}
    assert rescored["sandbox"] == sandbox and "parse_error" not in rescored
    assert rescored["timed_out"] is False and rescored["exit_code"] == 1
    assert rescored["rescored_at"] and rescored["hidden_tests"].endswith("hidden")
    # the run's own result is kept
    assert summary["hidden_tests"] == ORIGINAL and summary["status"] == "success"
    assert (run_dir / "hidden_tests.txt").read_text(encoding="utf-8") == (
        "Windows fatal exception\n")
    assert "1 failed, 1 passed, 1 error" in (run_dir / "hidden_tests_rescored.txt").read_text(
        encoding="utf-8")
    assert 'tests="3"' in (run_dir / "hidden_tests_rescored.xml").read_text(encoding="utf-8")
    # no answers left behind
    assert not (run_dir / "final" / "_hidden_tests").exists()
    assert not (run_dir / "final" / "_hidden_tests.xml").exists()
    assert (run_dir / "final" / "calc.py").is_file()
    return rescored


def test_rescore_on_the_host_writes_new_results_and_removes_the_tests(tmp_path, capsys):
    run_dir, hidden = fake_run(tmp_path)
    code = main(["rescore", str(run_dir), "--hidden-tests", str(hidden), "--sandbox", "none"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert ("hidden tests (rescored, the host (no sandbox)): 1 passed, 1 failed, 1 errors, "
            "0 skipped") in captured.out
    assert "--sandbox none" in captured.err
    rescored = assert_rescored(run_dir, "none")
    assert rescored["sandbox_image"] is None
    # a second rescore replaces the first; the tests left by a failed clean-up do not matter
    (run_dir / "final" / "_hidden_tests").mkdir()
    (run_dir / "final" / "_hidden_tests" / "test_stale.py").write_text(
        "def test_stale():\n    assert False\n", encoding="utf-8")
    assert main(["rescore", str(run_dir), "--hidden-tests", str(hidden),
                 "--sandbox", "none"]) == 0
    assert_rescored(run_dir, "none")


def test_rescore_in_the_sandbox_mounts_final(tmp_path):
    run_dir, hidden = fake_run(tmp_path)
    fake = FakeSandbox()
    stats = rescore(run_dir, hidden, fake.checks("img:test"))
    assert [(c["workspace"], c["image"], c["commands"]) for c in fake.containers] == [
        (run_dir / "final", "img:test", [HIDDEN_COMMAND])]
    assert fake.containers[0]["stopped"] and fake.removed == [fake.containers[0]["name"]]
    assert stats == assert_rescored(run_dir, "docker")
    assert stats["sandbox_image"] == "img:test"


def test_rescore_defaults_to_docker_and_never_falls_back(tmp_path, capsys, monkeypatch):
    run_dir, hidden = fake_run(tmp_path, {"hidden_tests": ORIGINAL, "sandbox_image": "img:run"})
    asked = []

    def unavailable(image):
        asked.append(image)
        raise SandboxUnavailable("--sandbox docker needs a running Docker daemon.")

    monkeypatch.setattr("aqours_code.taskgraph.sandbox.check_docker", unavailable)
    before = (run_dir / "summary.json").read_text(encoding="utf-8")
    code = main(["rescore", str(run_dir), "--hidden-tests", str(hidden)])
    captured = capsys.readouterr()
    assert code == 2 and "needs a running Docker daemon" in captured.err
    assert asked == ["img:run"]  # the image the run used
    assert (run_dir / "summary.json").read_text(encoding="utf-8") == before
    assert not (run_dir / "final" / "_hidden_tests").exists()
    assert not (run_dir / "hidden_tests_rescored.txt").exists()
    assert main(["rescore", str(run_dir), "--hidden-tests", str(hidden),
                 "--sandbox-image", "img:other"]) == 2
    assert asked == ["img:run", "img:other"]


def test_rescore_rejects_what_is_not_a_finished_run(tmp_path, capsys):
    run_dir, hidden = fake_run(tmp_path)
    args = ["--hidden-tests", str(hidden), "--sandbox", "none"]
    assert main(["rescore", str(tmp_path / "missing"), *args]) == 2
    assert main(["rescore", str(run_dir), "--hidden-tests", str(tmp_path / "none"),
                 "--sandbox", "none"]) == 2
    (run_dir / "summary.json").rename(run_dir / "summary.json.bak")
    assert main(["rescore", str(run_dir), *args]) == 2
    assert "no summary.json" in capsys.readouterr().err
    (run_dir / "summary.json.bak").rename(run_dir / "summary.json")
    (run_dir / "final" / "calc.py").unlink()
    (run_dir / "final").rmdir()
    assert main(["rescore", str(run_dir), *args]) == 2
    assert "no final/ directory" in capsys.readouterr().err


def test_rescore_reports_a_test_run_that_gave_no_result(tmp_path, capsys):
    run_dir, hidden = fake_run(tmp_path)
    (hidden / "conftest.py").write_text("import os\n\nos._exit(7)\n", encoding="utf-8")
    code = main(["rescore", str(run_dir), "--hidden-tests", str(hidden), "--sandbox", "none"])
    captured = capsys.readouterr()
    assert code == 1
    assert "gave no result (exit code 7)" in captured.err
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["hidden_tests_rescored"]["exit_code"] == 7
    assert "parse_error" in summary["hidden_tests_rescored"]
    assert summary["hidden_tests"] == ORIGINAL
    assert not (run_dir / "final" / "_hidden_tests").exists()


@needs_docker
def test_rescore_in_a_real_container(tmp_path, capsys):
    run_dir, hidden = fake_run(tmp_path)
    (hidden / "test_where.py").write_text(
        "import os\n\n\ndef test_in_container():\n    assert os.getcwd() == '/workspace'\n",
        encoding="utf-8")
    code = main(["rescore", str(run_dir), "--hidden-tests", str(hidden)])
    captured = capsys.readouterr()
    assert code == 0, captured.err + captured.out
    assert "hidden tests (rescored, docker): 2 passed, 1 failed, 1 errors" in captured.out
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["hidden_tests_rescored"]["sandbox"] == "docker"
    assert summary["hidden_tests_rescored"]["sandbox_image"] == DEFAULT_IMAGE
    assert summary["hidden_tests"] == ORIGINAL
    assert not (run_dir / "final" / "_hidden_tests").exists()


# ── a command that hangs in the sandbox still returns its output ──

@pytest.mark.parametrize(("host", "inside"), [
    (300, 285), (1800, 1785), (40, 25), (30, 15), (20, 10), (10, 5), (1, 0.5)])
def test_sandbox_timeout_is_15_seconds_shorter_but_at_least_half(host, inside):
    assert sandbox_timeout(host) == inside


def test_check_command_is_wrapped_in_timeout_and_quoted():
    assert with_sandbox_timeout("python -m pytest -q tests", 285) == (
        "timeout -k 5 285 /bin/sh -c 'python -m pytest -q tests'")
    assert with_sandbox_timeout("true", 0.5).startswith("timeout -k 5 0.5 /bin/sh -c ")
    assert with_sandbox_timeout("true", 123456789.0).startswith("timeout -k 5 123456789 ")
    for line in ("printf '%s\\n' \"it's $HOME\" && echo a\\b; echo 'x \"y\"' | cat",
                 "python -c 'print(1)'\necho second line",
                 'test "$(pwd)" = /workspace && test -f A.txt',
                 "echo `date` \\ $((1 + 2)) > out.txt 2>&1",
                 "python -m pytest -q 'tests/test_x.py::test_y[a- b ]'"):
        wrapped = with_sandbox_timeout(line, 7.5)
        # a POSIX shell reads it back as: timeout, its options, and the command intact
        assert shlex.split(wrapped) == ["timeout", "-k", "5", "7.5", "/bin/sh", "-c", line]


@pytest.mark.parametrize("code", [124, 137])
def test_a_command_stopped_inside_the_sandbox_is_a_timeout_that_keeps_its_output(tmp_path,
                                                                                code):
    class Stopped(FakeExecutor):
        def execute(self, command: str, cwd, timeout) -> dict:
            self.unwrap(command, timeout)
            return {"exit_code": code, "stdout": "collected 3 items\ntests/test_a.py ..",
                    "stderr": "", "timed_out": False}

    fake = FakeSandbox(Stopped)
    outcome = run_checks(["python -m pytest -q tests", "never reached"], tmp_path, 300,
                         checks=fake.checks())
    assert not outcome.ok and outcome.timed_out and outcome.timeout_retries == 0
    assert outcome.results == [{"command": "python -m pytest -q tests", "ok": False,
                                "exit_code": None, "timed_out": True,
                                "duration_s": outcome.results[0]["duration_s"]}]
    assert outcome.text.startswith("$ python -m pytest -q tests\n[timeout, ")
    assert outcome.text.endswith("collected 3 items\ntests/test_a.py ..\n"
                                 "[timed out after 285 s inside the sandbox]\n")
    assert fake.containers[0]["inner_timeouts"] == [285.0]
    assert fake.containers[0]["host_timeouts"] == [300]  # the host's limit stays


def test_other_exit_codes_and_host_checks_are_not_wrapped_or_reinterpreted(tmp_path,
                                                                            monkeypatch):
    class Failing(FakeExecutor):
        def execute(self, command: str, cwd, timeout) -> dict:
            self.unwrap(command, timeout)
            return {"exit_code": 1, "stdout": "1 failed", "stderr": "", "timed_out": False}

    failed = run_checks(["python -m pytest -q tests"], tmp_path, 300,
                        checks=FakeSandbox(Failing).checks())
    assert not failed.ok and not failed.timed_out
    assert failed.results[0]["exit_code"] == 1 and "inside the sandbox" not in failed.text

    seen = []

    def recording_shell(command, *, cwd, timeout, **kwargs):
        seen.append(command)
        return ProcessResult(command=command, exit_code=124, output="", timed_out=False,
                             duration_s=0.0)

    monkeypatch.setattr(checks_module, "run_shell", recording_shell)
    host = run_checks(["python -m pytest -q tests"], tmp_path, 300, checks=HostChecks())
    assert seen == ["python -m pytest -q tests"]  # as written, no timeout wrapper
    assert not host.timed_out and host.results[0]["exit_code"] == 124


@needs_docker
def test_real_sandbox_stops_a_hanging_command_and_keeps_what_it_printed(tmp_path):
    started = time.monotonic()
    outcome = run_checks(
        ["echo before; sleep 60; echo after",
         "printf '%s|%s\\n' \"it's quoted\" \"$(pwd)\""],
        tmp_path, 8, stop_on_failure=False, checks=SandboxChecks(DEFAULT_IMAGE),
        label="probe-timeout")
    assert time.monotonic() - started < 40  # not the 60 s the command wanted
    assert [(item["timed_out"], item["ok"]) for item in outcome.results] == [
        (True, False), (False, True)]
    assert "before\n[timed out after 4 s inside the sandbox]\n" in outcome.text
    assert "before\nafter" not in outcome.text  # stopped before the second echo
    assert "it's quoted|/workspace" in outcome.text  # the quoting survives the wrapper
