from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = PROJECT_ROOT / ".codepilot" / "stress_workspace"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / ".codepilot" / "stress_reports"
SUPPORTED_STAGES = {
    "all",
    "smoke",
    "large-file",
    "large-dir",
    "long-history",
    "multi-tool",
    "trace-retention",
    "fault-injection",
}


@dataclass
class StressCase:
    name: str
    prompt: str
    stage: str
    expected_status: str | None = None
    allowed_statuses: tuple[str, ...] | None = None
    timeout: float | None = None
    max_tools: int | None = None
    history_turns: int = 0
    initial_messages: list[dict] = field(default_factory=list)
    estimated_context_size: int = 0
    expected_terms: list[str] = field(default_factory=list)
    expected_created_files: int = 0
    notes: str = ""


@dataclass
class StressResult:
    case_name: str
    stage: str
    prompt: str
    run_id: str | None = None
    status: str = "unknown"
    duration_ms: int = 0
    tool_count: int = 0
    error_count: int = 0
    blocked_count: int = 0
    trace_size: int = 0
    timeline_size: int = 0
    file_count: int = 0
    created_files: int = 0
    round_count: int = 0
    history_turns: int = 0
    estimated_context_size: int = 0
    final_answer_preview: str = ""
    compact_count: int = 0
    tool_result_truncated: bool = False
    fake_run_count: int = 0
    deleted_count: int = 0
    kept_count: int = 0
    pinned_count: int = 0
    total_size_before: int = 0
    total_size_after: int = 0
    index_count_before: int = 0
    index_count_after: int = 0
    inconsistencies: int = 0
    timed_out: bool = False
    passed: bool = False
    outcome: str = "FAIL"
    reason: str = ""
    notes: str = ""


@dataclass
class StressReport:
    started_at: str
    workspace: str
    report_dir: str
    mock: bool
    stage: str
    requested_runs: int
    runtime_context: dict = field(default_factory=dict)
    stage_details: dict = field(default_factory=dict)
    total_runs: int = 0
    success_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0
    timeout_count: int = 0
    average_duration_ms: int = 0
    max_duration_ms: int = 0
    average_tool_count: float = 0
    max_tool_count: int = 0
    max_rounds: int = 0
    average_trace_size: int = 0
    max_trace_size: int = 0
    average_timeline_size: int = 0
    max_timeline_size: int = 0
    compact_count: int = 0
    tool_result_truncated_count: int = 0
    cleanup_deleted_count: int = 0
    fake_run_count: int = 0
    kept_count: int = 0
    pinned_count: int = 0
    total_size_before: int = 0
    total_size_after: int = 0
    index_count_before: int = 0
    index_count_after: int = 0
    inconsistencies: int = 0
    error_count: int = 0
    notes: list[str] = field(default_factory=list)
    results: list[StressResult] = field(default_factory=list)


class MockMessages:
    def __init__(self, stage: str = "smoke"):
        self.calls = 0
        self.stage = stage

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages") or []
        prompt = str(messages[-1].get("content", "") if messages else "").lower()
        last_content = messages[-1].get("content") if messages else None
        if isinstance(last_content, list) and any(
                "multi_tool" in str(item).lower() or "reports" in str(item).lower()
                for item in last_content):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(
                    type="text",
                    text="multi-tool stress case completed successfully.",
                )],
            )
        if "summarize this coding-agent conversation" in prompt:
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(
                    type="text",
                    text=("Earlier long-history constraints included alpha, beta, "
                          "gamma, delta, epsilon, zeta, eta, and theta."),
                )],
            )
        if self.stage in {"all", "long-history"}:
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(
                    type="text",
                    text=("I remember these key constraints: alpha, beta, gamma, "
                          "delta, epsilon, zeta, eta, theta."),
                )],
            )
        if self.stage in {"all", "multi-tool"}:
            if messages and isinstance(messages[-1].get("content"), list):
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(
                        type="text",
                        text="multi-tool stress case completed successfully.",
                    )],
                )
            if "multi_tool_files" in prompt or "20" in prompt:
                content = [SimpleNamespace(type="text", text="I will create and inspect numbered files.")]
                content.append(SimpleNamespace(
                    type="tool_use",
                    id="toolu_numbered_todo",
                    name="todo_write",
                    input={"todos": [
                        {"content": "Create 20 numbered files", "status": "in_progress"},
                        {"content": "Read files 1, 10, and 20", "status": "pending"},
                        {"content": "Summarize selected contents", "status": "pending"},
                    ]},
                ))
                for index in range(1, 21):
                    content.append(SimpleNamespace(
                        type="tool_use",
                        id=f"toolu_write_{index}",
                        name="write_file",
                        input={
                            "path": f"multi_tool_files/file_{index:02d}.txt",
                            "content": f"multi-tool file number {index}\n",
                        },
                    ))
                for index in (1, 10, 20):
                    content.append(SimpleNamespace(
                        type="tool_use",
                        id=f"toolu_read_{index}",
                        name="read_file",
                        input={"path": f"multi_tool_files/file_{index:02d}.txt"},
                    ))
                return SimpleNamespace(stop_reason="tool_use", content=content)
            if "reports" in prompt:
                content = [SimpleNamespace(type="text", text="I will create and inspect reports.")]
                content.append(SimpleNamespace(
                    type="tool_use",
                    id="toolu_reports_todo",
                    name="todo_write",
                    input={"todos": [
                        {"content": "Create 5 markdown reports", "status": "in_progress"},
                        {"content": "List reports directory", "status": "pending"},
                        {"content": "Read and summarize reports", "status": "pending"},
                    ]},
                ))
                for index in range(1, 6):
                    content.append(SimpleNamespace(
                        type="tool_use",
                        id=f"toolu_report_write_{index}",
                        name="write_file",
                        input={
                            "path": f"reports/report_{index}.md",
                            "content": f"# Report {index}\n\nSummary line {index}.\n",
                        },
                    ))
                content.append(SimpleNamespace(
                    type="tool_use",
                    id="toolu_reports_glob",
                    name="glob",
                    input={"pattern": "reports/*.md"},
                ))
                for index in range(1, 6):
                    content.append(SimpleNamespace(
                        type="tool_use",
                        id=f"toolu_report_read_{index}",
                        name="read_file",
                        input={"path": f"reports/report_{index}.md"},
                    ))
                return SimpleNamespace(stop_reason="tool_use", content=content)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(
                    type="text",
                    text="multi-tool stress case completed successfully.",
                )],
            )
        if self.stage in {"all", "large-file", "large-dir"}:
            if messages and isinstance(messages[-1].get("content"), list):
                return SimpleNamespace(
                    stop_reason="end_turn",
                    content=[SimpleNamespace(
                        type="text",
                        text="stress case completed; tool output was inspected and summarized.",
                    )],
                )
            if "stress_files" in prompt:
                return SimpleNamespace(
                    stop_reason="tool_use",
                    content=[
                        SimpleNamespace(type="text", text="I will list stress_files."),
                        SimpleNamespace(
                            type="tool_use",
                            id=f"toolu_stress_{self.calls}",
                            name="glob",
                            input={"pattern": "stress_files/*.txt"},
                        ),
                    ],
                )
            path = None
            if "stress_large_5mb" in prompt:
                path = "stress_large_5mb.txt"
            elif "stress_large_log" in prompt:
                path = "stress_large_log.txt"
            elif "stress_large_1mb" in prompt:
                path = "stress_large_1mb.txt"
            if path:
                return SimpleNamespace(
                    stop_reason="tool_use",
                    content=[
                        SimpleNamespace(type="text", text=f"I will inspect {path}."),
                        SimpleNamespace(
                            type="tool_use",
                            id=f"toolu_stress_{self.calls}",
                            name="read_file",
                            input={"path": path},
                        ),
                    ],
                )
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="stress smoke ok")],
        )


