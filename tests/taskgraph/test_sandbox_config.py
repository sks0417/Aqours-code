"""A task's own sandbox: image and environment from ``sandbox.json``, passed everywhere."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aqours_code.taskgraph import checks as checks_module
from aqours_code.taskgraph import worker_entry
from aqours_code.taskgraph.checks import SandboxChecks, checks_for
from aqours_code.taskgraph.cli import _sandbox, build_parser, main
from aqours_code.taskgraph.sandbox import (
    DEFAULT_IMAGE,
    NO_SANDBOX,
    SandboxConfig,
    docker_executor,
    GIT_VIEW_TARGET,
    git_view_mounts,
    load_sandbox_config,
    task_run_args,
)
from aqours_code.taskgraph.workers import AqoursWorker, WorkerRequest
from aqours_code.taskgraph import gitops
from taskgraph_support import commit_files, git, make_graph, make_node

ENV = {"PATH": "/opt/env/bin:/usr/bin:/bin", "PYTHONPATH": "/workspace"}


def write_config(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_sandbox_config(tmp_path):
    config = load_sandbox_config(write_config(tmp_path / "sandbox.json", {
        "comment": "why", "image": "img:task", "env": ENV, "nofile": 4096, "pids": 2048,
        "git_view": True}))
    assert (config.kind, config.image, config.nofile) == ("docker", "img:task", 4096)
    assert config.env == tuple(ENV.items()) and config.env_dict == ENV
    assert config.to_dict() == {"sandbox": "docker", "sandbox_image": "img:task",
                                "sandbox_env": ENV, "sandbox_nofile": 4096,
                                "sandbox_pids": 2048, "sandbox_git_view": True}
    plain = load_sandbox_config(write_config(tmp_path / "plain.json", {"image": "img:x"}))
    assert plain.env == () and plain.nofile is None and plain.pids is None
    assert plain.to_dict()["sandbox_env"] == {} and plain.to_dict()["sandbox_nofile"] is None
    assert plain.git_view is False and plain.to_dict()["sandbox_git_view"] is False


@pytest.mark.parametrize("data", [
    [], {"env": {}}, {"image": ""}, {"image": "i", "env": ["A=1"]},
    {"image": "i", "env": {"A": 1}}, {"image": "i", "mounts": []},
    {"image": "i", "env": {"1BAD": "x"}}, {"image": "i", "env": {"A B": "x"}},
    {"image": "i", "nofile": "4096"}, {"image": "i", "nofile": 0}, {"image": "i", "nofile": True},
    {"image": "i", "git_view": "yes"}, {"image": "i", "pids": 0}, {"image": "i", "pids": "9"},
])
def test_bad_sandbox_configs_are_refused(tmp_path, data):
    with pytest.raises(ValueError):
        load_sandbox_config(write_config(tmp_path / "sandbox.json", data))


def test_the_default_sandbox_is_unchanged():
    assert SandboxConfig().to_dict() == {"sandbox": "docker", "sandbox_image": DEFAULT_IMAGE,
                                         "sandbox_env": {}, "sandbox_nofile": None,
                                         "sandbox_pids": None, "sandbox_git_view": False}
    assert NO_SANDBOX.to_dict() == {"sandbox": "none", "sandbox_image": None,
                                    "sandbox_env": {}, "sandbox_nofile": None,
                                    "sandbox_pids": None, "sandbox_git_view": False}
    assert SandboxConfig(kind="none", env={"A": "1"}).env_dict == {}  # no container, no env


def test_task_run_args_put_the_variables_before_the_image():
    args = ["docker", "run", "--env", "HOME=/tmp/home", "img:x", "sleep", "infinity"]
    assert task_run_args(args, "img:x", (("A", "1"), ("B", "x=y"))) == [
        "docker", "run", "--env", "HOME=/tmp/home", "--env", "A=1", "--env", "B=x=y",
        "img:x", "sleep", "infinity"]
    assert task_run_args(args, "img:x", ()) == args
    with pytest.raises(ValueError):
        task_run_args(args[:-1], "img:x", ())
    limited = ["docker", "run", "--ulimit", "nofile=256:256", "img:x", "sleep", "infinity"]
    assert task_run_args(limited, "img:x", (), 4096)[3] == "nofile=4096:4096"
    with pytest.raises(ValueError):
        task_run_args(args, "img:x", (), 4096)  # no limit to replace


def test_docker_executor_starts_its_containers_with_the_environment(tmp_path):
    executor = docker_executor(tmp_path, "img:task", "aqours-tg-test", env=ENV)
    args = executor.inner.docker_run_args()
    at = args.index("img:task")
    assert args[at - 4:at] == ["--env", f"PATH={ENV['PATH']}", "--env", "PYTHONPATH=/workspace"]
    assert args[at:] == ["img:task", "sleep", "infinity"]
    for flag in ("--read-only", "--network", "--cap-drop", "--user"):
        assert flag in args  # the sandbox's limits stay
    plain = docker_executor(tmp_path, "img:x", "n").inner.docker_run_args()
    assert plain.count("--env") == 1 and "HOME=/tmp/home" in plain  # only the executor's own
    assert "nofile=256:256" in plain
    raised = docker_executor(tmp_path, "img:x", "n", nofile=4096).inner
    assert "nofile=4096:4096" in raised.docker_run_args()
    assert raised.resource_limits["nofile"] == "4096:4096"
    assert plain[plain.index("--pids-limit") + 1] == "256"
    more = docker_executor(tmp_path, "img:x", "n", pids=4096).inner
    assert more.docker_run_args()[more.docker_run_args().index("--pids-limit") + 1] == "4096"
    assert more.resource_limits["pids_limit"] == 4096


def test_checks_get_the_environment():
    checks = checks_for(SandboxConfig(image="img:task", env=ENV, nofile=4096, pids=2048))
    assert isinstance(checks, SandboxChecks) and checks.env == tuple(ENV.items())
    assert checks.nofile == 4096 and checks.pids == 2048
    assert checks_for(SandboxConfig()).env == () and checks_for(SandboxConfig()).nofile is None


def test_sandbox_checks_pass_the_environment_to_the_executor(tmp_path):
    seen = []

    class Executor:
        def start(self):
            pass

        def execute(self, command, cwd, timeout):
            return {"exit_code": 0, "stdout": "ok\n", "stderr": "", "timed_out": False}

        def stop(self):
            pass

    def factory(workspace, image, name, *, command_timeout, **extra):
        seen.append((image, extra))
        return Executor()

    for env, nofile in ((ENV, 4096), ({}, None)):
        checks = SandboxChecks("img:task", executor_factory=factory,
                               remove=lambda name: [], env=env, nofile=nofile,
                               pids=nofile, git_view=bool(env))
        with checks.session(tmp_path, 60, "label") as run:
            assert run("true").ok
    assert seen == [("img:task", {"env": tuple(ENV.items()), "nofile": 4096, "pids": 4096,
                                  "git_view": True}),
                    ("img:task", {})]  # a factory without the options still works


def test_rescore_records_the_environment(tmp_path, monkeypatch):
    checks = SandboxChecks("img:task", env=ENV, nofile=4096)
    run_dir = tmp_path / "run"
    (run_dir / "final").mkdir(parents=True)
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()

    def fake_hidden(final, tests_dir, checks, *, xml_path, text_path, timeout=0):
        return {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "exit_code": 0,
                "timed_out": False, "duration_s": 0.0}

    monkeypatch.setattr(checks_module, "run_hidden_tests", fake_hidden)
    stats = checks_module.rescore(run_dir, tests, checks)
    assert stats["sandbox_image"] == "img:task" and stats["sandbox_env"] == ENV
    assert stats["sandbox_nofile"] == 4096


def test_the_worker_gets_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("aqours_code.taskgraph.workers.remove_containers", lambda name: [])
    worker = AqoursWorker(entry_command=["python", "-c", "import sys; sys.exit(3)"],
                          sandbox=SandboxConfig(image="img:task", env=ENV, nofile=4096,
                                                pids=2048, git_view=True))
    (tmp_path / "wt").mkdir()
    worker.run(WorkerRequest(node_id="A", attempt=1, prompt="x", workspace=tmp_path / "wt",
                             log_dir=tmp_path / "log", timeout_s=30))
    config = json.loads((tmp_path / "log" / "worker_1_config.json").read_text(encoding="utf-8"))
    assert config["sandbox"]["image"] == "img:task" and config["sandbox"]["env"] == ENV
    assert config["sandbox"]["nofile"] == 4096 and config["sandbox"]["git_view"] is True
    assert config["sandbox"]["pids"] == 2048

    made = []
    monkeypatch.setattr("aqours_code.taskgraph.sandbox.docker_executor",
                        lambda *args, **kwargs: made.append((args, kwargs)) or "executor")
    assert worker_entry.command_executor_for(config, deadline=123.0) == "executor"
    assert made[0][0][1:] == ("img:task", config["sandbox"]["container"])
    assert made[0][1] == {"deadline": 123.0, "env": ENV, "nofile": 4096, "pids": 2048,
                          "git_view": True}
    for key in ("env", "nofile", "pids", "git_view"):  # a config from before the fields
        config["sandbox"].pop(key)
    worker_entry.command_executor_for(config, deadline=1.0)
    assert made[1][1] == {"deadline": 1.0, "env": {}, "nofile": None, "pids": None,
                          "git_view": False}


def test_cli_sandbox_options(tmp_path):
    path = write_config(tmp_path / "sandbox.json", {"image": "img:task", "env": ENV})
    parse = build_parser().parse_args

    def sandbox(*extra):
        return _sandbox(parse(["run", "g.json", "--repo", ".", *extra]))

    assert sandbox() == SandboxConfig(image=DEFAULT_IMAGE)
    assert sandbox("--sandbox-image", "img:y") == SandboxConfig(image="img:y")
    assert sandbox("--sandbox-config", str(path)) == SandboxConfig(image="img:task",
                                                                   env=tuple(ENV.items()))
    assert "not both" in sandbox("--sandbox-config", str(path), "--sandbox-image", "img:y")
    assert "needs --sandbox docker" in sandbox("--sandbox-config", str(path), "--sandbox",
                                               "none")
    assert "cannot read sandbox config" in sandbox("--sandbox-config", str(tmp_path / "no"))
    team = _sandbox(parse(["team", "r.md", "--repo", ".", "--sandbox-config", str(path)]))
    assert team.image == "img:task"


def test_run_refuses_a_bad_sandbox_config(toy_repo, tmp_path, capsys):
    graph = make_graph([make_node("A", create=("a.txt",))])
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)), encoding="utf-8")
    code = main(["run", str(path), "--repo", str(toy_repo.path), "--out", str(tmp_path / "runs"),
                 "--sandbox-config", str(tmp_path / "missing.json")])
    assert code == 2 and "cannot read sandbox config" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


# ── the git view of a worktree ──

def make_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repository with one commit, and a worktree of it with one changed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit = commit_files(repo, {"pkg/__init__.py": "x = 1\n", "README.md": "hi\n"}, "base")
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), commit)
    (worktree / "README.md").write_text("changed\n", encoding="utf-8", newline="\n")
    return repo, worktree, commit


def test_make_git_view_holds_the_worktree_head_and_its_index(tmp_path):
    repo, worktree, commit = make_worktree(tmp_path)
    view = tmp_path / "view"
    assert gitops.make_git_view(worktree, view) == commit
    look = ("--git-dir", str(view), "--work-tree", str(worktree))
    assert git(worktree, *look, "rev-parse", "HEAD") == commit
    assert git(worktree, *look, "status", "--porcelain") == "M README.md"
    assert git(worktree, *look, "rev-list", "--count", "--all") == "1"
    assert (worktree / ".git").read_text(encoding="utf-8").startswith("gitdir: ")  # unchanged


def test_git_view_mounts_only_for_a_worktree(tmp_path):
    repo, worktree, _ = make_worktree(tmp_path)
    mounts = git_view_mounts(worktree, tmp_path / "v1")
    assert len(mounts) == 2
    assert mounts[0].endswith(f"target={GIT_VIEW_TARGET},readonly")
    assert mounts[1].endswith("target=/workspace/.git,readonly")
    gitfile = (tmp_path / "v1" / "gitfile").read_text(encoding="utf-8")
    assert gitfile == f"gitdir: {GIT_VIEW_TARGET}\n"
    assert git_view_mounts(repo, tmp_path / "v2") == []  # a clone's own .git works
    assert git_view_mounts(tmp_path / "v1", tmp_path / "v3") == []  # no .git at all


def test_docker_executor_mounts_the_view_and_removes_it(tmp_path):
    _, worktree, _ = make_worktree(tmp_path)
    executor = docker_executor(worktree, "img:task", "aqours-tg-view", git_view=True)
    args = executor.inner.docker_run_args()
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert len(mounts) == 3 and mounts[0].endswith("target=/workspace")
    view_dir = executor._cleanup[0]
    assert (view_dir / "git" / "HEAD").is_file()
    executor.stop()  # no container was started; the view still goes
    assert not view_dir.exists()
    plain = docker_executor(worktree, "img:task", "aqours-tg-view")
    assert plain.inner.docker_run_args().count("--mount") == 1 and plain._cleanup == []


def test_task_run_args_add_mounts_before_the_image():
    args = ["docker", "run", "--ulimit", "nofile=256:256", "img:x", "sleep", "infinity"]
    assert task_run_args(args, "img:x", (("A", "1"),), None, ["type=bind,source=s,target=t"]) == [
        "docker", "run", "--ulimit", "nofile=256:256", "--env", "A=1",
        "--mount", "type=bind,source=s,target=t", "img:x", "sleep", "infinity"]
