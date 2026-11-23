"""Repository preparation and result check of the model smoke test.

    python smoke.py prepare <dest>
    python smoke.py check <run_dir> [--env-file <.env>] [--json <file>]

``smoke.ps1`` calls both; neither calls a model.

``prepare`` copies ``template/`` to ``<dest>`` with LF line endings, makes it a
git repository and commits once with a fixed author, date and message, so the
commit is the same on every platform; it prints the commit.

``check`` reads a finished (or broken) ``taskgraph run`` directory and prints
what the smoke test is for: the model in ``.env`` and the one the run used,
the output limits of the run (initial and escalated max_tokens), the
thinking level it asked for (reasoning effort), the OpenRouter provider its
requests were pinned to and the providers that actually served them (with how
many replies each), the run status, model calls
and tokens, whether the final check passed, whether ``mul`` was implemented and tested, and how often the logs show an
HTTP error on a model request, a reply cut at ``max_tokens``, a tool input
that did not parse (``_raw``), and an escape attempt. It ends with the four
things the smoke test confirms (README.md) and exits 0 if all of them hold.
It reads only ``AQOURS_CODE_MODEL`` of ``.env``.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
AQOURS_ROOT = HERE.parents[3]
if str(AQOURS_ROOT) not in sys.path:
    sys.path.insert(0, str(AQOURS_ROOT))

from aqours_code.taskgraph.escapes import attempt_traces, scan_run  # noqa: E402
from aqours_code.taskgraph.gate import _dotenv_value  # noqa: E402

TEMPLATE = HERE / "template"
OPS = "calc/ops.py"
TESTS = "tests/test_ops.py"
COMMIT_MESSAGE = "model smoke test base"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Model Smoke Test",
    "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Model Smoke Test",
    "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
INTEGRATION_BRANCH = "tg/integration"

EXIT_PASS, EXIT_FAIL, EXIT_INPUT_ERROR = 0, 1, 2
SAMPLES = 3
SAMPLE_CHARS = 200

# "Model request failed: HTTP 429: ..." (OpenAI-compatible providers),
# "Error code: 429 - {...}" (anthropic SDK), "provider_http_429" (broker)
_HTTP_IN_ERROR = re.compile(r"\bHTTP (\d{3})\b|\bError code: (\d{3})\b|\bprovider_http_(\d{3})\b")
# recovery.with_retry: "[429] retry 1/5 after ...", "[provider_http_503] retry ..."
_HTTP_RETRY = re.compile(r"\[(?:(\d{3})|provider_http_(\d{3}))\] retry\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ATTEMPT_FILE = re.compile(r"^(?:trace_(\d+)\.jsonl|worker_(\d+)(?:_config)?\.json)$")
# anything that might be a key in a provider's error message
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{4,}|[A-Za-z0-9_\-]{32,}")


# ── prepare ──


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a configuration independent of the user's."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", *args],
        cwd=repo, env={**os.environ, **GIT_ENV}, capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.decode("utf-8").strip()


def template_files() -> list[Path]:
    """The template's files, relative to ``template/``, without caches."""
    skip = {"__pycache__", ".pytest_cache"}
    return sorted(path.relative_to(TEMPLATE) for path in TEMPLATE.rglob("*")
                  if path.is_file() and not skip & set(path.parts) and path.suffix != ".pyc")