class MockClient:
    def __init__(self, stage: str = "smoke"):
        self.messages = MockMessages(stage)


def parse_args():
    parser = argparse.ArgumentParser(description="Run agent stress tests.")
    parser.add_argument("--stage", default="all",
                        help="Stage to run: all, smoke, large-file, large-dir, long-history, multi-tool, trace-retention, fault-injection")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of times to repeat selected cases.")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE),
                        help="Base workspace for stress test data.")
    parser.add_argument("--mock", action="store_true",
                        help="Use a mocked LLM client for dry-run/offline testing.")
    parser.add_argument("--real", action="store_true",
                        help="Use the configured real model client. This is now the default.")
    parser.add_argument("--timeout", type=float, default=60,
                        help="Per-case timeout budget in seconds. Stage 1 records overruns but does not kill running calls.")
    parser.add_argument("--large-dir-files", type=int, default=1000,
                        help="Number of small files to create for the large-dir stage.")
    parser.add_argument("--history-turns", type=int, default=60,
                        help="Number of prior user/assistant turns to synthesize for long-history.")
    parser.add_argument("--fake-runs", type=int, default=100,
                        help="Number of fake runs to create for the trace-retention stage.")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="Keep the per-run stress workspace for inspection.")
    return parser.parse_args()


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def ensure_project_importable():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def estimate_messages_size(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, default=str))


def build_long_history_messages(turns: int) -> tuple[list[dict], list[str]]:
    tokens = [
        "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
        "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi",
        "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
    ]
    messages = []
    for index in range(1, max(1, turns) + 1):
        token = tokens[(index - 1) % len(tokens)]
        filler = (f"context-padding-{index}-{token} " * 120).strip()
        messages.append({
            "role": "user",
            "content": (
                f"记住约束 {index}: 回答里必须提到 {token}. "
                f"这是长历史压力测试填充文本: {filler}"
            ),
        })
        messages.append({
            "role": "assistant",
            "content": f"已记住约束 {index}: {token}.",
        })
    return messages, tokens[:8]


def build_cases(stage: str, timeout: float,
                large_dir_files: int = 1000,
                history_turns: int = 60) -> tuple[list[StressCase], list[str]]:
    notes = []
    if stage not in SUPPORTED_STAGES:
        return [], [f"Unknown stage: {stage}"]

    cases = []
    if stage in {"all", "smoke"}:
        cases.append(StressCase(
            name="smoke_direct_answer",
            stage="smoke",
            prompt="Direct answer: stress smoke ok",
            expected_status="success",
            allowed_statuses=("success",),
            timeout=timeout,
            notes="Stage 1 smoke case: no tool use expected.",
        ))

    if stage in {"all", "large-file"}:
        cases.extend([
            StressCase(
                name="large_file_1mb_read",
                stage="large-file",
                prompt=("Use the read_file tool to read stress_large_1mb.txt, "
                        "then summarize what it is testing."),
                allowed_statuses=("success", "failed"),
                timeout=timeout,
                notes="Reads a 1MB text file and checks trace/timeline behavior.",
            ),
            StressCase(
                name="large_file_5mb_read",
                stage="large-file",
                prompt=("Use the read_file tool to read stress_large_5mb.txt, "
                        "then give a brief stability summary."),
                allowed_statuses=("success", "failed"),
                timeout=timeout,
                notes="Reads a 5MB text file to stress tool_result and trace truncation.",
            ),
            StressCase(
                name="large_file_log_read",
                stage="large-file",
                prompt=("Use the read_file tool to read stress_large_log.txt, "
                        "estimate the error types, and summarize."),
                allowed_statuses=("success", "failed"),
                timeout=timeout,
                notes="Reads a repeated ERROR/WARN/INFO log file.",
            ),
        ])

    if stage in {"all", "large-dir"}:
        cases.extend([
            StressCase(
                name="large_dir_count_and_pattern",
                stage="large-dir",
                prompt=("Use the glob tool once with pattern stress_files/*.txt. "
                        f"Count the files, expect about {large_dir_files}, "
                        "and summarize the naming pattern. Do not list the "
                        "directory repeatedly."),
                allowed_statuses=("success", "failed"),
                timeout=timeout,
                max_tools=10,
                notes="Lists a large directory and checks count/pattern behavior.",
            ),
            StressCase(
                name="large_dir_first_last_names",
                stage="large-dir",
                prompt=("Use the glob tool once with pattern stress_files/*.txt. "
                        "From that result, sort the names and report only the "
                        "first 5 and last 5 file names. Do not call glob or "
                        "bash repeatedly."),
                allowed_statuses=("success", "failed"),
                timeout=timeout,
                max_tools=10,
                notes="Checks whether a large directory listing stays manageable.",
            ),
        ])

    if stage in {"all", "long-history"}:
        turns = max(1, int(history_turns or 60))
        history, expected_terms = build_long_history_messages(turns)
        prompt = (
            "总结我之前提到的关键约束，列出你还记得哪些。"
            "请优先列出明确记得的关键词，不要编造没有出现过的约束。"
        )
        cases.append(StressCase(
            name="long_history_constraints_recall",
            stage="long-history",
            prompt=prompt,
            expected_status="success",
            allowed_statuses=("success",),
            timeout=timeout,
            history_turns=turns,
            initial_messages=history,
            estimated_context_size=estimate_messages_size([*history, {"role": "user", "content": prompt}]),
            expected_terms=expected_terms,
            notes="Synthesizes many prior turns and asks the agent to recall constraints.",
        ))

    if stage in {"all", "multi-tool"}:
        cases.extend([
            StressCase(
                name="multi_tool_numbered_files",
                stage="multi-tool",
                prompt=("Use write_file to create 20 small files under "
                        "multi_tool_files/, named file_01.txt through "
                        "file_20.txt. Each file must contain its own number. "
                        "Then use read_file to read file_01.txt, file_10.txt, "
                        "and file_20.txt, and summarize those three contents. "
                        "Use relative paths inside the workspace."),
                expected_status="success",
                allowed_statuses=("success",),
                timeout=timeout,
                max_tools=35,
                expected_created_files=20,
                notes="Creates 20 files and reads three of them.",
            ),
            StressCase(
                name="multi_tool_reports_dir",
                stage="multi-tool",
                prompt=("Create a reports directory by writing 5 markdown files "
                        "with write_file: reports/report_1.md through "
                        "reports/report_5.md. Then use glob with pattern "
                        "reports/*.md, read the markdown files with read_file, "
                        "and summarize the directory and file contents."),
                expected_status="success",
                allowed_statuses=("success",),
                timeout=timeout,
                max_tools=25,
                expected_created_files=5,
                notes="Creates report markdown files, lists the directory, and reads contents.",
            ),
        ])

    future_stages = SUPPORTED_STAGES - {
        "all",
        "smoke",
        "large-file",
        "large-dir",
        "long-history",
        "multi-tool",
        "trace-retention",
    }
    if stage in future_stages:
        notes.append(f"Stage '{stage}' is registered but not implemented yet.")
    return cases, notes


