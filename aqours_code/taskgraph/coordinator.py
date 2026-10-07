"""Run a validated task graph: schedule nodes, check them, merge, and record.

Every edge means the downstream node starts only after the upstream node has
been merged into the integration branch. Workers run in threads that each
drive one node. Every git command on the shared clone runs under ``git_lock``;
a merge, its post-merge check, and a possible undo run as one unit under
``merge_lock``, and the check itself does not hold ``git_lock``.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import stat
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .context_pack import build_context_pack
from .context_metrics import node_context_metrics
from .escapes import scan_run
from .process import run_process, run_shell
from .prompting import AttemptFailure, build_node_prompt
from .repo_index import RepoIndex, build_index
from .sandbox import NO_SANDBOX, SandboxConfig
from .schema import Graph, Node, dump_graph
from .validate import ValidationReport, edit_files, unique_nodes, usable_edges, validate
from .workers import Worker, WorkerRequest, WorkerResult, write_json_atomic

TASKGRAPH_VERSION = "coordinator-v0.3"
AQOURS_SOURCE = Path(__file__).resolve().parents[2]
DEFAULT_WORKERS = 2
DEFAULT_MAX_ATTEMPTS = 2
FINAL_CHECK_TIMEOUT_S = 1800.0
HIDDEN_TESTS_TIMEOUT_S = 1800.0
HIDDEN_TESTS_DIR = "_hidden_tests"
CHECK_OUTPUT_LIMIT = 200_000


class GraphInvalid(Exception):
    """The graph failed validation and was not executed."""

    def __init__(self, report: ValidationReport):
        super().__init__("task graph failed validation")
        self.report = report


@dataclass
class RunOptions:
    """Parameters of one run."""

    out_dir: Path = Path("runs")
    workers: int = DEFAULT_WORKERS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    worker_timeout_s: float = 1800.0
    hidden_tests: Path | None = None


@dataclass
class NodeRecord:
    """Execution record of one node, as written to summary.json."""

    status: str = "pending"  # pending, running, merged, failed, skipped
    reason: str = ""
    attempts: int = 0
    worker_reasons: list[str] = field(default_factory=list)
    start_t: float | None = None
    end_t: float | None = None
    worker_time_s: float = 0.0
    check_time_s: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    changed_files: list[str] = field(default_factory=list)
    out_of_scope_files: list[str] = field(default_factory=list)
    merge_commit: str | None = None
    error: str = ""
    escape_attempts: int = 0
    escape_samples: list[dict] = field(default_factory=list)
    context_chars: int = 0  # total across attempts; individual counts below
    context_packs: list[dict] = field(default_factory=list)
    reads_outside_pack: int = 0
    calls_before_first_write: int = 0
    soft_wall_blocked: int = 0
    confirmed_reads: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    """Outcome of ``run_graph``."""

    run_dir: Path
    summary: dict


def worker_sandbox(worker: Worker) -> SandboxConfig:
    """Where the worker runs shell commands (``none`` if it does not say)."""
    sandbox = getattr(worker, "sandbox", None)
    return sandbox if isinstance(sandbox, SandboxConfig) else NO_SANDBOX


def _make_writable(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete ``path`` if it exists, including read-only git files on Windows."""
    if path.exists():
        shutil.rmtree(path, onerror=_make_writable)


def new_run_id() -> str:
    """UTC timestamp plus a 4-character random suffix."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(2)}"


class EventLog:
    """Append-only events.jsonl with timestamps relative to the run start."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since the run started."""
        return round(time.monotonic() - self._start, 3)

    def emit(self, event_type: str, **fields) -> None:
        """Append one event."""
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "t": self.elapsed(), "type": event_type, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


@dataclass
class CheckOutcome:
    """Result of running a list of shell commands."""

    ok: bool
    text: str
    duration_s: float
    results: list[dict]