def prepare(dest: Path) -> str:
    """Create the smoke repository at ``dest``; return its commit."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    for relative in template_files():
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((TEMPLATE / relative).read_bytes().replace(b"\r\n", b"\n"))
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", COMMIT_MESSAGE)
    return git(dest, "rev-parse", "HEAD")


# ── check: the code ──


def _is_stub(body: list[ast.stmt]) -> bool:
    """True if a function body does nothing: a docstring, pass, ... or NotImplementedError."""
    statements = list(body)
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]
    for statement in statements:
        if isinstance(statement, ast.Pass):
            continue
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                and statement.value.value is Ellipsis):
            continue
        if isinstance(statement, ast.Raise) and statement.exc is not None:
            exc = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                continue
        return False
    return True


def mul_implemented(source: str | None) -> tuple[bool, str]:
    """Whether ``calc/ops.py`` defines a ``mul`` of two arguments that is not a stub."""
    if source is None:
        return False, f"{OPS} is missing"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"{OPS} does not parse: {exc.msg} (line {exc.lineno})"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "mul":
            args = node.args
            positional = len(args.posonlyargs) + len(args.args)
            if positional < 2 and args.vararg is None:
                return False, f"mul takes {positional} positional argument(s), not 2"
            if _is_stub(node.body):
                return False, "mul is a stub (no implementation)"
            return True, f"def mul in {OPS}"
    return False, f"no top-level def mul in {OPS}"


def _calls_mul(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if ((isinstance(func, ast.Name) and func.id == "mul")
                    or (isinstance(func, ast.Attribute) and func.attr == "mul")):
                return True
    return False


def mul_tests(source: str | None) -> list[str]:
    """The tests in ``tests/test_ops.py`` that call ``mul``."""
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if _calls_mul(node):
                found.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test") and _calls_mul(item)):
                    found.append(f"{node.name}.{item.name}")
    return found


def _blob(repo: Path, commit: str, path: str) -> str | None:
    proc = subprocess.run(["git", "cat-file", "blob", f"{commit}:{path}"], cwd=repo,
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _changed(repo: Path, base: str, commit: str) -> list[str]:
    proc = subprocess.run(["git", "-c", "core.quotepath=off", "diff", "--name-only",
                           base, commit], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        return []
    return sorted(line for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
                  if line)


# ── check: the logs ──


def _redact(text: str) -> str:
    text = " ".join(_SECRET.sub("<redacted>", str(text)).split())
    return text if len(text) <= SAMPLE_CHARS else text[:SAMPLE_CHARS - 3] + "..."


@dataclass
class LogScan:
    """What the node logs of one run show."""

    trace_files: int = 0
    stdout_files: int = 0
    tool_calls: int = 0
    pytest_calls: int = 0
    http_errors: int = 0
    http_statuses: dict = field(default_factory=dict)
    http_samples: list = field(default_factory=list)
    max_tokens: int = 0
    raw_inputs: int = 0
    raw_samples: list = field(default_factory=list)
    escape_attempts: int = 0
    escape_samples: list = field(default_factory=list)
    # the provider each reply names (OpenRouter's "provider"): {name: replies}
    served_by: dict = field(default_factory=dict)
    replies: int = 0  # llm_response events, named or not


def _attempts(node_dir: Path) -> list[int]:
    numbers = set()
    for path in node_dir.iterdir():
        match = _ATTEMPT_FILE.match(path.name)
        if match:
            numbers.add(int(match.group(1) or match.group(2)))
    return sorted(numbers)


def _scan_trace(path: Path, scan: LogScan, statuses: Counter, served: Counter) -> None:
    last_error = None  # an error recorded again as it propagates is one error
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "llm_request":
            last_error = None
        elif kind == "llm_response":
            if event.get("stop_reason") == "max_tokens":
                scan.max_tokens += 1
            scan.replies += 1
            provider = event.get("provider")
            if isinstance(provider, str) and provider:
                served[provider] += 1
        elif kind == "tool_use":
            scan.tool_calls += 1
            tool_input = event.get("input")
            if isinstance(tool_input, dict) and "_raw" in tool_input:
                scan.raw_inputs += 1
                if len(scan.raw_samples) < SAMPLES:
                    scan.raw_samples.append(
                        f"{event.get('tool')}: {_redact(tool_input['_raw'])}")
            elif (event.get("tool") == "bash" and isinstance(tool_input, dict)
                  and "pytest" in str(tool_input.get("command", ""))):
                scan.pytest_calls += 1
        elif kind == "error":
            message = str(event.get("message", ""))
            match = _HTTP_IN_ERROR.search(message)
            if match is None or message == last_error:
                continue
            last_error = message
            status = next(group for group in match.groups() if group)
            scan.http_errors += 1
            statuses[status] += 1
            if len(scan.http_samples) < SAMPLES:
                scan.http_samples.append(_redact(message))


def _scan_stdout(path: Path, scan: LogScan, statuses: Counter) -> None:
    text = _ANSI.sub("", path.read_text(encoding="utf-8", errors="replace"))
    for line in text.splitlines():
        match = _HTTP_RETRY.search(line)
        if match:
            status = match.group(1) or match.group(2)
            scan.http_errors += 1
            statuses[status] += 1
            if len(scan.http_samples) < SAMPLES:
                scan.http_samples.append(f"retried: {_redact(line.strip())}")


def scan_logs(run_dir: Path) -> LogScan:
    """Scan every attempt's trace and stdout under ``nodes/``."""
    scan = LogScan()
    statuses: Counter = Counter()
    served: Counter = Counter()
    nodes = run_dir / "nodes"
    for node_dir in sorted(nodes.iterdir()) if nodes.is_dir() else []:
        if not node_dir.is_dir():
            continue
        for attempt in _attempts(node_dir):
            for trace in attempt_traces(node_dir, attempt):
                scan.trace_files += 1
                _scan_trace(trace, scan, statuses, served)
            stdout = node_dir / f"worker_{attempt}_stdout.txt"
            if stdout.is_file():
                scan.stdout_files += 1
                _scan_stdout(stdout, scan, statuses)
    scan.http_statuses = dict(sorted(statuses.items()))
    scan.served_by = dict(served.most_common())
    for node_id, escapes in scan_run(run_dir).items():
        scan.escape_attempts += escapes.count
        for sample in escapes.samples():
            if len(scan.escape_samples) < SAMPLES:
                scan.escape_samples.append(
                    f"{node_id}: {sample['tool']} {sample['detail']} "
                    f"({', '.join(sample['reasons'])})")
    return scan