def write_repeated_text(path: Path, header: str, line: str, target_bytes: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(header)
        written = len(header.encode("utf-8"))
        line_bytes = len(line.encode("utf-8"))
        while written < target_bytes:
            handle.write(line)
            written += line_bytes


def prepare_large_file_stage(workspace: Path):
    write_repeated_text(
        workspace / "stress_large_1mb.txt",
        "Stress file: 1MB text payload for read_file/tool_result truncation tests.\n",
        "INFO payload chunk: alpha beta gamma delta epsilon 0123456789\n",
        1 * 1024 * 1024,
    )
    write_repeated_text(
        workspace / "stress_large_5mb.txt",
        "Stress file: 5MB text payload for large trace/timeline stability tests.\n",
        "DATA payload chunk: lorem ipsum dolor sit amet 0123456789 ABCDEFGHIJ\n",
        5 * 1024 * 1024,
    )
    log_path = workspace / "stress_large_log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    patterns = [
        "ERROR DatabaseTimeout request_id={i} shard=main\n",
        "WARN RetryScheduled request_id={i} attempt=2\n",
        "INFO RequestCompleted request_id={i} status=200\n",
        "ERROR PermissionDenied request_id={i} tool=bash\n",
        "WARN SlowToolResult request_id={i} chars=120000\n",
    ]
    with log_path.open("w", encoding="utf-8") as handle:
        for i in range(20000):
            handle.write(patterns[i % len(patterns)].format(i=i))


def prepare_large_dir_stage(workspace: Path, file_count: int):
    count = max(1, int(file_count or 1000))
    files_dir = workspace / "stress_files"
    files_dir.mkdir(parents=True, exist_ok=True)
    width = max(4, len(str(count)))
    for index in range(1, count + 1):
        name = f"file_{index:0{width}d}.txt"
        content = (
            f"stress directory file {index}\n"
            "purpose: many-small-files listing and trace truncation test\n"
        )
        (files_dir / name).write_text(content, encoding="utf-8")


def prepare_long_history_stage(workspace: Path):
    memory_dir = workspace / ".memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "Stress memory: preserve user constraints and avoid inventing missing ones.\n",
        encoding="utf-8",
    )


def prepare_stage_workspace(stage: str, workspace: Path,
                            large_dir_files: int = 1000):
    if stage in {"all", "large-file"}:
        prepare_large_file_stage(workspace)
    if stage in {"all", "large-dir"}:
        prepare_large_dir_stage(workspace, large_dir_files)
    if stage in {"all", "long-history"}:
        prepare_long_history_stage(workspace)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def load_metadata(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def dir_size(path: Path) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs
                       if not (Path(root) / name).is_symlink()]
            for name in files:
                file_path = Path(root) / name
                if not file_path.is_symlink():
                    total += file_size(file_path)
    except Exception:
        return total
    return total


def patch_trace_retention(trace, **overrides):
    keys = [
        "TRACE_CLEANUP_ENABLED",
        "TRACE_RETENTION_MAX_DAYS",
        "TRACE_RETENTION_MAX_RUNS",
        "TRACE_RETENTION_MAX_MB",
        "TRACE_MAX_RUN_MB",
        "TRACE_KEEP_PINNED",
    ]
    old = {key: getattr(trace, key) for key in keys}
    for key, value in overrides.items():
        setattr(trace, key, value)
    return old


def restore_trace_retention(trace, old: dict):
    for key, value in old.items():
        setattr(trace, key, value)


