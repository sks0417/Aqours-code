"""Checks for the model smoke test in experiments/taskgraph/models/ (no model is called).

The repository preparation and the result check of ``smoke/smoke.ps1`` are
tested through ``smoke/smoke.py`` and through the script itself
(``-PrepareOnly``, ``-CheckRun``) on fake run directories; ``use_model.ps1``
on a temporary repository root.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph import build_index, load_graph, validate
from aqours_code.taskgraph.checks import SandboxChecks, run_checks
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.sandbox import DEFAULT_IMAGE, SandboxUnavailable, check_docker

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "experiments" / "taskgraph" / "models"
SMOKE = MODELS / "smoke"
BASE_COMMIT = "836c69491eedaa3dd7f5afba0c708fe0bc50bfce"
FINAL_CHECK = "python -m pytest -q tests"
SECRET = "sk-test-0123456789abcdef-never-printed"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("model_smoke", SMOKE / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs PowerShell")


def docker_ready() -> bool:
    try:
        check_docker(DEFAULT_IMAGE)
    except SandboxUnavailable:
        return False
    return True


def powershell(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), *args],
        capture_output=True, timeout=120, env=env)
    proc.stdout = proc.stdout.decode("utf-8", errors="replace")
    proc.stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc


def env_file(path: Path, model: str = "fake-model") -> Path:
    path.write_text(f"AQOURS_CODE_PROVIDER=fake\nAQOURS_CODE_API_KEY={SECRET}\n"
                    f"AQOURS_CODE_BASE_URL=https://fake.invalid/v1\nAQOURS_CODE_MODEL={model}\n",
                    encoding="utf-8")
    return path


# ── a fake run directory, laid out as the Coordinator leaves it ──

OPS_WITH_MUL = '''\
"""Arithmetic operations."""


def add(a, b):
    """Return the sum of ``a`` and ``b``."""
    return a + b


def mul(a, b):
    """Return the product of ``a`` and ``b``."""
    return a * b
'''
TESTS_WITH_MUL = '''\
from calc.ops import add, mul


def test_add():
    assert add(2, 3) == 5


def test_add_negative_numbers():
    assert add(-2, -3) == -5


def test_mul():
    assert mul(3, 4) == 12
'''


def event(kind: str, **payload) -> dict:
    return {"type": kind, "ts": 0.0, **payload}


def request(n: int = 1) -> list[dict]:
    """``n`` model calls that each ask for a tool."""
    return [event("llm_request", model="fake-model", max_tokens=8000, message_count=1,
                  tool_count=7, purpose="lead", agent_role="")
            for _ in range(n)]


def response(stop_reason: str = "tool_use") -> dict:
    return event("llm_response", content=[], stop_reason=stop_reason, usage={},
                 purpose="lead", agent_role="")


def tool(tool_id: str, name: str, **tool_input) -> dict:
    return event("tool_use", tool=name, tool_use_id=tool_id, input=tool_input)


HEALTHY_TRACE = [
    *request(), response(),
    tool("t1", "read_file", path="calc/ops.py"),
    event("tool_result", tool="read_file", tool_use_id="t1", content="def add(a, b): ..."),
    *request(), response(),
    tool("t2", "edit_file", path="calc/ops.py", old_text="a + b\n", new_text="a + b\n\n\ndef mul"),
    tool("t3", "bash", command="python -m pytest -q tests"),
    *request(), response("end_turn"),
    event("final_answer", content="Added mul and its test."),
]
PROBLEM_TRACE = [
    *request(),
    # one failed request, recorded again as the error propagates: one HTTP error
    event("error", error_type="RuntimeError",
          message=f'Model request failed: HTTP 400: {{"error": "bad tools", "key": "{SECRET}"}}'),
    event("error", error_type="RuntimeError",
          message=f'Model request failed: HTTP 400: {{"error": "bad tools", "key": "{SECRET}"}}'),
    *request(),
    event("error", error_type="OverloadedError", message="Error code: 529 - overloaded"),
    *request(),
    event("error", error_type="TimeoutError", message="The read operation timed out"),
    *request(), response("max_tokens"),
    tool("t1", "bash", _raw='{"command": "ls'),
    *request(), response("max_tokens"),
    tool("t2", "read_file", path="../outside.txt"),
    tool("t3", "bash", command="cat /home/someone/notes.txt"),
    *request(), response("end_turn"),
]
PROBLEM_STDOUT = (
    "\x1b[36m> bash\x1b[0m\n"
    "  \x1b[33m[429] retry 1/10 after 1.3s\x1b[0m\n"
    "  \x1b[33m[provider_http_503] retry 2/10 after 2.1s\x1b[0m\n"
    "  \x1b[33m[provider_timeout] retry 3/10 after 4.0s\x1b[0m\n"
)


LIMITS_128K = {"initial": 128000, "escalated": 128000}


def make_run(tmp_path: Path, *, name: str = "run", ops: str = OPS_WITH_MUL,
             tests: str = TESTS_WITH_MUL, trace: list[dict] = HEALTHY_TRACE,
             stdout: str = "> bash\n", status: str = "success", final_ok: bool = True,
             worker_reason: str = "", with_summary: bool = True,
             worker_extra: dict | None = None) -> Path:
    """A run directory of the single graph with one attempt of node ``solve``.

    ``worker_extra`` replaces fields of ``config["worker"]`` (what describe() reported).
    """
    base = tmp_path / f"{name}-base"
    commit = smoke.prepare(base)
    run = tmp_path / name
    node = run / "nodes" / "solve"
    node.mkdir(parents=True)
    smoke.git(run, "clone", "-q", str(base), "repo")
    repo = run / "repo"
    smoke.git(repo, "checkout", "-q", "-b", smoke.INTEGRATION_BRANCH)
    (repo / "calc" / "ops.py").write_text(ops, encoding="utf-8", newline="\n")
    (repo / "tests" / "test_ops.py").write_text(tests, encoding="utf-8", newline="\n")
    smoke.git(repo, "commit", "-q", "-am", "tg: merge solve")
    integration = smoke.git(repo, "rev-parse", "HEAD")

    worker = {"worker": "aqours", "model_provider": "fake", "model": "fake-model",
              "max_tokens": LIMITS_128K, "reasoning_effort": "high", **(worker_extra or {})}
    config = {"base_commit": commit,
              "worker": {key: value for key, value in worker.items() if value is not ...}}
    (run / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (node / "worker_1_config.json").write_text(
        json.dumps({"workspace": str(run / "wt" / "solve")}), encoding="utf-8")
    (node / "worker_1.json").write_text(json.dumps(
        {"ok": not worker_reason, "reason": worker_reason, "model_calls": 4,
         "input_tokens": 1200, "output_tokens": 300}), encoding="utf-8")
    (node / "worker_1_stdout.txt").write_text(stdout, encoding="utf-8")
    trace_text = "".join(json.dumps(item) + "\n" for item in trace)
    if with_summary:
        (node / "trace_1.jsonl").write_text(trace_text, encoding="utf-8")
        summary = {
            "run_id": name, "status": status, "config": config,
            "nodes": {"solve": {"status": "merged" if status == "success" else "failed",
                                "reason": "" if status == "success" else "check_failed",
                                "attempts": 1, "worker_reasons": [worker_reason]}},
            "totals": {"model_calls": 4, "input_tokens": 1200, "output_tokens": 300},
            "integration_commit": integration,
            "final_checks": [{"command": FINAL_CHECK, "ok": final_ok,
                              "exit_code": 0 if final_ok else 1, "timed_out": False,
                              "duration_s": 0.5}],
        }
        (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    else:  # a worker killed before it wrote trace_1.jsonl: only the live Aqours trace
        live = node / "aqours_1" / "trace" / "session"
        live.mkdir(parents=True)
        (live / "trace.jsonl").write_text(trace_text, encoding="utf-8")
    return run


# ── the template and the repository ──


def test_request_asks_for_mul_and_its_tests():
    text = (SMOKE / "request.md").read_text(encoding="utf-8")
    for needle in ("`mul(a, b)`", "`calc/ops.py`", "`tests/test_ops.py`",
                   "Keep the existing tests"):
        assert needle in text


def test_prepare_is_deterministic_with_lf_endings(tmp_path):
    first, second = smoke.prepare(tmp_path / "a"), smoke.prepare(tmp_path / "b")
    assert first == second == BASE_COMMIT
    files = smoke.git(tmp_path / "a", "ls-files").splitlines()
    assert files == ["README.md", "calc/__init__.py", "calc/ops.py", "tests/test_ops.py"]
    for name in files:
        assert b"\r\n" not in (tmp_path / "a" / name).read_bytes()
    log = smoke.git(tmp_path / "a", "log", "--format=%an <%ae> %at %ct %s")
    # 1767225600: 2026-01-01T00:00:00Z
    assert log == ("Model Smoke Test <smoke@example.invalid> 1767225600 1767225600 "
                   "model smoke test base")


def test_prepare_refuses_a_directory_that_is_not_empty(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        smoke.prepare(tmp_path / "repo")
    assert smoke.main(["prepare", str(tmp_path / "repo")]) == smoke.EXIT_INPUT_ERROR


def test_template_tests_pass_on_the_host(tmp_path):
    smoke.prepare(tmp_path / "repo")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests"], cwd=tmp_path / "repo", capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


@pytest.mark.docker
@pytest.mark.skipif(not docker_ready(), reason="needs Docker and the aqours-code-eval image")
def test_template_tests_pass_in_the_sandbox(tmp_path):
    smoke.prepare(tmp_path / "repo")
    outcome = run_checks([FINAL_CHECK], tmp_path / "repo", 300,
                         checks=SandboxChecks(DEFAULT_IMAGE), label="final")
    assert outcome.ok, outcome.text
    assert "2 passed" in outcome.text


def test_single_graph_of_the_request_is_valid(tmp_path):
    repo = tmp_path / "repo"
    smoke.prepare(repo)
    graph_path = tmp_path / "graph.json"
    assert main(["single", str(SMOKE / "request.md"), "--repo", str(repo),
                 "--out", str(graph_path), "--final-check", FINAL_CHECK]) == 0
    graph = load_graph(graph_path)
    assert graph.base_commit == BASE_COMMIT
    assert graph.final_checks == [FINAL_CHECK]
    assert [node.id for node in graph.nodes] == ["solve"]
    report = validate(graph, build_index(repo, graph.base_commit))
    assert report.ok, report.format()


# ── the check: the code ──


@pytest.mark.parametrize("source, expected", [
    (OPS_WITH_MUL, True),
    ("def mul(a, b):\n    return a * b\n", True),
    ("def mul(*args):\n    return args[0] * args[1]\n", True),
    ("def add(a, b):\n    return a + b\n", False),
    ("def mul(a, b):\n    pass\n", False),
    ('def mul(a, b):\n    """Later."""\n    raise NotImplementedError\n', False),
    ("def mul(a, b):\n    ...\n", False),
    ("def mul(a):\n    return a\n", False),
    ("def mul(a, b)\n    return a * b\n", False),
    (None, False),
])
def test_mul_implemented(source, expected):
    assert smoke.mul_implemented(source)[0] is expected


def test_mul_tests_finds_functions_and_methods_that_call_mul():
    source = '''\
from calc import ops
from calc.ops import add, mul


def test_add():
    assert add(1, 2) == 3


def test_mul():
    assert mul(2, 3) == 6


def helper():
    return mul(1, 1)


class TestOps:
    def test_mul_zero(self):
        assert ops.mul(0, 5) == 0

    def test_add(self):
        assert add(0, 0) == 0
'''
    assert smoke.mul_tests(source) == ["test_mul", "TestOps.test_mul_zero"]
    assert smoke.mul_tests(TESTS_WITH_MUL) == ["test_mul"]
    assert smoke.mul_tests("def test_x(:\n") == []
    assert smoke.mul_tests(None) == []


# ── the check: whole run directories ──


def test_check_of_a_healthy_run_passes(tmp_path):
    run = make_run(tmp_path)
    result = smoke.check(run, env_file(tmp_path / ".env"))
    assert result.passed
    assert (result.env_model, result.run_provider, result.run_model) == (
        "fake-model", "fake", "fake-model")
    assert result.status == "success"
    assert (result.model_calls, result.input_tokens, result.output_tokens) == (4, 1200, 300)
    assert result.max_tokens == LIMITS_128K
    assert ("  output limits      : initial 128000, escalated 128000"
            in smoke.format_check(result).splitlines())
    assert result.reasoning_effort == "high"
    assert "  reasoning effort   : high" in smoke.format_check(result).splitlines()
    assert result.final_check_passed
    assert result.mul_implemented and result.mul_tests == ["test_mul"]
    assert result.changed_files == ["calc/ops.py", "tests/test_ops.py"]
    logs = result.logs
    assert (logs.trace_files, logs.stdout_files, logs.tool_calls, logs.pytest_calls) == (1, 1, 3, 1)
    assert (logs.http_errors, logs.max_tokens, logs.raw_inputs, logs.escape_attempts) == (0, 0, 0, 0)
    assert all(item["ok"] for item in result.criteria.values())
    assert list(result.criteria) == ["tool calls work", "edits files", "runs tests",
                                     "finishes normally"]


def test_check_counts_every_problem_in_the_logs(tmp_path):
    run = make_run(tmp_path, ops="def add(a, b):\n    return a + b\n",
                   tests="from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n",
                   trace=PROBLEM_TRACE, stdout=PROBLEM_STDOUT, status="failed",
                   final_ok=False, worker_reason="worker_timeout")
    result = smoke.check(run, env_file(tmp_path / ".env", model="other-model"))
    assert not result.passed
    logs = result.logs
    # trace: HTTP 400 (recorded twice, one error) and 529; stdout: retried 429 and 503;
    # the timeouts are not HTTP errors
    assert logs.http_errors == 4
    assert logs.http_statuses == {"400": 1, "429": 1, "503": 1, "529": 1}
    assert logs.max_tokens == 2
    assert logs.raw_inputs == 1
    assert logs.escape_attempts == 2
    assert logs.pytest_calls == 0
    assert not result.mul_implemented and result.mul_tests == []
    assert not result.final_check_passed
    assert {name: item["ok"] for name, item in result.criteria.items()} == {
        "tool calls work": False, "edits files": False, "runs tests": False,
        "finishes normally": False}
    assert "worker_timeout" in result.criteria["finishes normally"]["detail"]
    report = smoke.format_check(result)
    assert SECRET not in report
    assert "HTTP errors on model requests         : 4  (HTTP 400 x1, HTTP 429 x1" in report
    assert "replies cut at max_tokens             : 2" in report
    assert "tool inputs that did not parse (_raw) : 1" in report
    assert "escape attempts                       : 2" in report
    assert "the run did not use the model in .env" in report
    assert "Smoke result: FAIL" in report


def test_check_shows_limits_that_are_not_recorded_or_invalid(tmp_path):
    # a run from before the limits were recorded: no worker.max_tokens at all
    old = make_run(tmp_path, name="old", worker_extra={"max_tokens": ...})
    report = smoke.format_check(smoke.check(old, tmp_path / "missing.env"))
    assert ("  output limits      : not recorded (config.json of the run has no "
            "worker.max_tokens)") in report.splitlines()
    error = "AQOURS_CODE_MAX_TOKENS must be a positive whole number, got '0'"
    bad = make_run(tmp_path, name="bad", worker_extra={"max_tokens": None,
                                                       "max_tokens_error": error})
    report = smoke.format_check(smoke.check(bad, tmp_path / "missing.env"))
    assert f"  output limits      : invalid: {error}" in report.splitlines()


def test_check_shows_a_level_that_is_none_not_recorded_or_invalid(tmp_path):
    def line(name: str, extra: dict) -> str:
        report = smoke.format_check(smoke.check(make_run(tmp_path, name=name, worker_extra=extra),
                                                tmp_path / "missing.env"))
        return next(item for item in report.splitlines() if "reasoning effort" in item)

    # recorded as null: the requests asked no level (unset, or deepseek-flash)
    assert line("none", {"reasoning_effort": None}) == (
        "  reasoning effort   : none sent (the provider's default)")
    # a run from before the level was recorded
    assert line("old", {"reasoning_effort": ...}) == (
        "  reasoning effort   : not recorded (config.json of the run has no "
        "worker.reasoning_effort)")
    error = ("AQOURS_CODE_REASONING_EFFORT must be one of low, medium, high, xhigh, max "
             "(or unset), got 'hgh'")
    assert line("bad", {"reasoning_effort": None, "reasoning_effort_error": error}) == (
        f"  reasoning effort   : invalid: {error}")


def served_by(trace: list[dict], *providers: str | None) -> list[dict]:
    """``trace`` with its llm_response events naming ``providers`` in turn (None: no name)."""
    names = iter(providers)
    result = []
    for item in trace:
        if item["type"] == "llm_response":
            name = next(names)
            item = {**item, **({"provider": name} if name is not None else {})}
        result.append(item)
    return result


def provider_lines(report: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith("  provider ")]


def test_check_shows_the_provider_set_and_the_providers_that_served(tmp_path):
    run = make_run(tmp_path, trace=served_by(HEALTHY_TRACE, "Z.AI", "Z.AI", "Z.AI"),
                   worker_extra={"openrouter_provider": ["z-ai"]})
    result = smoke.check(run, env_file(tmp_path / ".env"))
    assert result.passed
    assert result.openrouter_provider == ["z-ai"]
    assert (result.logs.served_by, result.logs.replies) == ({"Z.AI": 3}, 3)
    report = smoke.format_check(result)
    assert provider_lines(report) == ["  provider set       : z-ai (no fallbacks)",
                                      "  provider served    : Z.AI x3"]
    # right after the thinking level
    lines = report.splitlines()
    assert lines[lines.index("  reasoning effort   : high") + 1] == provider_lines(report)[0]


def test_check_counts_every_provider_that_served_and_the_replies_that_name_none(tmp_path):
    run = make_run(tmp_path, trace=served_by(PROBLEM_TRACE, "DeepInfra", "Z.AI", "Z.AI"),
                   worker_extra={"openrouter_provider": ["z-ai", "deepinfra"]},
                   status="failed", final_ok=False)
    result = smoke.check(run, tmp_path / "missing.env")
    assert result.logs.served_by == {"Z.AI": 2, "DeepInfra": 1}
    assert provider_lines(smoke.format_check(result)) == [
        "  provider set       : z-ai, deepinfra (no fallbacks)",
        "  provider served    : Z.AI x2, DeepInfra x1"]
    mixed = make_run(tmp_path, name="mixed", trace=served_by(HEALTHY_TRACE, "Z.AI", None, None))
    assert provider_lines(smoke.format_check(smoke.check(mixed, tmp_path / "missing.env")))[1] == (
        "  provider served    : Z.AI x1; 2 replies named none")
    one = make_run(tmp_path, name="one", trace=served_by(HEALTHY_TRACE, "Z.AI", "Z.AI", None))
    assert provider_lines(smoke.format_check(smoke.check(one, tmp_path / "missing.env")))[1] == (
        "  provider served    : Z.AI x2; 1 reply named none")


def test_check_counts_the_providers_of_a_run_that_did_not_finish(tmp_path):
    # no trace_<n>.jsonl: the Aqours run trace of the attempt is read instead
    run = make_run(tmp_path, trace=served_by(PROBLEM_TRACE, "Z.AI", "Z.AI", "Z.AI"),
                   with_summary=False, worker_reason="worker_error")
    assert smoke.check(run, tmp_path / "missing.env").logs.served_by == {"Z.AI": 3}


def test_check_shows_a_provider_that_is_none_not_recorded_or_invalid(tmp_path):
    def lines(name: str, extra: dict) -> list[str]:
        run = make_run(tmp_path, name=name, worker_extra=extra)
        return provider_lines(smoke.format_check(smoke.check(run, tmp_path / "missing.env")))

    # recorded as null: not pinned (unset, not OpenRouter, or DeepSeek); no reply names one
    assert lines("none", {"openrouter_provider": None}) == [
        "  provider set       : none (OpenRouter picks the provider of each request)",
        "  provider served    : none named in the traces (3 replies)"]
    # a run from before the setting was recorded
    assert lines("old", {})[0] == (
        "  provider set       : not recorded (config.json of the run has no "
        "worker.openrouter_provider)")
    error = ("AQOURS_CODE_OPENROUTER_PROVIDER must be comma-separated OpenRouter provider "
             "slugs such as z-ai (or unset), got 'Z.AI'")
    assert lines("bad", {"openrouter_provider": None, "openrouter_provider_error": error})[0] == (
        f"  provider set       : invalid: {error}")


def test_check_json_has_the_providers(tmp_path):
    run = make_run(tmp_path, trace=served_by(HEALTHY_TRACE, "Z.AI", "Z.AI", "Z.AI"),
                   worker_extra={"openrouter_provider": ["z-ai"]})
    out = tmp_path / "check.json"
    assert smoke.main(["check", str(run), "--env-file", str(env_file(tmp_path / ".env")),
                       "--json", str(out)]) == smoke.EXIT_PASS
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["openrouter_provider"] == ["z-ai"]
    assert data["logs"]["served_by"] == {"Z.AI": 3}
    assert data["logs"]["replies"] == 3


def test_check_of_a_run_that_did_not_finish(tmp_path):
    run = make_run(tmp_path, trace=PROBLEM_TRACE, with_summary=False,
                   worker_reason="worker_error")
    result = smoke.check(run, tmp_path / "missing.env")
    assert not result.passed and not result.summary_found
    assert result.env_model is None
    assert result.status.startswith("no summary.json")
    assert (result.model_calls, result.input_tokens, result.output_tokens) == (4, 1200, 300)
    # the live trace under aqours_1/trace/ is scanned instead of trace_1.jsonl
    assert result.logs.trace_files == 1 and result.logs.max_tokens == 2
    assert not result.criteria["finishes normally"]["ok"]
    assert "did not finish" in result.criteria["finishes normally"]["detail"]
    assert "final check        : not run" in smoke.format_check(result)


def test_check_command_writes_json_and_reads_only_the_model_of_env(tmp_path, capsys):
    run = make_run(tmp_path)
    out = tmp_path / "check.json"
    code = smoke.main(["check", str(run), "--env-file", str(env_file(tmp_path / ".env")),
                       "--json", str(out)])
    printed = capsys.readouterr().out
    assert code == smoke.EXIT_PASS
    assert "Smoke result: PASS" in printed and "model in .env      : fake-model" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["passed"] is True and data["logs"]["tool_calls"] == 3
    for text in (printed, out.read_text(encoding="utf-8")):
        assert SECRET not in text and "fake.invalid" not in text
    assert smoke.main(["check", str(tmp_path / "nowhere")]) == smoke.EXIT_INPUT_ERROR


# ── smoke.ps1 ──


@needs_powershell
def test_smoke_ps1_prepare_only(tmp_path):
    proc = powershell(SMOKE / "smoke.ps1", "-Root", str(tmp_path / "smoke"), "-PrepareOnly")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (smoke_dir,) = (tmp_path / "smoke").iterdir()
    repo = smoke_dir / "repo"
    assert smoke.git(repo, "rev-parse", "HEAD") == BASE_COMMIT
    assert BASE_COMMIT in (smoke_dir / "prepare.log").read_text(encoding="utf-8")
    assert f"repository: {repo}" in proc.stdout
    assert sorted(path.name for path in smoke_dir.iterdir()) == ["prepare.log", "repo"]


@needs_powershell
def test_smoke_ps1_check_run_passes_on_a_healthy_run(tmp_path):
    run = make_run(tmp_path)
    proc = powershell(SMOKE / "smoke.ps1", "-CheckRun", str(run),
                      "-EnvFile", str(env_file(tmp_path / ".env")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "== step 4: check" in proc.stdout
    assert "model in .env      : fake-model" in proc.stdout
    assert "Smoke result: PASS" in proc.stdout
    assert "SMOKE PASSED" in proc.stdout and str(run) in proc.stdout
    assert SECRET not in proc.stdout + proc.stderr


@needs_powershell
def test_smoke_ps1_check_run_names_the_failed_step_and_the_run(tmp_path):
    run = make_run(tmp_path, trace=PROBLEM_TRACE, stdout=PROBLEM_STDOUT, status="failed",
                   final_ok=False)
    proc = powershell(SMOKE / "smoke.ps1", "-CheckRun", str(run),
                      "-EnvFile", str(env_file(tmp_path / ".env")))
    assert proc.returncode == 1
    assert "Smoke result: FAIL" in proc.stdout
    assert "SMOKE FAILED at step 4 (check): the smoke criteria are not met" in proc.stdout
    assert f"run directory  : {run}" in proc.stdout
    assert SECRET not in proc.stdout + proc.stderr

    missing = powershell(SMOKE / "smoke.ps1", "-CheckRun", str(tmp_path / "nowhere"))
    assert missing.returncode == 1
    assert "SMOKE FAILED at step 4 (check): run directory not found" in missing.stdout


@needs_powershell
def test_smoke_ps1_stops_at_preflight_without_a_model(tmp_path):
    root = tmp_path / "smoke"
    proc = powershell(SMOKE / "smoke.ps1", "-Root", str(root),
                      "-EnvFile", str(tmp_path / "missing.env"))
    assert proc.returncode == 1
    assert "SMOKE FAILED at step 0 (preflight)" in proc.stdout
    assert "use_model.ps1" in proc.stdout
    no_model = tmp_path / "no_model.env"
    no_model.write_text(f"AQOURS_CODE_API_KEY={SECRET}\n", encoding="utf-8")
    proc = powershell(SMOKE / "smoke.ps1", "-Root", str(root), "-EnvFile", str(no_model))
    assert proc.returncode == 1
    assert "AQOURS_CODE_MODEL is not set" in proc.stdout
    assert SECRET not in proc.stdout + proc.stderr
    assert not root.exists()  # nothing was prepared, nothing ran


# ── use_model.ps1 and .gitignore ──


@needs_powershell
def test_use_model_copies_and_prints_three_settings_only(tmp_path):
    source = tmp_path / ".env.fake"
    source.write_text(
        "# fake provider\n"
        "AQOURS_CODE_PROVIDER=openai_compatible\n"
        f"AQOURS_CODE_API_KEY={SECRET}\n"
        "AQOURS_CODE_BASE_URL=https://fake.invalid/v1 # the endpoint\n"
        "AQOURS_CODE_MODEL=first\n"
        'export AQOURS_CODE_MODEL="fake-model-2"\n'
        "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS=200000\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AQOURS_CODE_MODEL=old\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AQOURS_CODE_")}
    proc = powershell(MODELS / "use_model.ps1", "fake", "-RepoRoot", str(tmp_path), env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / ".env").read_bytes() == source.read_bytes()
    lines = proc.stdout.splitlines()
    assert lines[1:] == ["AQOURS_CODE_PROVIDER=openai_compatible",
                         "AQOURS_CODE_BASE_URL=https://fake.invalid/v1",
                         "AQOURS_CODE_MODEL=fake-model-2"]
    assert SECRET not in proc.stdout + proc.stderr
    assert "API_KEY" not in proc.stdout + proc.stderr

    proc = powershell(MODELS / "use_model.ps1", "fake", "-RepoRoot", str(tmp_path),
                      env={**env, "AQOURS_CODE_MODEL": "from-shell"})
    assert proc.returncode == 0
    assert "warning: set in this shell, and used instead of .env: AQOURS_CODE_MODEL" in proc.stdout
    assert "from-shell" not in proc.stdout


@needs_powershell
def test_use_model_leaves_env_alone_when_the_file_is_missing(tmp_path):
    (tmp_path / ".env.fake").write_text(f"AQOURS_CODE_API_KEY={SECRET}\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("AQOURS_CODE_MODEL=\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AQOURS_CODE_MODEL=old\n", encoding="utf-8")
    proc = powershell(MODELS / "use_model.ps1", "missing", "-RepoRoot", str(tmp_path))
    assert proc.returncode == 1
    assert ".env.missing not found; .env was not changed (available: fake)" in proc.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AQOURS_CODE_MODEL=old\n"
    proc = powershell(MODELS / "use_model.ps1", "..\\fake", "-RepoRoot", str(tmp_path))
    assert proc.returncode == 2 and "invalid model name" in proc.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AQOURS_CODE_MODEL=old\n"
    assert SECRET not in proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_gitignore_ignores_env_files_but_not_the_example():
    def ignored(name: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", name],
                              cwd=ROOT).returncode == 0

    assert ignored(".env") and ignored(".env.qwen") and ignored(".env.some-model")
    assert not ignored(".env.example")