# ── check: the run ──


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _worker_results(run_dir: Path) -> dict[str, list[dict]]:
    """Each node's worker_<n>.json results, by attempt (for a run without summary.json)."""
    results = {}
    nodes = run_dir / "nodes"
    for node_dir in sorted(nodes.iterdir()) if nodes.is_dir() else []:
        if node_dir.is_dir():
            results[node_dir.name] = [
                data for n in _attempts(node_dir)
                if (data := _read_json(node_dir / f"worker_{n}.json")) is not None]
    return results


def _ending(nodes: dict) -> tuple[bool, str]:
    """Whether every node's last worker attempt gave an answer (reason ""), and how they ended."""
    if not nodes:
        return False, "no node ran"
    endings = {node_id: (node["worker_reasons"][-1] if node["worker_reasons"] else "no attempt")
               for node_id, node in nodes.items()}
    failed = {node_id: reason for node_id, reason in endings.items() if reason != ""}
    if not failed:
        return True, "the agent answered (no worker error or timeout)"
    return False, "; ".join(f"{node_id}: {reason}" for node_id, reason in failed.items())


@dataclass
class Check:
    """The result of ``check``; also written as JSON."""

    run_dir: str
    env_model: str | None
    run_provider: str | None
    run_model: str | None
    max_tokens: dict | None  # {"initial", "escalated"} of the run's worker, if recorded
    max_tokens_note: str
    # the thinking level the run's worker asked for (None: none), if recorded
    reasoning_effort: str | None
    reasoning_effort_recorded: bool
    reasoning_effort_note: str
    # the OpenRouter providers the run's worker was pinned to (None: not
    # pinned), if recorded; who served the replies is in logs.served_by
    openrouter_provider: list | None
    openrouter_provider_recorded: bool
    openrouter_provider_note: str
    summary_found: bool
    status: str
    nodes: dict
    model_calls: int
    input_tokens: int
    output_tokens: int
    final_checks: list
    final_check_passed: bool
    changed_files: list
    mul_implemented: bool
    mul_detail: str
    mul_tests: list
    logs: LogScan
    criteria: dict
    passed: bool