def make_fake_trace_run(workdir: Path, run_id: str, *, start_time: float | None = None,
                        status: str = "success", pinned: bool = False,
                        missing_metadata: bool = False, bad_metadata: bool = False,
                        trace_bytes: int = 256, artifacts_bytes: int = 0) -> Path:
    run_dir = workdir / ".codepilot" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    start = float(start_time if start_time is not None else time.time())
    if bad_metadata:
        (run_dir / "metadata.json").write_text("{bad json", encoding="utf-8")
    elif not missing_metadata:
        metadata = {
            "run_id": run_id,
            "start_time": start,
            "end_time": start + 1,
            "duration_ms": 1000,
            "status": status,
            "prompt_preview": f"fake {status} run",
            "model_provider": "fake",
            "model": "trace-retention",
            "workdir": str(workdir),
            "tool_count": 1,
            "error_count": 1 if status == "failed" else 0,
            "blocked_count": 1 if status == "blocked" else 0,
            "event_count": 4,
            "timeline_event_count": 3,
        }
        (run_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8")
    (run_dir / "trace.jsonl").write_text("x" * trace_bytes, encoding="utf-8")
    (run_dir / "timeline.jsonl").write_text(
        json.dumps({"type": "final_answer", "content": "fake final"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "timeline.md").write_text("# Timeline\n\nfake timeline\n", encoding="utf-8")
    (run_dir / "final.md").write_text("fake final\n", encoding="utf-8")
    if artifacts_bytes:
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "large.txt").write_text("a" * artifacts_bytes, encoding="utf-8")
    if pinned:
        (run_dir / ".keep").write_text("", encoding="utf-8")
    try:
        os.utime(run_dir, (start, start))
    except Exception:
        pass
    return run_dir


def list_run_dirs(workdir: Path) -> list[Path]:
    runs_dir = workdir / ".codepilot" / "runs"
    try:
        return sorted([path for path in runs_dir.iterdir() if path.is_dir()],
                      key=lambda item: item.name)
    except Exception:
        return []


def run_index_inconsistencies(trace, workdir: Path) -> tuple[int, list[str]]:
    run_dirs = {path.name: path for path in list_run_dirs(workdir)}
    index_items = trace.load_run_index(workdir)
    index_ids = {str(item.get("run_id")) for item in index_items if item.get("run_id")}
    issues = []
    for run_id in sorted(index_ids):
        if run_id not in run_dirs:
            issues.append(f"stale index entry: {run_id}")
    for run_id, run_dir in sorted(run_dirs.items()):
        if load_metadata(run_dir) and run_id not in index_ids:
            issues.append(f"valid metadata run missing from index: {run_id}")
    return len(issues), issues


def read_text_preview(path: Path, limit: int = 500) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        return text[:limit]
    except Exception:
        return ""


def _agent_loop_worker(case: StressCase, workdir: str, model_provider: str,
                       model: str, mock: bool, conn):
    os.environ["CODEPILOT_S20_WORKDIR"] = workdir
    ensure_project_importable()
    run = None
    try:
        from codepilot_s20 import agent_loop, context as context_mod, trace
        from codepilot_s20 import compact as compact_mod

        if mock:
            agent_loop.client = MockClient(case.stage)
            compact_mod.summarize_history = lambda messages: (
                "Earlier long-history constraints included alpha, beta, gamma, "
                "delta, epsilon, zeta, eta, and theta."
            )
            agent_loop.MODEL_PROVIDER = "mock"
            agent_loop.MODEL = "codepilot-s20-stress"
            model_provider = "mock"
            model = "codepilot-s20-stress"

        run = trace.start_run(
            case.prompt,
            workdir=agent_loop.WORKDIR,
            model_provider=model_provider,
            model=model,
        )
        conn.send({
            "type": "started",
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "trace_path": str(run.trace_path),
            "timeline_path": str(run.timeline_path),
        })
        messages = list(case.initial_messages)
        messages.append({"role": "user", "content": case.prompt})
        context_obj = context_mod.update_context({}, messages)
        agent_loop.agent_loop(messages, context_obj)
        conn.send({"type": "done"})
    except BaseException as exc:
        try:
            from codepilot_s20 import trace
            trace.record_error(exc)
            trace.finish_run(f"[Error] {type(exc).__name__}: {exc}")
        except Exception:
            pass
        try:
            conn.send({
                "type": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "run_id": getattr(run, "run_id", None),
                "run_dir": str(getattr(run, "run_dir", "")),
            })
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def analyze_trace(trace_path: Path) -> dict:
    stats = {"compact_count": 0, "tool_result_truncated": False, "round_count": 0}
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") == "llm_request":
                stats["round_count"] += 1
            if event.get("type") == "compact":
                stats["compact_count"] += 1
            elif event.get("type") == "tool_use" and event.get("tool") == "compact":
                stats["compact_count"] += 1
            if event.get("type") == "tool_result" and "[truncated" in str(event.get("content", "")):
                stats["tool_result_truncated"] = True
    except Exception:
        return stats
    return stats


def count_stress_files(workspace: Path) -> int:
    try:
        return sum(1 for path in (workspace / "stress_files").iterdir()
                   if path.is_file())
    except Exception:
        return 0


def count_created_files(workspace: Path, case_name: str) -> int:
    if case_name == "multi_tool_numbered_files":
        target = workspace / "multi_tool_files"
    elif case_name == "multi_tool_reports_dir":
        target = workspace / "reports"
    else:
        return 0
    try:
        return sum(1 for path in target.iterdir() if path.is_file())
    except Exception:
        return 0


def mark_run_timeout(run_dir: Path, duration_ms: int):
    metadata_path = run_dir / "metadata.json"
    try:
        data = load_metadata(run_dir)
        if not data:
            return
        data["status"] = "timeout"
        data["duration_ms"] = duration_ms
        data["end_time"] = data.get("end_time") or time.time()
        metadata_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_case(case: StressCase, modules, model_provider: str, model: str,
             mock: bool = False) -> StressResult:
    agent_loop = modules["agent_loop"]
    started = time.time()
    run_id = None
    run_dir = None
    trace_path = None
    timeline_path = None
    result = StressResult(
        case_name=case.name,
        stage=case.stage,
        prompt=case.prompt,
        history_turns=case.history_turns,
        estimated_context_size=case.estimated_context_size,
        notes=case.notes,
    )

    timeout = float(case.timeout or 120)
    parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
    proc = multiprocessing.Process(
        target=_agent_loop_worker,
        args=(case, str(agent_loop.WORKDIR), model_provider, model, mock, child_conn),
        daemon=True,
    )
    try:
        proc.start()
        child_conn.close()
        deadline = started + timeout
        while True:
            try:
                if parent_conn.poll(0.1):
                    message = parent_conn.recv()
                    msg_type = message.get("type")
                    if msg_type == "started":
                        run_id = message.get("run_id")
                        run_dir = Path(message.get("run_dir", ""))
                        trace_path = Path(message.get("trace_path", ""))
                        timeline_path = Path(message.get("timeline_path", ""))
                    elif msg_type == "error":
                        if message.get("run_id"):
                            run_id = message.get("run_id")
                        if message.get("run_dir"):
                            run_dir = Path(message.get("run_dir"))
                        result.reason = f"Worker error: {message.get('error', '')}"
                    elif msg_type == "done":
                        break
            except EOFError:
                break

            if not proc.is_alive():
                break
            if time.time() >= deadline:
                result.timed_out = True
                result.reason = f"Timeout after {timeout:g}s"
                proc.terminate()
                break

        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass

    duration_ms = int((time.time() - started) * 1000)
    if result.timed_out and run_dir:
        mark_run_timeout(run_dir, duration_ms)

    if run_dir:
        result.run_id = run_id
        metadata = load_metadata(run_dir)
        result.status = metadata.get("status", "unknown")
        result.duration_ms = int(metadata.get("duration_ms") or duration_ms)
        result.tool_count = int(metadata.get("tool_count") or 0)
        result.error_count = int(metadata.get("error_count") or 0)
        result.blocked_count = int(metadata.get("blocked_count") or 0)
        result.trace_size = file_size(trace_path or (run_dir / "trace.jsonl"))
        result.timeline_size = file_size(timeline_path or (run_dir / "timeline.md"))
        result.final_answer_preview = read_text_preview(run_dir / "final.md")
        trace_stats = analyze_trace(trace_path or (run_dir / "trace.jsonl"))
        result.compact_count = int(trace_stats["compact_count"])
        result.tool_result_truncated = bool(trace_stats["tool_result_truncated"])
        result.round_count = int(trace_stats["round_count"])
        if case.stage == "large-dir":
            result.file_count = count_stress_files(agent_loop.WORKDIR)
        if case.stage == "multi-tool":
            result.created_files = count_created_files(agent_loop.WORKDIR, case.name)
    else:
        result.duration_ms = duration_ms

    expected = case.expected_status
    timed_out = result.timed_out or bool(case.timeout and duration_ms > case.timeout * 1000)
    result.timed_out = timed_out
    if timed_out:
        result.outcome = "WARN"
        result.reason = result.reason or f"Case exceeded timeout budget ({case.timeout}s)."
    elif case.max_tools is not None and result.tool_count > case.max_tools:
        result.outcome = "WARN"
        result.reason = f"Tool count {result.tool_count} exceeded limit {case.max_tools}."
    elif case.allowed_statuses and result.status not in case.allowed_statuses:
        result.outcome = "FAIL"
        result.reason = f"Expected status in {case.allowed_statuses}, got {result.status}."
    elif expected and result.status != expected:
        result.outcome = "FAIL"
        result.reason = f"Expected status {expected}, got {result.status}."
    elif result.reason:
        result.outcome = "FAIL"
    elif case.expected_created_files and result.created_files < case.expected_created_files:
        result.outcome = "FAIL"
        result.reason = (f"Expected at least {case.expected_created_files} created files, "
                         f"got {result.created_files}.")
    elif case.expected_terms and not any(
            term.lower() in result.final_answer_preview.lower()
            for term in case.expected_terms):
        result.outcome = "WARN"
        result.reason = "Final answer did not mention any expected long-history terms."
    else:
        result.outcome = "PASS"
        result.passed = True
        result.reason = "OK"
    return result


def finish_retention_result(result: StressResult, checks: list[tuple[str, bool]],
                            issues: list[str] | None = None) -> StressResult:
    failed = [name for name, ok in checks if not ok]
    result.status = "failed" if failed else "success"
    result.outcome = "FAIL" if failed else "PASS"
    result.passed = not failed
    result.reason = "OK" if not failed else "; ".join(failed)
    if issues:
        preview = "\n".join(issues[:20])
        if len(issues) > 20:
            preview += f"\n... {len(issues) - 20} more issue(s)"
        result.final_answer_preview = preview
    return result


def run_trace_retention_mixed(trace, base: Path, fake_run_count: int,
                              timeout: float) -> StressResult:
    started = time.time()
    workdir = base / "mixed"
    now = time.time()
    count = max(20, int(fake_run_count or 100))
    max_runs = max(10, count // 2)
    old = patch_trace_retention(
        trace,
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=7,
        TRACE_RETENTION_MAX_RUNS=max_runs,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        expired = make_fake_trace_run(
            workdir, "expired_delete", start_time=now - 20 * 24 * 60 * 60)
        window_old = make_fake_trace_run(
            workdir, "window_delete", start_time=now - 6 * 24 * 60 * 60)
        pinned = make_fake_trace_run(
            workdir, "pinned_old", start_time=now - 30 * 24 * 60 * 60, pinned=True)
        current = make_fake_trace_run(
            workdir, "current_running", start_time=now - 30 * 24 * 60 * 60,
            status="running")
        bad = make_fake_trace_run(workdir, "bad_metadata", start_time=now,
                                  bad_metadata=True)
        missing = make_fake_trace_run(workdir, "missing_metadata", start_time=now,
                                      missing_metadata=True)

        statuses = ("success", "failed", "blocked")
        for index in range(count):
            make_fake_trace_run(
                workdir,
                f"fake_{index:04d}",
                start_time=now - index * 60,
                status=statuses[index % len(statuses)],
                pinned=(index % 37 == 0),
                trace_bytes=512 + (index % 5) * 64,
            )
        total_before = dir_size(workdir / ".codepilot" / "runs")
        index_before = len(trace.reconcile_run_index(workdir))
        cleanup_stats = trace.cleanup_old_runs(
            workdir=workdir, current_run_id="current_running")
        total_after = dir_size(workdir / ".codepilot" / "runs")
        index_after = len(trace.load_run_index(workdir))
        inconsistencies, issues = run_index_inconsistencies(trace, workdir)
        kept = len(list_run_dirs(workdir))
        pinned_count = sum(1 for path in list_run_dirs(workdir)
                           if (path / ".keep").exists())
        checks = [
            ("expired TTL run was not deleted", not expired.exists()),
            ("old sliding-window run was not deleted", not window_old.exists()),
            ("pinned run was deleted", pinned.exists()),
            ("current running run was deleted", current.exists()),
            ("bad metadata run crashed or disappeared unexpectedly", bad.exists()),
            ("missing metadata run crashed or disappeared unexpectedly", missing.exists()),
            ("cleanup did not delete any runs", int(cleanup_stats.get("deleted") or 0) > 0),
            ("run_index has inconsistencies", inconsistencies == 0),
        ]
        result = StressResult(
            case_name="trace_retention_mixed",
            stage="trace-retention",
            prompt="fake run cleanup: ttl, sliding window, pinned, current, bad metadata",
            run_id="fake-runs",
            duration_ms=int((time.time() - started) * 1000),
            fake_run_count=count + 6,
            deleted_count=int(cleanup_stats.get("deleted") or 0),
            kept_count=kept,
            pinned_count=pinned_count,
            total_size_before=total_before,
            total_size_after=total_after,
            index_count_before=index_before,
            index_count_after=index_after,
            inconsistencies=inconsistencies,
            notes=f"max_runs={max_runs}",
        )
        return finish_retention_result(result, checks, issues)
    except Exception as exc:
        return StressResult(
            case_name="trace_retention_mixed",
            stage="trace-retention",
            prompt="fake run cleanup: ttl, sliding window, pinned, current, bad metadata",
            status="failed",
            outcome="FAIL",
            reason=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
    finally:
        restore_trace_retention(trace, old)
        trace.CURRENT_TRACE = None


def run_trace_retention_quota(trace, base: Path, timeout: float) -> StressResult:
    started = time.time()
    workdir = base / "quota"
    now = time.time()
    old = patch_trace_retention(
        trace,
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=1000,
        TRACE_RETENTION_MAX_MB=0.12,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        oldest = make_fake_trace_run(
            workdir, "quota_oldest", start_time=now - 200, trace_bytes=20_000)
        newest = make_fake_trace_run(
            workdir, "quota_newest", start_time=now, trace_bytes=20_000)
        for index in range(10):
            make_fake_trace_run(
                workdir, f"quota_{index:02d}",
                start_time=now - 190 + index * 10,
                trace_bytes=20_000,
            )
        total_before = dir_size(workdir / ".codepilot" / "runs")
        index_before = len(trace.reconcile_run_index(workdir))
        cleanup_stats = trace.cleanup_old_runs(workdir=workdir)
        total_after = dir_size(workdir / ".codepilot" / "runs")
        index_after = len(trace.load_run_index(workdir))
        inconsistencies, issues = run_index_inconsistencies(trace, workdir)
        checks = [
            ("quota did not reduce total size", total_after < total_before),
            ("quota oldest run was not deleted first", not oldest.exists()),
            ("quota newest run was deleted unexpectedly", newest.exists()),
            ("quota cleanup deleted no runs", int(cleanup_stats.get("deleted") or 0) > 0),
            ("run_index has inconsistencies", inconsistencies == 0),
        ]
        result = StressResult(
            case_name="trace_retention_quota",
            stage="trace-retention",
            prompt="fake run cleanup: storage quota",
            run_id="fake-runs",
            duration_ms=int((time.time() - started) * 1000),
            fake_run_count=12,
            deleted_count=int(cleanup_stats.get("deleted") or 0),
            kept_count=len(list_run_dirs(workdir)),
            pinned_count=0,
            total_size_before=total_before,
            total_size_after=total_after,
            index_count_before=index_before,
            index_count_after=index_after,
            inconsistencies=inconsistencies,
        )
        return finish_retention_result(result, checks, issues)
    except Exception as exc:
        return StressResult(
            case_name="trace_retention_quota",
            stage="trace-retention",
            prompt="fake run cleanup: storage quota",
            status="failed",
            outcome="FAIL",
            reason=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
    finally:
        restore_trace_retention(trace, old)
        trace.CURRENT_TRACE = None


def run_trace_retention_large_run(trace, base: Path, timeout: float) -> StressResult:
    started = time.time()
    workdir = base / "large_run"
    old = patch_trace_retention(
        trace,
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=1000,
        TRACE_RETENTION_MAX_RUNS=1000,
        TRACE_RETENTION_MAX_MB=1000,
        TRACE_MAX_RUN_MB=0.001,
        TRACE_KEEP_PINNED=True,
    )
    try:
        run_dir = make_fake_trace_run(
            workdir, "large_run", start_time=time.time(),
            trace_bytes=5000, artifacts_bytes=5000)
        total_before = dir_size(workdir / ".codepilot" / "runs")
        index_before = len(trace.reconcile_run_index(workdir))
        cleanup_stats = trace.cleanup_old_runs(workdir=workdir)
        total_after = dir_size(workdir / ".codepilot" / "runs")
        index_after = len(trace.load_run_index(workdir))
        trace_text = read_text_preview(run_dir / "trace.jsonl", limit=2000)
        inconsistencies, issues = run_index_inconsistencies(trace, workdir)
        checks = [
            ("large run directory was deleted", run_dir.exists()),
            ("large run metadata was deleted", (run_dir / "metadata.json").exists()),
            ("large run timeline.jsonl was deleted", (run_dir / "timeline.jsonl").exists()),
            ("large run timeline.md was deleted", (run_dir / "timeline.md").exists()),
            ("large run final.md was deleted", (run_dir / "final.md").exists()),
            ("large run artifacts were not removed", not (run_dir / "artifacts").exists()),
            ("large run trace was not truncated", "Full trace truncated" in trace_text),
            ("run_index has inconsistencies", inconsistencies == 0),
        ]
        result = StressResult(
            case_name="trace_retention_large_run",
            stage="trace-retention",
            prompt="fake run cleanup: single run limit",
            run_id="large_run",
            duration_ms=int((time.time() - started) * 1000),
            fake_run_count=1,
            deleted_count=int(cleanup_stats.get("deleted") or 0),
            kept_count=len(list_run_dirs(workdir)),
            pinned_count=0,
            total_size_before=total_before,
            total_size_after=total_after,
            index_count_before=index_before,
            index_count_after=index_after,
            inconsistencies=inconsistencies,
        )
        return finish_retention_result(result, checks, issues)
    except Exception as exc:
        return StressResult(
            case_name="trace_retention_large_run",
            stage="trace-retention",
            prompt="fake run cleanup: single run limit",
            status="failed",
            outcome="FAIL",
            reason=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
    finally:
        restore_trace_retention(trace, old)
        trace.CURRENT_TRACE = None


def run_trace_retention_failure(trace, base: Path, timeout: float) -> StressResult:
    started = time.time()
    workdir = base / "failure"
    old_scan = trace._scan_runs
    old = patch_trace_retention(
        trace,
        TRACE_CLEANUP_ENABLED=True,
        TRACE_RETENTION_MAX_DAYS=0,
        TRACE_RETENTION_MAX_RUNS=0,
        TRACE_RETENTION_MAX_MB=0,
        TRACE_MAX_RUN_MB=1000,
        TRACE_KEEP_PINNED=True,
    )
    try:
        make_fake_trace_run(workdir, "failure_input", start_time=time.time())

        def broken_scan(*args, **kwargs):
            raise RuntimeError("synthetic cleanup scan failure")

        trace._scan_runs = broken_scan
        cleanup_stats = trace.cleanup_old_runs(workdir=workdir)
        checks = [
            ("cleanup failure propagated instead of returning stats",
             isinstance(cleanup_stats, dict)),
            ("cleanup failure removed test run unexpectedly",
             (workdir / ".codepilot" / "runs" / "failure_input").exists()),
        ]
        result = StressResult(
            case_name="trace_retention_cleanup_failure",
            stage="trace-retention",
            prompt="fake run cleanup: failure isolation",
            run_id="fake-runs",
            duration_ms=int((time.time() - started) * 1000),
            fake_run_count=1,
            deleted_count=int(cleanup_stats.get("deleted") or 0),
            kept_count=len(list_run_dirs(workdir)),
            total_size_before=dir_size(workdir / ".codepilot" / "runs"),
            total_size_after=dir_size(workdir / ".codepilot" / "runs"),
            notes="Monkeypatched cleanup scan to raise.",
        )
        return finish_retention_result(result, checks)
    except Exception as exc:
        return StressResult(
            case_name="trace_retention_cleanup_failure",
            stage="trace-retention",
            prompt="fake run cleanup: failure isolation",
            status="failed",
            outcome="FAIL",
            reason=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.time() - started) * 1000),
        )
    finally:
        trace._scan_runs = old_scan
        restore_trace_retention(trace, old)
        trace.CURRENT_TRACE = None


def run_trace_retention_stage(trace, workspace: Path, fake_run_count: int,
                              timeout: float) -> list[StressResult]:
    base = workspace / "trace_retention"
    base.mkdir(parents=True, exist_ok=True)
    return [
        run_trace_retention_mixed(trace, base, fake_run_count, timeout),
        run_trace_retention_quota(trace, base, timeout),
        run_trace_retention_large_run(trace, base, timeout),
        run_trace_retention_failure(trace, base, timeout),
    ]


def summarize(report: StressReport):
    results = report.results
    report.total_runs = len(results)
    report.success_count = sum(1 for item in results if item.status == "success")
    report.failed_count = sum(1 for item in results if item.status == "failed")
    report.blocked_count = sum(1 for item in results if item.status == "blocked")
    report.timeout_count = sum(1 for item in results if item.status == "timeout" or item.outcome == "WARN")
    report.error_count = sum(item.error_count for item in results)
    report.compact_count = sum(item.compact_count for item in results)
    report.tool_result_truncated_count = sum(1 for item in results if item.tool_result_truncated)
    report.cleanup_deleted_count = sum(item.deleted_count for item in results)
    report.fake_run_count = sum(item.fake_run_count for item in results)
    report.kept_count = sum(item.kept_count for item in results)
    report.pinned_count = sum(item.pinned_count for item in results)
    report.total_size_before = sum(item.total_size_before for item in results)
    report.total_size_after = sum(item.total_size_after for item in results)
    report.index_count_before = sum(item.index_count_before for item in results)
    report.index_count_after = sum(item.index_count_after for item in results)
    report.inconsistencies = sum(item.inconsistencies for item in results)
    if not results:
        return
    report.average_duration_ms = int(sum(item.duration_ms for item in results) / len(results))
    report.max_duration_ms = max(item.duration_ms for item in results)
    report.average_tool_count = round(sum(item.tool_count for item in results) / len(results), 2)
    report.max_tool_count = max(item.tool_count for item in results)
    report.max_rounds = max(item.round_count for item in results)
    report.average_trace_size = int(sum(item.trace_size for item in results) / len(results))
    report.max_trace_size = max(item.trace_size for item in results)
    report.average_timeline_size = int(sum(item.timeline_size for item in results) / len(results))
    report.max_timeline_size = max(item.timeline_size for item in results)


def write_json_report(report: StressReport, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(report)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"WARN failed to write json report: {exc}")


def markdown_report(report: StressReport) -> str:
    lines = [
        "# Agent Stress Report",
        "",
        f"Started: `{report.started_at}`",
        f"Stage: `{report.stage}`",
        f"Mock: `{report.mock}`",
        f"Workspace: `{report.workspace}`",
        "",
        "## Runtime",
        "",
        f"- os: {report.runtime_context.get('os', 'unknown')}",
        f"- platform: {report.runtime_context.get('platform', 'unknown')}",
        f"- shell: {report.runtime_context.get('shell', 'unknown')}",
        f"- path_separator: {report.runtime_context.get('path_separator', '')}",
        "",
    ]
    if report.stage_details:
        lines.extend(["## Stage Details", ""])
        lines.extend(f"- {key}: {value}" for key, value in report.stage_details.items())
        lines.append("")

    lines.extend([
        "## Summary",
        "",
        f"- total_runs: {report.total_runs}",
        f"- success_count: {report.success_count}",
        f"- failed_count: {report.failed_count}",
        f"- blocked_count: {report.blocked_count}",
        f"- timeout_count: {report.timeout_count}",
        f"- average_duration_ms: {report.average_duration_ms}",
        f"- max_duration_ms: {report.max_duration_ms}",
        f"- average_tool_count: {report.average_tool_count}",
        f"- max_tool_count: {report.max_tool_count}",
        f"- max_rounds: {report.max_rounds}",
        f"- average_trace_size: {report.average_trace_size}",
        f"- max_trace_size: {report.max_trace_size}",
        f"- average_timeline_size: {report.average_timeline_size}",
        f"- max_timeline_size: {report.max_timeline_size}",
        f"- compact_count: {report.compact_count}",
        f"- tool_result_truncated_count: {report.tool_result_truncated_count}",
        f"- cleanup_deleted_count: {report.cleanup_deleted_count}",
        f"- fake_run_count: {report.fake_run_count}",
        f"- kept_count: {report.kept_count}",
        f"- pinned_count: {report.pinned_count}",
        f"- total_size_before: {report.total_size_before}",
        f"- total_size_after: {report.total_size_after}",
        f"- index_count_before: {report.index_count_before}",
        f"- index_count_after: {report.index_count_after}",
        f"- inconsistencies: {report.inconsistencies}",
        f"- error_count: {report.error_count}",
        "",
    ])
    if report.notes:
        lines.extend(["## Notes", ""])
        lines.extend(f"- {note}" for note in report.notes)
        lines.append("")

    lines.extend(["## Results", ""])
    for item in report.results:
        lines.extend([
            f"### {item.outcome}: {item.case_name}",
            "",
            f"- stage: {item.stage}",
            f"- run_id: {item.run_id}",
            f"- status: {item.status}",
            f"- duration_ms: {item.duration_ms}",
            f"- round_count: {item.round_count}",
            f"- tool_count: {item.tool_count}",
            f"- error_count: {item.error_count}",
            f"- blocked_count: {item.blocked_count}",
            f"- trace_size: {item.trace_size}",
            f"- timeline_size: {item.timeline_size}",
            f"- file_count: {item.file_count}",
            f"- created_files: {item.created_files}",
            f"- history_turns: {item.history_turns}",
            f"- estimated_context_size: {item.estimated_context_size}",
            f"- compact_count: {item.compact_count}",
            f"- tool_result_truncated: {item.tool_result_truncated}",
            f"- fake_run_count: {item.fake_run_count}",
            f"- deleted_count: {item.deleted_count}",
            f"- kept_count: {item.kept_count}",
            f"- pinned_count: {item.pinned_count}",
            f"- total_size_before: {item.total_size_before}",
            f"- total_size_after: {item.total_size_after}",
            f"- index_count_before: {item.index_count_before}",
            f"- index_count_after: {item.index_count_after}",
            f"- inconsistencies: {item.inconsistencies}",
            f"- timed_out: {item.timed_out}",
            f"- reason: {item.reason}",
            "",
        ])
        if item.final_answer_preview:
            lines.extend([
                "Final answer preview:",
                "",
                "```text",
                item.final_answer_preview,
                "```",
                "",
            ])
    return "\n".join(lines)


def write_markdown_report(report: StressReport, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown_report(report), encoding="utf-8")
    except Exception as exc:
        print(f"WARN failed to write markdown report: {exc}")


def print_result(result: StressResult):
    file_part = f" files={result.file_count}" if result.file_count else ""
    history_part = f" history_turns={result.history_turns}" if result.history_turns else ""
    retention_part = ""
    if result.stage == "trace-retention":
        retention_part = (
            f" fake_runs={result.fake_run_count}"
            f" deleted={result.deleted_count}"
            f" kept={result.kept_count}"
            f" inconsistencies={result.inconsistencies}"
        )
    print(
        f"{result.outcome} {result.stage}/{result.case_name} "
        f"status={result.status} run_id={result.run_id} "
        f"duration_ms={result.duration_ms}{file_part}{history_part}{retention_part} "
        f"truncated={result.tool_result_truncated} "
        f"compact_count={result.compact_count} reason={result.reason}"
    )


def cleanup_workspace(path: Path) -> bool:
    try:
        if path.exists() and path.is_dir() and DEFAULT_WORKSPACE in path.parents:
            shutil.rmtree(path)
            return True
    except Exception as exc:
        print(f"WARN failed to clean stress workspace: {exc}")
    return False


def main() -> int:
    args = parse_args()
    stage = args.stage.strip().lower()
    mock = bool(args.mock)

    run_stamp = timestamp()
    workspace_base = Path(args.workspace).resolve()
    session_workspace = workspace_base / run_stamp
    report_dir = DEFAULT_REPORT_ROOT / run_stamp
    session_workspace.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    os.environ["CODEPILOT_S20_WORKDIR"] = str(session_workspace)
    ensure_project_importable()

    from codepilot_s20 import agent_loop, context, trace  # noqa: E402
    from codepilot_s20.runtime_context import detect_runtime_context  # noqa: E402

    if mock:
        agent_loop.client = MockClient(stage)

    large_dir_files = max(1, int(args.large_dir_files or 1000))
    history_turns = max(1, int(args.history_turns or 60))
    cases, notes = build_cases(stage, args.timeout, large_dir_files, history_turns)
    prepare_stage_workspace(stage, session_workspace, large_dir_files)
    repeated_cases = []
    for index in range(max(1, args.runs)):
        for case in cases:
            name = case.name if args.runs == 1 else f"{case.name}_{index + 1}"
            repeated_cases.append(StressCase(
                name=name,
                prompt=case.prompt,
                stage=case.stage,
                expected_status=case.expected_status,
                allowed_statuses=case.allowed_statuses,
                timeout=case.timeout,
                max_tools=case.max_tools,
                history_turns=case.history_turns,
                initial_messages=case.initial_messages,
                estimated_context_size=case.estimated_context_size,
                expected_terms=case.expected_terms,
                expected_created_files=case.expected_created_files,
                notes=case.notes,
            ))

    stage_details = {}
    if stage in {"all", "large-dir"}:
        stage_details["large_dir_file_count"] = large_dir_files
        stage_details["large_dir_path"] = str(session_workspace / "stress_files")
    if stage in {"all", "long-history"}:
        stage_details["history_turns"] = max(1, int(args.history_turns or 60))
    if stage in {"all", "multi-tool"}:
        stage_details["multi_tool_expected_files"] = "20 numbered files; 5 markdown reports"
    if stage in {"all", "trace-retention"}:
        stage_details["trace_retention_fake_runs"] = max(1, int(args.fake_runs or 100))
        stage_details["trace_retention_workspace"] = str(session_workspace / "trace_retention")

    report = StressReport(
        started_at=run_stamp,
        workspace=str(session_workspace),
        report_dir=str(report_dir),
        mock=mock,
        stage=stage,
        requested_runs=args.runs,
        runtime_context=detect_runtime_context(session_workspace),
        stage_details=stage_details,
        notes=notes,
    )
    if mock:
        report.notes.append("Mock mode replaces model calls; local context compaction still runs.")

    if not repeated_cases and stage not in {"trace-retention"}:
        report.notes.append("No executable cases for this stage in phase 1.")
        print(f"WARN no executable cases for stage '{stage}'")

    for case in repeated_cases:
        result = run_case(
            case,
            modules={"agent_loop": agent_loop, "context": context, "trace": trace},
            model_provider=getattr(agent_loop, "MODEL_PROVIDER", "unknown"),
            model=getattr(agent_loop, "MODEL", "unknown"),
            mock=mock,
        )
        report.results.append(result)
        print_result(result)

    if stage in {"all", "trace-retention"}:
        for result in run_trace_retention_stage(
                trace,
                session_workspace,
                max(1, int(args.fake_runs or 100)),
                args.timeout):
            report.results.append(result)
            print_result(result)

    summarize(report)

    write_json_report(report, report_dir / "stress_report.json")
    write_markdown_report(report, report_dir / "stress_report.md")
    print()
    print(f"Report: {report_dir}")

    if not args.keep_workspace:
        if cleanup_workspace(session_workspace):
            print("Workspace cleaned. Use --keep-workspace to inspect generated runs.")
        else:
            print(f"Workspace kept: {session_workspace}")
    else:
        print(f"Workspace kept: {session_workspace}")

    failed = any(item.outcome == "FAIL" for item in report.results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