def run_checks(commands: list[str], cwd: Path, timeout: float, *,
               stop_on_failure: bool = True) -> CheckOutcome:
    """Run ``commands`` in order through the shell."""
    started = time.monotonic()
    parts, results, ok = [], [], True
    for command in commands:
        if not command.strip():
            continue
        result = run_shell(command, cwd=cwd, timeout=timeout)
        status = "timeout" if result.timed_out else f"exit {result.exit_code}"
        parts.append(f"$ {command}\n[{status}, {result.duration_s:.2f}s]\n{result.output}")
        results.append({"command": command, "ok": result.ok, "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "duration_s": round(result.duration_s, 3)})
        if not result.ok:
            ok = False
            if stop_on_failure:
                break
    return CheckOutcome(ok=ok, text="\n".join(parts)[-CHECK_OUTPUT_LIMIT:],
                        duration_s=time.monotonic() - started, results=results)


class Coordinator:
    """Executes one graph in one run directory."""

    def __init__(self, graph: Graph, index: RepoIndex, worker: Worker,
                 options: RunOptions, run_dir: Path, events: EventLog,
                 base_commit: str):
        self.graph = graph
        self.index = index
        self.worker = worker
        self.options = options
        self.run_dir = run_dir
        self.repo = run_dir / "repo"
        self.events = events
        self.git_lock = threading.Lock()
        self.merge_lock = threading.Lock()
        # Last integration commit whose post-merge check passed. New worktrees
        # start here, never from a merge still being checked, so an undone
        # merge cannot leak into another node's branch.
        self.integration_head = base_commit
        self.sandbox = worker_sandbox(worker)
        self.nodes = unique_nodes(graph)
        self.by_id = {node.id: node for node in self.nodes}
        self.records = {node.id: NodeRecord() for node in self.nodes}
        self.upstream: dict[str, set[str]] = {node.id: set() for node in self.nodes}
        self.downstream: dict[str, set[str]] = {node.id: set() for node in self.nodes}
        for edge in usable_edges(graph):
            self.upstream[edge.to].add(edge.from_)
            self.downstream[edge.from_].add(edge.to)

    # ── scheduling ──

    def _is_ready(self, node_id: str) -> bool:
        return all(self.records[up].status == "merged" for up in self.upstream[node_id])

    def _skip_descendants(self, node_id: str) -> None:
        stack = list(self.downstream[node_id])
        while stack:
            current = stack.pop()
            record = self.records[current]
            if record.status != "pending":
                continue
            record.status, record.reason = "skipped", "upstream_failed"
            self.events.emit("node_skipped", node=current, reason="upstream_failed",
                             cause=node_id)
            stack.extend(self.downstream[current])

    def execute(self) -> None:
        """Run every node, respecting edges, the worker limit and edit sets."""
        announced: set[str] = set()
        running: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            while True:
                for node in self.nodes:
                    if (node.id not in announced and self.records[node.id].status == "pending"
                            and self._is_ready(node.id)):
                        announced.add(node.id)
                        self.events.emit("node_ready", node=node.id)
                busy = set().union(*(edit_files(self.by_id[n]) for n in running.values()))
                for node in self.nodes:
                    if len(running) >= self.options.workers:
                        break
                    record = self.records[node.id]
                    if (record.status != "pending" or node.id not in announced
                            or edit_files(node) & busy):
                        continue
                    record.status = "running"
                    busy |= edit_files(node)
                    running[pool.submit(self._run_node_safely, node)] = node.id
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = running.pop(future)
                    if self.records[node_id].status == "failed":
                        self._skip_descendants(node_id)
        for node_id, record in self.records.items():
            if record.status == "pending":  # unreachable after validation
                record.status, record.reason = "skipped", "not_scheduled"
                self.events.emit("node_skipped", node=node_id, reason="not_scheduled")

    # ── one node ──

    def _run_node_safely(self, node: Node) -> None:
        record = self.records[node.id]
        try:
            self._run_node(node, record)
        except Exception as exc:  # noqa: BLE001 - recorded, run continues
            record.status, record.reason = "failed", "coordinator_error"
            record.error = f"{type(exc).__name__}: {exc}"
        if record.end_t is None:
            record.end_t = self.events.elapsed()
        if record.status == "failed":
            self.events.emit("node_failed", node=node.id, reason=record.reason,
                             attempts=record.attempts, error=record.error[:2000])

    def _run_node(self, node: Node, record: NodeRecord) -> None:
        log_dir = self.run_dir / "nodes" / node.id
        log_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.run_dir / "wt" / node.id
        record.start_t = self.events.elapsed()
        self.events.emit("node_start", node=node.id)
        with self.git_lock:
            start = self.integration_head
            gitops.add_node_worktree(self.repo, node.id, worktree, start)
        try:
            if self._attempt_until_checked(node, record, log_dir, worktree, start):
                self._merge(node, record, log_dir)
        finally:
            with self.git_lock:
                branch = gitops.NODE_BRANCH_PREFIX + node.id
                record.changed_files = gitops.changed_files(self.repo, start, branch)
                (log_dir / "diff.patch").write_text(
                    gitops.diff_patch(self.repo, start, branch), encoding="utf-8")
                gitops.remove_worktree(self.repo, worktree)
            allowed = edit_files(node)
            record.out_of_scope_files = ([] if node.edit_set.any_file else
                                         [path for path in record.changed_files if path not in allowed])
            record.end_t = self.events.elapsed()

    def _attempt_until_checked(self, node: Node, record: NodeRecord, log_dir: Path,
                               worktree: Path, start: str) -> bool:
        failure: AttemptFailure | None = None
        for attempt in range(1, self.options.max_attempts + 1):
            record.attempts = attempt
            pack = build_context_pack(node, worktree)
            (log_dir / f"context_{attempt}.md").write_text(pack.text, encoding="utf-8")
            metadata = {"attempt": attempt, "workspace": str(worktree), **pack.report()}
            write_json_atomic(log_dir / f"context_{attempt}.json", metadata)
            record.context_packs.append(metadata)
            record.context_chars += len(pack.text)
            prompt = build_node_prompt(self.graph, node, self.index, attempt, failure,
                                       sandbox=self.sandbox.kind, context_pack=pack)
            (log_dir / f"prompt_{attempt}.md").write_text(prompt, encoding="utf-8")
            self.events.emit("worker_start", node=node.id, attempt=attempt)
            request = WorkerRequest(node_id=node.id, attempt=attempt, prompt=prompt,
                                    workspace=worktree, log_dir=log_dir,
                                    timeout_s=self.options.worker_timeout_s,
                                    soft_wall_enabled=len(self.graph.nodes) > 1,
                                    own_files=pack.own_files, full_files=pack.full_files)
            try:
                result = self.worker.run(request)
            except Exception as exc:  # noqa: BLE001 - a broken worker fails the attempt
                result = WorkerResult(ok=False, reason="worker_error",
                                      error=f"{type(exc).__name__}: {exc}")
            write_json_atomic(log_dir / f"worker_{attempt}.json", result.to_dict())
            record.worker_time_s += result.duration_s
            record.model_calls += result.model_calls
            record.input_tokens += result.input_tokens
            record.output_tokens += result.output_tokens
            self.events.emit("worker_end", node=node.id, attempt=attempt, ok=result.ok,
                             reason=result.reason, duration_s=round(result.duration_s, 3),
                             model_calls=result.model_calls,
                             input_tokens=result.input_tokens,
                             output_tokens=result.output_tokens)
            with self.git_lock:
                gitops.commit_all(worktree, f"attempt {attempt}")
                changed = gitops.changed_files(worktree, start)
            worker_reason = "" if result.ok else (result.reason or "worker_error")
            record.worker_reasons.append(worker_reason)
            # A worker that timed out may still have finished the work: if it
            # changed files, its checks decide. Other worker failures are final.
            if worker_reason and not (worker_reason == "worker_timeout" and changed):
                record.reason = worker_reason
                failure = AttemptFailure(record.reason, result.error or result.final_answer)
                continue
            if not changed:
                record.reason = "no_changes"
                failure = AttemptFailure("no_changes",
                                         "The attempt finished without changing any file.")
                continue
            self.events.emit("check_start", node=node.id, attempt=attempt)
            check = run_checks(node.check.commands, worktree, node.check.timeout_s)
            (log_dir / f"check_{attempt}.txt").write_text(check.text, encoding="utf-8")
            record.check_time_s += check.duration_s
            self.events.emit("check_end", node=node.id, attempt=attempt, ok=check.ok,
                             duration_s=round(check.duration_s, 3))
            if check.ok:
                record.reason = ""
                return True
            record.reason = "check_failed"
            failure = AttemptFailure("check_failed", check.text)
        record.status = "failed"
        return False

    def _merge(self, node: Node, record: NodeRecord, log_dir: Path) -> None:
        branch = gitops.NODE_BRANCH_PREFIX + node.id
        with self.merge_lock:
            with self.git_lock:
                self.events.emit("merge_start", node=node.id)
                commit = gitops.squash_merge(self.repo, branch,
                                             f"[taskgraph] {node.id}: {node.title}")
                self.events.emit("merge_end", node=node.id, ok=commit is not None,
                                 commit=commit)
            if commit is None:
                record.status, record.reason = "failed", "merge_conflict"
                return
            check = run_checks(node.check.commands, self.repo, node.check.timeout_s)
            (log_dir / "post_merge_check.txt").write_text(check.text, encoding="utf-8")
            record.check_time_s += check.duration_s
            self.events.emit("post_merge_check_end", node=node.id, ok=check.ok,
                             duration_s=round(check.duration_s, 3))
            with self.git_lock:
                if not check.ok:
                    gitops.undo_last_commit(self.repo)
                    record.status, record.reason = "failed", "post_merge_check_failed"
                    return
                self.integration_head = commit
        record.status, record.merge_commit = "merged", commit
        self.events.emit("node_merged", node=node.id, commit=commit,
                         attempts=record.attempts)

    # ── final stage ──

    def final_checks(self) -> list[dict]:
        """Run ``final_checks`` on the integration branch; record each command."""
        outcome = run_checks(self.graph.final_checks, self.repo, FINAL_CHECK_TIMEOUT_S,
                             stop_on_failure=False)
        (self.run_dir / "final_checks.txt").write_text(outcome.text, encoding="utf-8")
        self.events.emit("final_checks_end", ok=outcome.ok, results=outcome.results)
        return outcome.results

    def hidden_tests(self, tests_dir: Path) -> dict:
        """Run hidden tests on a clean checkout of the integration branch."""
        final = self.run_dir / "final"
        with self.git_lock:
            gitops.add_detached_worktree(self.repo, final, gitops.INTEGRATION_BRANCH)
        shutil.copytree(tests_dir, final / HIDDEN_TESTS_DIR)
        xml_path = self.run_dir / "hidden_tests.xml"
        result = run_process(
            [sys.executable, "-m", "pytest", "-q", HIDDEN_TESTS_DIR, "-p",
             "no:cacheprovider", f"--junitxml={xml_path}"],
            cwd=final, timeout=HIDDEN_TESTS_TIMEOUT_S)
        (self.run_dir / "hidden_tests.txt").write_text(result.output, encoding="utf-8")
        stats = parse_junit(xml_path)
        stats.update({"exit_code": result.exit_code, "timed_out": result.timed_out,
                      "duration_s": round(result.duration_s, 3)})
        self.events.emit("hidden_tests_end", **stats)
        return stats

    def audit_escapes(self) -> int:
        """Record each node's escape attempts from its traces; return the total."""
        for node_id, escapes in scan_run(self.run_dir).items():
            if node_id in self.records:
                self.records[node_id].escape_attempts = escapes.count
                self.records[node_id].escape_samples = escapes.samples()
        total = sum(record.escape_attempts for record in self.records.values())
        self.events.emit("escape_audit_end", total=total)
        return total

    def audit_context(self) -> None:
        """Record reads beyond full context and calls before each node's first write."""
        for node_id, record in self.records.items():
            metrics = node_context_metrics(self.run_dir / "nodes" / node_id,
                                           record.context_packs)
            record.reads_outside_pack = metrics["reads_outside_pack"]
            record.calls_before_first_write = metrics["calls_before_first_write"]
            record.soft_wall_blocked = metrics["soft_wall_blocked"]
            record.confirmed_reads = metrics["confirmed_reads"]

    def remove_answers(self) -> None:
        """Delete what a later worker must not find: hidden tests and worktrees.

        ``final/`` keeps the final code; ``nodes/`` keeps diffs, logs, traces.
        """
        remove_tree(self.run_dir / "final" / HIDDEN_TESTS_DIR)
        remove_tree(self.run_dir / "wt")
        with self.git_lock:
            gitops.git(self.repo, "worktree", "prune", check=False)


def parse_junit(path: Path) -> dict:
    """Count passed, failed, errors and skipped tests in a JUnit XML file."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {**counts, "parse_error": str(exc)}
    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    total = 0
    for suite in suites:
        total += int(suite.get("tests", 0))
        counts["failed"] += int(suite.get("failures", 0))
        counts["errors"] += int(suite.get("errors", 0))
        counts["skipped"] += int(suite.get("skipped", 0))
    counts["passed"] = total - counts["failed"] - counts["errors"] - counts["skipped"]
    return counts


def run_status(records: dict[str, NodeRecord], final_results: list[dict]) -> str:
    """success, partial, or failed (hidden tests do not count)."""
    merged = [record for record in records.values() if record.status == "merged"]
    if not merged:
        return "failed"
    if len(merged) == len(records) and all(item["ok"] for item in final_results):
        return "success"
    return "partial"


def run_graph(graph: Graph, repo: Path, worker: Worker,
              options: RunOptions | None = None) -> RunResult:
    """Validate and execute ``graph`` against ``repo``; return the run summary.

    Raises ``GraphInvalid`` (nothing is executed) when validation reports an
    error, and ``gitops.GitError`` / ``RuntimeError`` on git or index failures.
    """
    options = options or RunOptions()
    repo = Path(repo).resolve()
    index = build_index(repo, graph.base_commit)
    report = validate(graph, index)
    if not report.ok:
        raise GraphInvalid(report)
    preflight = getattr(worker, "preflight", None)
    if preflight is not None:
        preflight()  # SandboxUnavailable (a RuntimeError) stops the run here
    sandbox = worker_sandbox(worker)

    run_id = new_run_id()
    run_dir = Path(options.out_dir).resolve() / run_id
    run_dir.mkdir(parents=True)
    dump_graph(graph, run_dir / "graph.json")
    config = {
        "run_id": run_id,
        "repo": str(repo),
        "base_commit": graph.base_commit,
        "request_id": graph.request_id,
        "workers": options.workers,
        "max_attempts": options.max_attempts,
        "worker_timeout_s": options.worker_timeout_s,
        "hidden_tests": str(options.hidden_tests) if options.hidden_tests else None,
        "worker": worker.describe(),
        **sandbox.to_dict(),
        "taskgraph_version": TASKGRAPH_VERSION,
        "aqours_commit": gitops.source_state(AQOURS_SOURCE),
        "validation_warnings": [issue.format() for issue in report.warnings],
        "python": platform.python_version(),
        "platform": sys.platform,
    }
    write_json_atomic(run_dir / "config.json", config)
    events = EventLog(run_dir / "events.jsonl")
    events.emit("run_start", run_id=run_id, workers=options.workers,
                nodes=[node.id for node in unique_nodes(graph)])

    base = gitops.clone_for_run(repo, run_dir / "repo", graph.base_commit)
    coordinator = Coordinator(graph, index, worker, options, run_dir, events, base)
    coordinator.execute()
    final_results = coordinator.final_checks()
    hidden = (coordinator.hidden_tests(Path(options.hidden_tests))
              if options.hidden_tests else None)
    escapes_total = coordinator.audit_escapes()
    coordinator.audit_context()
    coordinator.remove_answers()
    status = run_status(coordinator.records, final_results)
    wall_time = events.elapsed()
    events.emit("run_end", status=status, wall_time_s=wall_time)

    records = coordinator.records
    summary = {
        "run_id": run_id,
        "status": status,
        "wall_time_s": wall_time,
        "config": config,
        "nodes": {node_id: asdict(record) for node_id, record in records.items()},
        "totals": {
            "model_calls": sum(r.model_calls for r in records.values()),
            "input_tokens": sum(r.input_tokens for r in records.values()),
            "output_tokens": sum(r.output_tokens for r in records.values()),
            "context_chars": sum(r.context_chars for r in records.values()),
            "soft_wall_blocked": sum(r.soft_wall_blocked for r in records.values()),
            "confirmed_reads": sum(len(r.confirmed_reads) for r in records.values()),
            "reads_outside_pack": sum(r.reads_outside_pack for r in records.values()),
            "calls_before_first_write": sum(r.calls_before_first_write for r in records.values()),
        },
        "integration_commit": gitops.head(run_dir / "repo"),
        "final_checks": final_results,
        "hidden_tests": hidden,
        **sandbox.to_dict(),
        "escape_attempts_total": escapes_total,
    }
    write_json_atomic(run_dir / "summary.json", summary)
    return RunResult(run_dir=run_dir, summary=summary)