def check(run_dir: Path, env_file: Path | None = None) -> Check:
    """Check one run directory."""
    run_dir = Path(run_dir)
    env_file = AQOURS_ROOT / ".env" if env_file is None else Path(env_file)
    env_model = _dotenv_value(env_file, "AQOURS_CODE_MODEL")
    summary = _read_json(run_dir / "summary.json")
    config = (summary or {}).get("config") or _read_json(run_dir / "config.json") or {}
    worker = config.get("worker") or {}

    if summary is not None:
        status = str(summary.get("status", "unknown"))
        nodes = {node_id: {"status": node.get("status"), "reason": node.get("reason", ""),
                           "attempts": node.get("attempts", 0),
                           "worker_reasons": node.get("worker_reasons", [])}
                 for node_id, node in (summary.get("nodes") or {}).items()}
        totals = summary.get("totals") or {}
        calls, tokens_in, tokens_out = (int(totals.get(key, 0)) for key in
                                        ("model_calls", "input_tokens", "output_tokens"))
        final_checks = list(summary.get("final_checks") or [])
        commit = summary.get("integration_commit") or INTEGRATION_BRANCH
    else:
        status = "no summary.json (the run did not finish)"
        results = _worker_results(run_dir)
        nodes = {node_id: {"status": "unknown", "reason": "", "attempts": len(items),
                           "worker_reasons": [item.get("reason", "") for item in items]}
                 for node_id, items in results.items()}
        items = [item for node in results.values() for item in node]
        calls, tokens_in, tokens_out = (sum(int(item.get(key, 0)) for item in items) for key in
                                        ("model_calls", "input_tokens", "output_tokens"))
        final_checks = []
        commit = INTEGRATION_BRANCH
    final_passed = bool(final_checks) and all(item.get("ok") for item in final_checks)

    repo = run_dir / "repo"
    base = config.get("base_commit")
    if repo.is_dir():
        ops, tests = _blob(repo, commit, OPS), _blob(repo, commit, TESTS)
        changed = _changed(repo, base, commit) if base else []
    else:
        ops = tests = None
        changed = []
    implemented, detail = mul_implemented(ops)
    if not repo.is_dir():
        detail = "no repo/ in the run directory"
    tested = mul_tests(tests)

    logs = scan_logs(run_dir)
    answered, ending = _ending(nodes)
    if summary is None:
        answered, ending = False, f"the run did not finish (no summary.json); {ending}"
    criteria = {
        "tool calls work": (logs.tool_calls > 0 and logs.raw_inputs == 0,
                            f"{logs.tool_calls} tool call(s), {logs.raw_inputs} unparsed"),
        "edits files": (implemented and bool(tested),
                        (f"mul implemented and tested ({', '.join(tested)})"
                         if implemented and tested else
                         detail if not implemented else f"no test in {TESTS} calls mul")),
        "runs tests": (logs.pytest_calls > 0 and final_passed,
                       f"the agent ran pytest {logs.pytest_calls} time(s); the final check "
                       + ("passed" if final_passed else "did not pass")),
        "finishes normally": (answered, ending),
    }
    passed = status == "success" and all(ok for ok, _ in criteria.values())
    max_tokens = worker.get("max_tokens")
    if not isinstance(max_tokens, dict):
        max_tokens = None
    note = str(worker.get("max_tokens_error") or "")
    effort = worker.get("reasoning_effort")
    pinned = worker.get("openrouter_provider")
    return Check(run_dir=str(run_dir), env_model=env_model,
                 run_provider=worker.get("model_provider"), run_model=worker.get("model"),
                 max_tokens=max_tokens, max_tokens_note=note,
                 reasoning_effort=str(effort) if effort is not None else None,
                 reasoning_effort_recorded="reasoning_effort" in worker,
                 reasoning_effort_note=str(worker.get("reasoning_effort_error") or ""),
                 openrouter_provider=([str(item) for item in pinned]
                                      if isinstance(pinned, list) else None),
                 openrouter_provider_recorded="openrouter_provider" in worker,
                 openrouter_provider_note=str(worker.get("openrouter_provider_error") or ""),
                 summary_found=summary is not None, status=status, nodes=nodes,
                 model_calls=calls, input_tokens=tokens_in, output_tokens=tokens_out,
                 final_checks=final_checks, final_check_passed=final_passed,
                 changed_files=changed, mul_implemented=implemented, mul_detail=detail,
                 mul_tests=tested, logs=logs,
                 criteria={name: {"ok": ok, "detail": text}
                           for name, (ok, text) in criteria.items()},
                 passed=passed)


def _replies(count: int) -> str:
    return f"{count} reply" if count == 1 else f"{count} replies"


def format_check(result: Check) -> str:
    """The report that smoke.ps1 prints."""
    logs = result.logs
    lines = [f"Smoke check of {result.run_dir}"]
    run_model = (f"{result.run_provider} / {result.run_model}" if result.run_model
                 else "unknown (no config.json)")
    lines.append(f"  model in .env      : {result.env_model or '(not set)'}")
    lines.append(f"  model of the run   : {run_model}")
    if result.max_tokens is not None:
        limits = (f"initial {result.max_tokens.get('initial')}, "
                  f"escalated {result.max_tokens.get('escalated')}")
    elif result.max_tokens_note:
        limits = f"invalid: {result.max_tokens_note}"
    else:
        limits = "not recorded (config.json of the run has no worker.max_tokens)"
    lines.append(f"  output limits      : {limits}")
    if result.reasoning_effort_note:
        effort = f"invalid: {result.reasoning_effort_note}"
    elif result.reasoning_effort is not None:
        effort = result.reasoning_effort
    elif result.reasoning_effort_recorded:
        effort = "none sent (the provider's default)"
    else:
        effort = "not recorded (config.json of the run has no worker.reasoning_effort)"
    lines.append(f"  reasoning effort   : {effort}")
    if result.openrouter_provider_note:
        pinned = f"invalid: {result.openrouter_provider_note}"
    elif result.openrouter_provider:
        pinned = f"{', '.join(result.openrouter_provider)} (no fallbacks)"
    elif result.openrouter_provider_recorded:
        pinned = "none (OpenRouter picks the provider of each request)"
    else:
        pinned = "not recorded (config.json of the run has no worker.openrouter_provider)"
    lines.append(f"  provider set       : {pinned}")
    served = ", ".join(f"{name} x{count}" for name, count in logs.served_by.items())
    unnamed = logs.replies - sum(logs.served_by.values())
    if not served:
        served = f"none named in the traces ({_replies(logs.replies)})"
    elif unnamed:
        served += f"; {_replies(unnamed)} named none"
    lines.append(f"  provider served    : {served}")
    if result.run_model and result.env_model and result.run_model != result.env_model:
        lines.append("  warning: the run did not use the model in .env (an AQOURS_CODE_* "
                     "environment variable wins over .env)")
    node_text = "; ".join(
        f"{node_id}: {node['status']}"
        + (f" ({node['reason']})" if node["reason"] else "")
        + f", attempts {node['attempts']}" for node_id, node in result.nodes.items())
    lines.append(f"  run status         : {result.status}"
                 + (f"  [{node_text}]" if node_text else ""))
    lines.append(f"  model calls        : {result.model_calls}")
    lines.append(f"  tokens             : {result.input_tokens} in / {result.output_tokens} out")
    if result.final_checks:
        lines.append("  final check        : "
                     + ("passed" if result.final_check_passed else "FAILED"))
        for item in result.final_checks:
            state = "timeout" if item.get("timed_out") else f"exit {item.get('exit_code')}"
            lines.append(f"      {'ok  ' if item.get('ok') else 'FAIL'} {item.get('command')}"
                         f"  ({state})")
    else:
        lines.append("  final check        : not run")
    lines.append(f"  mul implemented    : {'yes' if result.mul_implemented else 'NO'}"
                 f"  ({result.mul_detail})")
    lines.append(f"  mul tested         : "
                 + (f"yes  ({', '.join(result.mul_tests)})" if result.mul_tests
                    else f"NO  (no test in {TESTS} calls mul)"))
    lines.append(f"  files changed      : {', '.join(result.changed_files) or 'none'}")
    lines.append(f"Logs ({logs.trace_files} trace file(s), {logs.stdout_files} stdout file(s)):")
    statuses = ", ".join(f"HTTP {code} x{count}" for code, count in logs.http_statuses.items())
    rows = [
        ("HTTP errors on model requests", logs.http_errors,
         f"  ({statuses})" if statuses else "", logs.http_samples),
        ("replies cut at max_tokens", logs.max_tokens, "", []),
        ("tool inputs that did not parse (_raw)", logs.raw_inputs, "", logs.raw_samples),
        ("escape attempts", logs.escape_attempts, "", logs.escape_samples),
    ]
    for label, count, extra, samples in rows:
        lines.append(f"  {label:<38}: {count}{extra}")
        lines.extend(f"      {sample}" for sample in samples)
    lines.append(f"Smoke result: {'PASS' if result.passed else 'FAIL'}")
    for name, item in result.criteria.items():
        lines.append(f"  [{'ok' if item['ok'] else 'FAIL':<4}] {name:<17}: {item['detail']}")
    if not result.passed and all(item["ok"] for item in result.criteria.values()):
        lines.append(f"  [FAIL] run status       : {result.status}")
    return "\n".join(lines)


# ── command line ──


def main(argv: list[str] | None = None) -> int:
    """``prepare`` or ``check``; see the module docstring."""
    parser = argparse.ArgumentParser(prog="smoke.py")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = commands.add_parser("prepare", help="create the smoke repository")
    prepare_cmd.add_argument("dest")
    check_cmd = commands.add_parser("check", help="check a taskgraph run directory")
    check_cmd.add_argument("run_dir")
    check_cmd.add_argument("--env-file", help="the .env to read AQOURS_CODE_MODEL from "
                                              "(default: the repository's .env)")
    check_cmd.add_argument("--json", help="also write the result as JSON to this file")
    args = parser.parse_args(argv)

    if args.command == "prepare":
        try:
            print(prepare(Path(args.dest)))
        except (OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        return EXIT_PASS

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"error: run directory not found: {run_dir}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    result = check(run_dir, Path(args.env_file) if args.env_file else None)
    print(format_check(result))
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    return EXIT_PASS if result.passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
