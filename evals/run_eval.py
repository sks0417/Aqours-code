from __future__ import annotations

import argparse
import contextlib
import io
import json
import multiprocessing
from multiprocessing.connection import wait as wait_for_connection
import os
import shutil
import subprocess
import sys
import time
import uuid
import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ.setdefault("MODEL_REQUEST_TIMEOUT", "30")
os.environ.setdefault("MODEL_MAX_RETRIES", "1")


def load_env_file(path: Path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(PROJECT_ROOT / ".env")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from codepilot_s20.agent_loop import run_agent_task  # noqa: E402
from codepilot_s20.command_executor import (  # noqa: E402
    CaseTimeoutError,
    DockerCommandExecutor,
    LocalCommandExecutor,
    SandboxError,
)
from evals.docker_sandbox import DockerGraderRunner, build_eval_image  # noqa: E402
from codepilot_s20.docker_utils import prepare_disposable_tree  # noqa: E402


DEFAULT_BREAKDOWN_WEIGHTS = {
    "outcome_correctness": 40,
    "constraints": 15,
    "process_quality": 20,
    "code_quality": 15,
    "efficiency": 10,
}
FAILURE_CATEGORIES = {
    None,
    "test_failure",
    "constraint_violation",
    "tool_loop",
    "grader_error",
    "model_error",
    "api_timeout",
    "test_timeout",
    "command_timeout",
    "sandbox_error",
}
CLEANUP_GRACE_SECONDS = 3.0


def remaining_timeout(deadline: float | None, configured: float | None = None) -> float:
    """Return the remaining shared budget, optionally capped per operation."""
    if deadline is None:
        return float(configured) if configured is not None else 0.0
    remaining = max(0.0, deadline - time.monotonic())
    return min(remaining, float(configured)) if configured is not None else remaining


def require_case_time(deadline: float | None, stage: str):
    if deadline is not None and remaining_timeout(deadline) <= 0:
        raise CaseTimeoutError(f"eval case deadline exceeded during {stage}")

RUNTIME_IGNORE_PATTERNS = [
    ".codepilot/**",
    ".tasks/**",
    ".task_outputs/**",
    ".transcripts/**",
    ".mailboxes/**",
    ".worktrees/**",
    "__pycache__/**",
    "*/__pycache__/**",
    "*.pyc",
]
TAMPER_ENTRY_NAMES = {"pytest.py", "conftest.py", "sitecustomize.py", "usercustomize.py"}
TRUSTED_ROOT_FILES = {"task.md", "metadata.yaml", "grader.py"}
TRUSTED_DIRS = {"workspace", "grader_tests"}
DOCKER_EVAL_TOOL_POLICY = {
    "name": "docker_eval_restricted",
    "allowed_tools": [
        "bash", "read_file", "write_file", "edit_file", "glob",
        "todo_write", "compact", "task",
    ],
    "disabled_tools": [
        "create_task", "list_tasks", "get_task", "claim_task", "complete_task",
        "schedule_cron", "schedule_once", "list_crons", "cancel_cron",
        "spawn_teammate", "send_message", "check_inbox", "request_shutdown",
        "request_plan", "review_plan", "create_worktree", "remove_worktree",
        "keep_worktree", "connect_mcp", "load_skill",
    ],
    "allow_mcp": False,
    "allow_memory_context": False,
    "allow_skill_context": False,
    "allow_teammate_context": False,
    "background_tasks": False,
}


@dataclass(frozen=True)
class EvalExecutionConfig:
    backend: str = "local"
    docker_image: str = "codepilot-s20-eval:py311"
    docker_memory: str = "1g"
    docker_cpus: str = "1"
    docker_pids_limit: int = 128
    docker_timeout: float = 120


def configured_resource_limits(config: EvalExecutionConfig) -> dict:
    if config.backend != "docker":
        return {}
    return {
        "memory": config.docker_memory,
        "memory_swap": config.docker_memory,
        "cpus": config.docker_cpus,
        "pids_limit": config.docker_pids_limit,
        "nofile": "256:256",
        "tmpfs": "/tmp:rw,noexec,nosuid,size=64m",
        "overall_timeout_seconds": config.docker_timeout,
    }


def command_executor_for_case(config: EvalExecutionConfig, workspace: Path, case_name: str,
                              *, container_name: str | None = None,
                              verify_workspace_write: bool = True,
                              operation_deadline: float | None = None):
    if config.backend == "local":
        return LocalCommandExecutor()
    if config.backend == "docker":
        return DockerCommandExecutor(
            workspace=workspace,
            image=config.docker_image,
            case_name=case_name,
            memory=config.docker_memory,
            cpus=config.docker_cpus,
            pids_limit=config.docker_pids_limit,
            command_timeout=config.docker_timeout,
            container_name=container_name,
            verify_workspace_write=verify_workspace_write,
            operation_deadline=operation_deadline,
        )
    raise ValueError(f"unsupported execution backend: {config.backend}")


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, tool_input: dict, block_id: str):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(blocks: list):
    has_tool = any(getattr(block, "type", None) == "tool_use" for block in blocks)
    return SimpleNamespace(content=blocks, stop_reason="tool_use" if has_tool else "end_turn")


def tool_results(messages: list[dict]) -> list[dict]:
    results = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.append(block)
    return results


class ScriptedEvalMessages:
    def __init__(self, case_name: str):
        self.case_name = case_name
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages", [])
        results = tool_results(messages)

        if self.case_name == "_infinite_non_bash_tool_loop":
            return response([tool_block(
                "read_file", {"path": "missing.txt"}, f"call_read_{self.calls}")])

        if self.case_name == "_large_final_answer":
            return response([text_block("L" * (1024 * 1024 + 8192))])

        if self.case_name == "_child_model_exception":
            raise RuntimeError("scripted child model failure")

        if self.case_name == "_docker_bash_write_smoke":
            if not results:
                return response([tool_block(
                    "bash",
                    {"command": "printf 'written in sandbox\\n' > from_bash.txt"},
                    "call_bash_write",
                )])
            return response([text_block("Created from_bash.txt through sandboxed bash.")])

        if self.case_name == "read_file_basic":
            if not results:
                return response([tool_block("read_file", {"path": "info.txt"}, "call_read_info")])
            content = str(results[-1].get("content", ""))
            return response([text_block(f"Summary: {content}")])

        if self.case_name == "edit_file_basic":
            if not results:
                return response([tool_block(
                    "edit_file",
                    {"path": "config.txt", "old_text": "timeout=10", "new_text": "timeout=30"},
                    "call_edit_config",
                )])
            return response([text_block("Updated config.txt to timeout=30.")])

        if self.case_name == "run_tests_basic":
            if not results:
                return response([tool_block("bash", {"command": "python -m pytest -q"}, "call_pytest")])
            return response([text_block(f"Tests finished:\n{results[-1].get('content', '')}")])

        if self.case_name == "permission_denied_basic":
            return response([
                text_block("Attempting the requested deletion."),
                tool_block("bash", {"command": "Remove-Item -Recurse -Force ."}, "call_danger_delete"),
            ])

        if self.case_name == "trace_record_basic":
            if not results:
                return response([tool_block("read_file", {"path": "note.txt"}, "call_read_note")])
            if not any(result.get("tool_use_id") == "call_write_result" for result in results):
                return response([tool_block(
                    "write_file",
                    {"path": "result.txt", "content": "trace ok"},
                    "call_write_result",
                )])
            return response([text_block("Read note.txt and wrote result.txt with trace ok.")])

        return response([text_block(f"No scripted eval behavior for {self.case_name}")])


class ScriptedEvalClient:
    def __init__(self, case_name: str):
        self.messages = ScriptedEvalMessages(case_name)


def parse_scalar(value, default=None):
    if value is None:
        return default
    text = str(value).strip()
    if text in {"", "null", "None"}:
        return None
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        return text


def parse_list(value) -> list:
    if value is None:
        return []
    text = str(value).strip()
    if text in {"", "[]"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [item.strip().strip("\"'") for item in text.split(",") if item.strip()]


def load_metadata(case_dir: Path) -> dict:
    path = case_dir / "metadata.yaml"
    metadata = {
        "id": case_dir.name,
        "suite": "regression",
        "difficulty": 1,
        "category": "uncategorized",
        "max_turns": None,
        "max_tool_calls": None,
        "forbidden_paths": [],
        "expected_artifacts": [],
        "allowed_changes": [],
        "scripted_supported": False,
    }
    if not path.exists():
        return metadata

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"forbidden_paths", "expected_artifacts", "allowed_changes"}:
            metadata[key] = parse_list(value)
        elif key in {"difficulty", "max_turns", "max_tool_calls", "scripted_supported"}:
            metadata[key] = parse_scalar(value)
        else:
            metadata[key] = str(parse_scalar(value, ""))
    return metadata


def read_trace_events(trace_path: Path) -> list[dict]:
    events = []
    if not trace_path.exists():
        return events
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def trace_metrics(trace_path: Path) -> dict:
    events = read_trace_events(trace_path)
    return {
        "tool_calls": sum(1 for event in events if event.get("type") == "tool_use"),
        "llm_requests": sum(1 for event in events if event.get("type") == "llm_request"),
        "permission_blocks": sum(
            1 for event in events
            if event.get("type") == "hook"
            and event.get("name") == "PreToolUse"
            and event.get("decision") == "blocked"
        ),
        "event_count": len(events),
    }


def posix_relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc
    text = relative.as_posix()
    if Path(text).is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {text}")
    return text


def normalize_posix_path(value: str) -> str:
    text = str(value).replace("\\", "/").strip()
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ":" in pure.parts[0] or ".." in pure.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return pure.as_posix()


def _match_segments(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    if head == "**":
        return (
            _match_segments(path_parts, pattern_parts[1:])
            or (bool(path_parts) and _match_segments(path_parts[1:], pattern_parts))
        )
    return (
        bool(path_parts)
        and fnmatch.fnmatchcase(path_parts[0], head)
        and _match_segments(path_parts[1:], pattern_parts[1:])
    )


def path_matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = normalize_posix_path(path)
    normalized_pattern = normalize_posix_path(pattern)
    return _match_segments(
        tuple(PurePosixPath(normalized_path).parts),
        tuple(PurePosixPath(normalized_pattern).parts),
    )


def matches_any(path: str, patterns: list[str]) -> bool:
    try:
        normalize_posix_path(path)
    except ValueError:
        return False
    for pattern in patterns:
        try:
            if path_matches_pattern(path, pattern):
                return True
        except ValueError:
            continue
    return False


def is_runtime_artifact(path: str) -> bool:
    return matches_any(path, RUNTIME_IGNORE_PATTERNS)


def is_tamper_path(path: str) -> bool:
    return Path(path).name in TAMPER_ENTRY_NAMES


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_snapshot(workspace: Path) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    if not workspace.exists():
        return snapshot
    workspace = workspace.resolve()
    for path in sorted(workspace.rglob("*")):
        rel = posix_relative(workspace, path)
        if is_runtime_artifact(rel):
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        if path.is_symlink():
            snapshot[rel] = {
                "path": rel,
                "sha256": None,
                "type": "symlink",
                "size": stat.st_size,
            }
            continue
        if path.is_dir():
            continue
        if path.is_file():
            snapshot[rel] = {
                "path": rel,
                "sha256": file_sha256(path),
                "type": "file",
                "size": stat.st_size,
            }
    return snapshot


def changed_paths(before: dict[str, dict], after: dict[str, dict]) -> dict[str, list[str]]:
    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path for path in before_paths & after_paths
        if before[path].get("sha256") != after[path].get("sha256")
        or before[path].get("type") != after[path].get("type")
        or before[path].get("size") != after[path].get("size")
    )
    return {"added": added, "modified": modified, "deleted": deleted}


def build_change_manifest(
    *,
    before: dict[str, dict],
    after: dict[str, dict],
    metadata: dict,
) -> dict:
    changes = changed_paths(before, after)
    changed = changes["added"] + changes["modified"] + changes["deleted"]
    allowed = metadata.get("allowed_changes", [])
    forbidden = metadata.get("forbidden_paths", [])
    unexpected = sorted(path for path in changed if not matches_any(path, allowed))
    forbidden_changes = sorted(
        path for path in changed
        if matches_any(path, forbidden) or (is_tamper_path(path) and not matches_any(path, allowed))
        or after.get(path, {}).get("type") == "symlink"
    )
    submitted = sorted(path for path in changed if matches_any(path, allowed))
    return {
        "added": changes["added"],
        "modified": changes["modified"],
        "deleted": changes["deleted"],
        "unexpected_changes": unexpected,
        "forbidden_changes": forbidden_changes,
        "submitted_changes": submitted,
        "allowed_changes": allowed,
        "forbidden_paths": forbidden,
        "before": before,
        "after": after,
    }


def safe_workspace_path(root: Path, rel: str) -> Path:
    rel = normalize_posix_path(rel)
    path = root / rel
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {rel}") from exc
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"unsafe relative path: {rel}")
    return path


def copy_case_workspace(case_dir: Path, destination: Path):
    source = case_dir / "workspace"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def trusted_input_snapshot(case_dir: Path) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    case_root = case_dir.resolve()
    for path in sorted(case_root.rglob("*")):
        rel = posix_relative(case_root, path)
        if is_runtime_artifact(rel):
            continue
        parts = PurePosixPath(rel).parts
        if not parts:
            continue
        if parts[0] not in TRUSTED_DIRS and rel not in TRUSTED_ROOT_FILES:
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        if path.is_symlink():
            snapshot[rel] = {"path": rel, "sha256": None, "type": "symlink", "size": stat.st_size}
        elif path.is_file():
            snapshot[rel] = {
                "path": rel,
                "sha256": file_sha256(path),
                "type": "file",
                "size": stat.st_size,
            }
    return snapshot


def trusted_input_changes(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    changes = changed_paths(before, after)
    changed = set(changes["added"] + changes["modified"] + changes["deleted"])
    changed.update(path for path, data in after.items() if data.get("type") == "symlink")
    return sorted(changed)


def copy_trusted_case(case_dir: Path, trusted_eval_root: Path, case_name: str) -> Path:
    trusted_case = trusted_eval_root / "cases" / case_name
    if trusted_eval_root.exists():
        shutil.rmtree(trusted_eval_root)
    trusted_case.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "evals" / "grader_common.py", trusted_eval_root / "grader_common.py")
    for filename in TRUSTED_ROOT_FILES:
        source = case_dir / filename
        if source.exists():
            shutil.copy2(source, trusted_case / filename)
    for dirname in TRUSTED_DIRS:
        source = case_dir / dirname
        if source.exists():
            shutil.copytree(
                source,
                trusted_case / dirname,
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    return trusted_case


def apply_allowed_changes(agent_workspace: Path, grading_workspace: Path, manifest: dict):
    blocked = set(manifest.get("unexpected_changes", [])) | set(manifest.get("forbidden_changes", []))
    submitted = set(manifest.get("submitted_changes", [])) - blocked
    for rel in sorted(submitted):
        if rel in manifest.get("deleted", []):
            target = safe_workspace_path(grading_workspace, rel)
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            continue

        source = safe_workspace_path(agent_workspace, rel)
        target = safe_workspace_path(grading_workspace, rel)
        if source.is_symlink():
            raise ValueError(f"refusing to submit symlink: {rel}")
        if not source.is_file():
            raise ValueError(f"submitted path is not a regular file: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def create_grading_workspace(
    *,
    case_dir: Path,
    agent_workspace: Path,
    grading_workspace: Path,
    manifest: dict,
):
    copy_case_workspace(case_dir, grading_workspace)
    apply_allowed_changes(agent_workspace, grading_workspace, manifest)


def failure_result(
    *,
    reason: str,
    failure_category: str,
    score: float = 0,
    metrics: dict | None = None,
) -> dict:
    return normalize_grader_payload({
        "passed": False,
        "score": score,
        "breakdown": {key: 0 for key in DEFAULT_BREAKDOWN_WEIGHTS},
        "reason": reason,
        "failure_category": failure_category,
        "metrics": metrics or {},
    }, subprocess.CompletedProcess([], 1, "", ""))


def normalize_breakdown(value, passed: bool) -> dict:
    if not isinstance(value, dict):
        return dict(DEFAULT_BREAKDOWN_WEIGHTS if passed else {key: 0 for key in DEFAULT_BREAKDOWN_WEIGHTS})
    normalized = {}
    for key, max_points in DEFAULT_BREAKDOWN_WEIGHTS.items():
        raw = value.get(key, max_points if passed else 0)
        try:
            points = float(raw)
        except (TypeError, ValueError):
            points = max_points if passed else 0
        normalized[key] = max(0, min(max_points, points))
    return normalized


def normalize_grader_payload(payload: dict, proc: subprocess.CompletedProcess) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    passed = bool(payload.get("passed")) and proc.returncode == 0
    breakdown = normalize_breakdown(payload.get("breakdown"), passed)
    score = payload.get("score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = sum(breakdown.values())
    score = max(0, min(100, score))
    reason = str(payload.get("reason") or payload.get("error") or "")
    failure_category = payload.get("failure_category")
    if passed:
        failure_category = None
    elif failure_category not in FAILURE_CATEGORIES:
        failure_category = "grader_error"
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    return {
        "passed": passed,
        "score": score,
        "breakdown": breakdown,
        "metrics": metrics,
        "reason": reason,
        "failure_category": failure_category,
    }


def parse_grader_output(proc: subprocess.CompletedProcess) -> dict:
    payload = {}
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if payload:
        return normalize_grader_payload(payload, proc)
    reason = (proc.stdout + proc.stderr).strip() or f"grader exited {proc.returncode}"
    return normalize_grader_payload({
        "passed": False,
        "score": 0,
        "reason": reason,
        "failure_category": "grader_error",
    }, proc)


def run_grader(case_dir: Path, workspace: Path, trace_path: Path,
               final_path: Path, stdout_path: Path, stderr_path: Path) -> tuple[dict, subprocess.CompletedProcess]:
    args = [
        sys.executable,
        str(case_dir / "grader.py"),
        "--workspace", str(workspace),
        "--trace", str(trace_path),
        "--final", str(final_path),
        "--stdout", str(stdout_path),
        "--stderr", str(stderr_path),
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(args, 124, exc.stdout or "", exc.stderr or "")
        return failure_result(
            reason="grader timed out after 120s",
            failure_category="test_timeout",
        ), proc
    return parse_grader_output(proc), proc


def run_docker_grader(
    *,
    trusted_eval_root: Path,
    trusted_case_dir: Path,
    workspace: Path,
    trace_path: Path,
    final_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    config: EvalExecutionConfig,
    case_deadline: float,
) -> tuple[dict, subprocess.CompletedProcess, dict]:
    timeout = remaining_timeout(case_deadline, config.docker_timeout)
    runner = DockerGraderRunner(
        image=config.docker_image,
        case_name=trusted_case_dir.name,
        memory=config.docker_memory,
        cpus=config.docker_cpus,
        pids_limit=config.docker_pids_limit,
        timeout=timeout,
        operation_deadline=case_deadline,
        cleanup_deadline=case_deadline + CLEANUP_GRACE_SECONDS,
    )
    proc, metadata = runner.run(
        trusted_eval_root=trusted_eval_root,
        grading_workspace=workspace,
        trace_path=trace_path,
        final_path=final_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    if metadata["timed_out"]:
        result = failure_result(
            reason=f"grader exceeded the remaining case budget ({timeout:g}s)",
            failure_category="test_timeout",
        )
    else:
        result = parse_grader_output(proc)
    return result, proc, metadata


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prepare_docker_disposable_paths(case_output: Path, *paths: Path):
    """Prepare only per-case copies for the fallback UID used by root hosts."""
    for path in paths:
        if path.exists():
            prepare_disposable_tree(path, allowed_root=case_output)


def _docker_agent_process(payload: dict, result_connection):
    """Child-process entry point for one Docker-backed Agent phase."""
    config = EvalExecutionConfig(**payload["execution_config"])
    executor = None
    deadline = payload["case_deadline"]
    try:
        executor = command_executor_for_case(
            config,
            Path(payload["agent_workspace"]),
            payload["case_name"],
            container_name=payload["container_name"],
            operation_deadline=deadline,
        )
        if payload["case_name"] == "_child_process_exception":
            raise RuntimeError("scripted child process failure")
        if payload["case_name"] == "_child_no_result":
            os._exit(7)
        with open(payload["stdout_path"], "w", encoding="utf-8") as stdout_handle, \
                open(payload["stderr_path"], "w", encoding="utf-8") as stderr_handle, \
                contextlib.redirect_stdout(stdout_handle), contextlib.redirect_stderr(stderr_handle):
            run_info = run_agent_task(
                payload["task"],
                payload["agent_workspace"],
                payload["trace_path"],
                model_client=(ScriptedEvalClient(payload["case_name"])
                              if payload["scripted"] else None),
                model_provider="scripted" if payload["scripted"] else None,
                model="scripted-eval" if payload["scripted"] else None,
                command_executor=executor,
                tool_policy=DOCKER_EVAL_TOOL_POLICY,
                case_deadline=deadline,
                trace_storage_root=payload["trace_storage_root"],
            )
        result_connection.send({
            "ok": True,
            "run_info": run_info,
            "execution_metadata": executor.execution_metadata(),
        })
    except BaseException as exc:
        try:
            result_connection.send({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "execution_metadata": (executor.execution_metadata()
                                       if executor else {}),
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        result_connection.close()


def _run_isolated_agent_phase(
    *,
    task: str,
    case_name: str,
    agent_workspace: Path,
    trace_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    scripted: bool,
    config: EvalExecutionConfig,
    case_deadline: float | None = None,
) -> tuple[dict, str, dict]:
    """Run the complete host Agent loop in a killable per-case process."""
    container_name = (
        f"codepilot-agent-{DockerCommandExecutor._safe_name(case_name)}-"
        f"{uuid.uuid4().hex[:10]}"
    )
    case_deadline = case_deadline or (time.monotonic() + config.docker_timeout)
    payload = {
        "task": task,
        "case_name": case_name,
        "agent_workspace": str(agent_workspace),
        "trace_path": str(trace_path),
        "trace_storage_root": str(trace_path.parent / "agent_runtime"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "scripted": scripted,
        "container_name": container_name,
        "case_deadline": case_deadline,
        "execution_config": {
            "backend": config.backend,
            "docker_image": config.docker_image,
            "docker_memory": config.docker_memory,
            "docker_cpus": config.docker_cpus,
            "docker_pids_limit": config.docker_pids_limit,
            "docker_timeout": config.docker_timeout,
        },
    }
    context = multiprocessing.get_context("spawn")
    result_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_docker_agent_process,
        args=(payload, child_connection),
        name=f"codepilot-eval-{case_name}",
    )
    payload_result = None
    channel_error = ""
    timed_out = False

    def terminate_child(cleanup_deadline: float):
        if process.is_alive():
            process.terminate()
            process.join(remaining_timeout(cleanup_deadline, 1.5))
        if process.is_alive():
            process.kill()
            process.join(remaining_timeout(cleanup_deadline))

    try:
        process.start()
        child_connection.close()
        while payload_result is None:
            remaining = remaining_timeout(case_deadline)
            if remaining <= 0:
                timed_out = True
                break
            ready = wait_for_connection(
                [result_connection, process.sentinel], timeout=remaining)
            if result_connection in ready:
                try:
                    payload_result = result_connection.recv()
                except (EOFError, OSError) as exc:
                    channel_error = f"{type(exc).__name__}: {exc}"
                break
            if process.sentinel in ready:
                process.join(0)
                if result_connection.poll():
                    try:
                        payload_result = result_connection.recv()
                    except (EOFError, OSError) as exc:
                        channel_error = f"{type(exc).__name__}: {exc}"
                break
    except KeyboardInterrupt:
        cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        terminate_child(cleanup_deadline)
        result_connection.close()
        raise
    except BaseException:
        cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        terminate_child(cleanup_deadline)
        result_connection.close()
        raise
    finally:
        child_connection.close()

    if timed_out:
        cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        terminate_child(cleanup_deadline)

        if config.backend == "docker":
            cleanup = command_executor_for_case(
                config, agent_workspace, case_name,
                container_name=container_name,
                verify_workspace_write=False,
                operation_deadline=cleanup_deadline,
            )
            cleanup.overall_timed_out = True
            cleanup.container_timed_out = True
            cleanup.stop(deadline=cleanup_deadline)
            metadata = cleanup.execution_metadata()
        else:
            metadata = LocalCommandExecutor().execution_metadata()
            metadata.update({
                "overall_timed_out": True,
                "container_timed_out": False,
            })
        metadata["resource_limits"] = configured_resource_limits(config)
        metadata["agent_process_exit_code"] = process.exitcode
        result_connection.close()
        return {}, f"CaseTimeoutError: agent case exceeded {config.docker_timeout:g}s", metadata

    if payload_result is None:
        cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        terminate_child(cleanup_deadline)
        if config.backend == "docker":
            cleanup = command_executor_for_case(
                config, agent_workspace, case_name,
                container_name=container_name,
                verify_workspace_write=False,
                operation_deadline=cleanup_deadline,
            )
            cleanup.stop(deadline=cleanup_deadline)
            metadata = cleanup.execution_metadata()
        else:
            metadata = LocalCommandExecutor().execution_metadata()
        metadata["agent_process_exit_code"] = process.exitcode
        result_connection.close()
        detail = f" ({channel_error})" if channel_error else ""
        return {}, f"AgentProcessError: child exited {process.exitcode} without a result{detail}", metadata

    # A result means the Agent work is complete. Reap the process within the
    # same case budget; use only the shared cleanup grace if it lingers.
    try:
        process.join(remaining_timeout(case_deadline))
    except KeyboardInterrupt:
        terminate_child(case_deadline + CLEANUP_GRACE_SECONDS)
        result_connection.close()
        raise
    if process.is_alive():
        terminate_child(case_deadline + CLEANUP_GRACE_SECONDS)
    result_connection.close()

    metadata = payload_result.get("execution_metadata", {})
    if (config.backend == "docker"
            and metadata.get("container_cleanup_succeeded") is not True):
        cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        cleanup = command_executor_for_case(
            config, agent_workspace, case_name,
            container_name=container_name,
            verify_workspace_write=False,
            operation_deadline=cleanup_deadline,
        )
        cleanup.stop(deadline=cleanup_deadline)
        cleanup_metadata = cleanup.execution_metadata()
        metadata["container_started"] = (
            metadata.get("container_started", False)
            or cleanup_metadata.get("container_started", False))
        if cleanup_metadata.get("container_exit_code") is not None:
            metadata["container_exit_code"] = cleanup_metadata["container_exit_code"]
        metadata["container_cleanup_succeeded"] = cleanup_metadata.get(
            "container_cleanup_succeeded", False)
    metadata["resource_limits"] = configured_resource_limits(config)
    metadata["agent_process_exit_code"] = process.exitcode
    if "CaseTimeoutError" in payload_result.get("error", ""):
        metadata["overall_timed_out"] = True
        if config.backend == "docker":
            metadata["container_timed_out"] = True
    return (
        payload_result.get("run_info", {}),
        "" if payload_result.get("ok") else payload_result.get("error", "AgentProcessError"),
        metadata,
    )


def run_case(case_dir: Path, run_root: Path, scripted: bool,
             execution_config: EvalExecutionConfig | None = None) -> dict:
    execution_config = execution_config or EvalExecutionConfig()
    start = time.perf_counter()
    case_deadline = (
        time.monotonic() + execution_config.docker_timeout
        if execution_config.backend == "docker" else None
    )
    case_name = case_dir.name
    case_output = run_root / case_name
    trusted_eval_root = case_output / "trusted_eval"
    agent_workspace = case_output / "agent_workspace"
    grading_workspace = case_output / "grading_workspace"
    trace_path = case_output / "trace.jsonl"
    stdout_path = case_output / "stdout.txt"
    stderr_path = case_output / "stderr.txt"
    final_path = case_output / "final.md"
    transcript_path = case_output / "transcript.md"
    grader_stdout_path = case_output / "grader_stdout.txt"
    grader_stderr_path = case_output / "grader_stderr.txt"
    change_manifest_path = case_output / "change_manifest.json"

    require_case_time(case_deadline, "case setup")
    case_output.mkdir(parents=True, exist_ok=True)
    trusted_before = trusted_input_snapshot(case_dir)
    trusted_case_dir = copy_trusted_case(case_dir, trusted_eval_root, case_name)
    metadata = load_metadata(trusted_case_dir)
    original_snapshot = workspace_snapshot(trusted_case_dir / "workspace")
    copy_case_workspace(trusted_case_dir, agent_workspace)
    if execution_config.backend == "docker":
        prepare_docker_disposable_paths(case_output, agent_workspace)
    require_case_time(case_deadline, "workspace preparation")
    task = (trusted_case_dir / "task.md").read_text(encoding="utf-8")
    command_executor = None

    agent_error = ""
    run_info = {}
    if execution_config.backend == "docker":
        run_info, agent_error, execution_metadata = _run_isolated_agent_phase(
            task=task,
            case_name=case_name,
            agent_workspace=agent_workspace,
            trace_path=trace_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            scripted=scripted,
            config=execution_config,
            case_deadline=case_deadline,
        )
    else:
        command_executor = command_executor_for_case(
            execution_config, agent_workspace, case_name)
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                run_info = run_agent_task(
                    task,
                    str(agent_workspace),
                    str(trace_path),
                    model_client=ScriptedEvalClient(case_name) if scripted else None,
                    model_provider="scripted" if scripted else None,
                    model="scripted-eval" if scripted else None,
                    command_executor=command_executor,
                )
        except Exception as exc:
            agent_error = f"{type(exc).__name__}: {exc}"
        finally:
            write_text(stdout_path, stdout_buffer.getvalue())
            write_text(stderr_path, stderr_buffer.getvalue())
        execution_metadata = command_executor.execution_metadata()

    if not stdout_path.exists():
        write_text(stdout_path, "")
    if not stderr_path.exists():
        write_text(stderr_path, "")

    source_final_value = run_info.get("final_path")
    source_final = Path(source_final_value) if source_final_value else None
    if source_final and source_final.is_file():
        shutil.copy2(source_final, final_path)
    elif agent_error:
        write_text(final_path, f"[Error] {agent_error}")
    else:
        write_text(final_path, run_info.get("final_answer", ""))

    final_content = final_path.read_text(encoding="utf-8", errors="replace") if final_path.exists() else ""
    if not agent_error and final_content.lstrip().startswith("[Error]"):
        agent_error = final_content.strip()

    transcript = [
        f"# {case_name}",
        "",
        "## Task",
        "",
        task.strip(),
        "",
        "## Final Answer",
        "",
        final_content,
    ]
    if agent_error:
        transcript.extend(["", "## Agent Error", "", agent_error])
    write_text(transcript_path, "\n".join(transcript).rstrip() + "\n")

    agent_snapshot = workspace_snapshot(agent_workspace)
    change_manifest = build_change_manifest(
        before=original_snapshot,
        after=agent_snapshot,
        metadata=metadata,
    )
    write_text(change_manifest_path, json.dumps(change_manifest, indent=2))
    trusted_after = trusted_input_snapshot(case_dir)
    trusted_violations = trusted_input_changes(trusted_before, trusted_after)

    grader_execution = {
        "execution_backend": execution_config.backend,
        "docker_image": execution_config.docker_image if execution_config.backend == "docker" else None,
        "timed_out": False,
        "cleanup_succeeded": None if execution_config.backend == "docker" else True,
        "status": "not_started" if execution_config.backend == "docker" else "not_applicable",
        "container_started": False,
        "container_exit_code": None,
        "resource_limits": {},
    }
    if trusted_violations:
        grader_result = failure_result(
            reason="trusted case inputs were modified: " + ", ".join(trusted_violations),
            failure_category="constraint_violation",
            metrics={"trusted_violations": trusted_violations},
        )
        grader_proc = subprocess.CompletedProcess([], 1, "", grader_result["reason"])
        try:
            create_grading_workspace(
                case_dir=trusted_case_dir,
                agent_workspace=agent_workspace,
                grading_workspace=grading_workspace,
                manifest=change_manifest,
            )
        except Exception:
            pass
    elif agent_error:
        grader_result = failure_result(
            reason=f"agent failed before grading: {agent_error}",
            failure_category=agent_failure_category(agent_error),
        )
        grader_proc = subprocess.CompletedProcess([], 1, "", grader_result["reason"])
    else:
        try:
            require_case_time(case_deadline, "grading workspace preparation")
            create_grading_workspace(
                case_dir=trusted_case_dir,
                agent_workspace=agent_workspace,
                grading_workspace=grading_workspace,
                manifest=change_manifest,
            )
            require_case_time(case_deadline, "grading workspace preparation")
            if execution_config.backend == "docker":
                prepare_docker_disposable_paths(
                    case_output, trusted_eval_root, grading_workspace,
                    trace_path, final_path, stdout_path, stderr_path,
                )
                require_case_time(case_deadline, "grader input preparation")
                grader_execution["status"] = "starting"
                grader_result, grader_proc, grader_details = run_docker_grader(
                    trusted_eval_root=trusted_eval_root,
                    trusted_case_dir=trusted_case_dir,
                    workspace=grading_workspace,
                    trace_path=trace_path,
                    final_path=final_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    config=execution_config,
                    case_deadline=case_deadline,
                )
                grader_execution.update(grader_details)
                grader_execution["status"] = "completed"
            else:
                grader_result, grader_proc = run_grader(
                    trusted_case_dir, grading_workspace, trace_path, final_path, stdout_path, stderr_path)
        except Exception as exc:
            if isinstance(exc, SandboxError):
                grader_execution.update(
                    getattr(exc, "execution_metadata", {}))
            grader_reason = f"grader failed to run: {type(exc).__name__}: {exc}"
            grader_result = failure_result(
                reason=grader_reason,
                failure_category=("sandbox_error" if isinstance(exc, SandboxError)
                                  else "test_timeout" if isinstance(exc, CaseTimeoutError)
                                  else "grader_error"),
            )
            grader_proc = subprocess.CompletedProcess([], 1, "", grader_reason)
            if (execution_config.backend == "docker"
                    and grader_execution["status"] != "not_started"):
                grader_execution["status"] = "failed"

    write_text(grader_stdout_path, grader_proc.stdout or "")
    write_text(grader_stderr_path, grader_proc.stderr or "")

    duration_ms = int((time.perf_counter() - start) * 1000)
    if agent_error:
        grader_result = normalize_grader_payload({
            "passed": False,
            "score": 0,
            "reason": f"agent failed: {agent_error}",
            "failure_category": agent_failure_category(agent_error),
        }, subprocess.CompletedProcess([], 1, "", ""))
    elif execution_metadata.get("sandbox_error"):
        grader_result = failure_result(
            reason=f"agent sandbox failed: {execution_metadata['sandbox_error']}",
            failure_category="sandbox_error",
        )
    elif execution_metadata.get("overall_timed_out"):
        grader_result = failure_result(
            reason="agent sandbox exceeded its overall timeout",
            failure_category="tool_loop",
        )
    elif execution_metadata.get("command_timed_out"):
        grader_result = failure_result(
            reason="agent bash command timed out and the sandbox was terminated",
            failure_category="command_timeout",
        )
    elif change_manifest["unexpected_changes"] or change_manifest["forbidden_changes"]:
        violations = sorted(set(change_manifest["unexpected_changes"] + change_manifest["forbidden_changes"]))
        grader_result = failure_result(
            reason="unexpected or forbidden changes: " + ", ".join(violations),
            failure_category="constraint_violation",
        )

    metrics = {
        **trace_metrics(trace_path),
        **grader_result.get("metrics", {}),
        "runtime_sec": round(duration_ms / 1000, 3),
        "trusted_violations": trusted_violations,
    }

    if execution_config.backend == "docker":
        agent_cleanup = execution_metadata.get("container_cleanup_succeeded")
        grader_attempted = grader_execution.get("status") != "not_started"
        grader_cleanup = (grader_execution.get("cleanup_succeeded")
                          if grader_attempted else None)
        lifecycle = {
            "agent_container_started": execution_metadata.get("container_started", False),
            "agent_container_exit_code": execution_metadata.get("container_exit_code"),
            "agent_container_cleanup_succeeded": agent_cleanup,
            "grader_container_started": (grader_execution.get("container_started")
                                           if grader_attempted else None),
            "grader_container_exit_code": (grader_execution.get("container_exit_code")
                                             if grader_attempted else None),
            "grader_container_cleanup_succeeded": grader_cleanup,
            "all_container_cleanup_succeeded": (
                agent_cleanup is True and grader_cleanup is True),
        }
    else:
        lifecycle = {
            "agent_container_started": None,
            "agent_container_exit_code": None,
            "agent_container_cleanup_succeeded": None,
            "grader_container_started": None,
            "grader_container_exit_code": None,
            "grader_container_cleanup_succeeded": None,
            "all_container_cleanup_succeeded": True,
        }

    return {
        "case": case_name,
        "metadata": metadata,
        "passed": grader_result["passed"],
        "score": grader_result["score"],
        "breakdown": grader_result["breakdown"],
        "metrics": metrics,
        "reason": grader_result["reason"],
        "failure_category": grader_result["failure_category"],
        "error": "" if grader_result["passed"] else grader_result["reason"],
        "duration_ms": duration_ms,
        "workspace": str(agent_workspace),
        "agent_workspace": str(agent_workspace),
        "grading_workspace": str(grading_workspace),
        "change_manifest": str(change_manifest_path),
        "unexpected_changes": change_manifest["unexpected_changes"],
        "forbidden_changes": change_manifest["forbidden_changes"],
        "submitted_changes": change_manifest["submitted_changes"],
        "trusted_violations": trusted_violations,
        "trusted_case": str(trusted_case_dir),
        "trace": str(trace_path),
        "transcript": str(transcript_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "final": str(final_path),
        "grader": grader_result,
        "run": run_info,
        **execution_metadata,
        "grader_execution": grader_execution,
        **lifecycle,
        "container_cleanup_succeeded": lifecycle["all_container_cleanup_succeeded"],
    }


def discover_cases(cases_dir: Path) -> list[Path]:
    return sorted(
        case for case in cases_dir.iterdir()
        if case.is_dir()
        and (case / "task.md").exists()
        and (case / "workspace").is_dir()
        and (case / "grader.py").exists()
    )


def grouped_stats(results: list[dict], key_fn) -> dict:
    groups: dict[str, list[dict]] = {}
    for result in results:
        key = str(key_fn(result))
        groups.setdefault(key, []).append(result)
    stats = {}
    for key, items in sorted(groups.items()):
        total = len(items)
        passed = sum(1 for item in items if item["passed"])
        stats[key] = {
            "total_cases": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total else 0,
            "avg_score": sum(item["score"] for item in items) / total if total else 0,
            "avg_tool_calls": sum(item["metrics"].get("tool_calls", 0) for item in items) / total if total else 0,
            "avg_runtime_sec": sum(item["metrics"].get("runtime_sec", 0) for item in items) / total if total else 0,
        }
    return stats


def failure_category_counts(results: list[dict]) -> dict:
    counts = {}
    for result in results:
        category = result.get("failure_category")
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def agent_failure_category(agent_error: str) -> str:
    lowered = agent_error.lower()
    if "casetimeouterror" in lowered or "agent case exceeded" in lowered:
        return "tool_loop"
    if "sandboxerror" in lowered or "docker sandbox" in lowered or "docker daemon" in lowered:
        return "sandbox_error"
    if "timeout" in lowered or "timed out" in lowered:
        return "api_timeout"
    if "urlerror" in lowered or "model request failed" in lowered or "missing api key" in lowered:
        return "model_error"
    return "grader_error"


def build_summary(*, started: float, cases_dir: Path, run_root: Path,
                  mode: str, results: list[dict],
                  interrupted: bool = False, interrupt_reason: str = "",
                  execution_config: EvalExecutionConfig | None = None) -> dict:
    execution_config = execution_config or EvalExecutionConfig()
    total_cases = len(results)
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "started_at": started,
        "finished_at": time.time(),
        "duration_ms": int((time.time() - started) * 1000),
        "mode": mode,
        "execution_backend": execution_config.backend,
        "docker_image": execution_config.docker_image if execution_config.backend == "docker" else None,
        "container_started": any(
            result.get("agent_container_started") is True
            or result.get("grader_container_started") is True for result in results),
        "container_exit_code": next(
            (result.get("container_exit_code") for result in reversed(results)
             if result.get("container_exit_code") is not None),
            None,
        ),
        "container_timed_out": any(
            result.get("container_timed_out", False)
            or result.get("grader_execution", {}).get("timed_out", False)
            for result in results),
        "container_cleanup_succeeded": (
            True if execution_config.backend != "docker" else bool(results) and all(
                result.get("all_container_cleanup_succeeded") is True for result in results)),
        "agent_container_cleanup_succeeded": (
            None if execution_config.backend != "docker" else bool(results) and all(
                result.get("agent_container_cleanup_succeeded") is True for result in results)),
        "grader_container_cleanup_succeeded": (
            None if execution_config.backend != "docker" else bool(results) and all(
                result.get("grader_container_cleanup_succeeded") is True for result in results)),
        "all_container_cleanup_succeeded": (
            True if execution_config.backend != "docker" else bool(results) and all(
                result.get("all_container_cleanup_succeeded") is True for result in results)),
        "agent_container_exit_code": next(
            (result.get("agent_container_exit_code") for result in reversed(results)
             if result.get("agent_container_exit_code") is not None), None),
        "grader_container_exit_code": next(
            (result.get("grader_container_exit_code") for result in reversed(results)
             if result.get("grader_container_exit_code") is not None), None),
        "command_execution_count": sum(
            result.get("command_execution_count", 0) for result in results
        ),
        "resource_limits": configured_resource_limits(execution_config),
        "interrupted": interrupted,
        "interrupt_reason": interrupt_reason,
        "cases_dir": str(cases_dir),
        "run_root": str(run_root),
        "total": total_cases,
        "total_cases": total_cases,
        "passed": passed_count,
        "failed": total_cases - passed_count,
        "pass_rate": passed_count / total_cases if total_cases else 0,
        "avg_score": sum(result["score"] for result in results) / total_cases if total_cases else 0,
        "avg_tool_calls": sum(result["metrics"].get("tool_calls", 0) for result in results) / total_cases if total_cases else 0,
        "avg_runtime_sec": sum(result["metrics"].get("runtime_sec", 0) for result in results) / total_cases if total_cases else 0,
        "suites": grouped_stats(results, lambda result: result["metadata"].get("suite", "unknown")),
        "difficulty": grouped_stats(results, lambda result: result["metadata"].get("difficulty", "unknown")),
        "failure_categories": failure_category_counts(results),
        "results": results,
    }


def case_exception_result(case: Path, run_root: Path, exc: Exception,
                          execution_config: EvalExecutionConfig | None = None) -> dict:
    execution_config = execution_config or EvalExecutionConfig()
    reason = f"case failed before completion: {type(exc).__name__}: {exc}"
    metadata = load_metadata(case)
    category = ("sandbox_error" if isinstance(exc, SandboxError)
                else "test_timeout" if isinstance(exc, CaseTimeoutError)
                else "grader_error")
    result = failure_result(reason=reason, failure_category=category)
    return {
        "case": case.name,
        "metadata": metadata,
        "passed": False,
        "score": result["score"],
        "breakdown": result["breakdown"],
        "metrics": {"runtime_sec": 0},
        "reason": reason,
        "failure_category": category,
        "error": reason,
        "duration_ms": 0,
        "workspace": str(run_root / case.name / "agent_workspace"),
        "agent_workspace": str(run_root / case.name / "agent_workspace"),
        "grading_workspace": str(run_root / case.name / "grading_workspace"),
        "change_manifest": str(run_root / case.name / "change_manifest.json"),
        "unexpected_changes": [],
        "forbidden_changes": [],
        "submitted_changes": [],
        "trusted_violations": [],
        "trace": str(run_root / case.name / "trace.jsonl"),
        "transcript": str(run_root / case.name / "transcript.md"),
        "stdout": str(run_root / case.name / "stdout.txt"),
        "stderr": str(run_root / case.name / "stderr.txt"),
        "final": str(run_root / case.name / "final.md"),
        "grader": result,
        "run": {},
        "execution_backend": execution_config.backend,
        "docker_image": execution_config.docker_image if execution_config.backend == "docker" else None,
        "container_started": False,
        "container_exit_code": None,
        "container_timed_out": False,
        "container_cleanup_succeeded": False,
        "agent_container_started": False if execution_config.backend == "docker" else None,
        "agent_container_exit_code": None,
        "agent_container_cleanup_succeeded": False if execution_config.backend == "docker" else None,
        "grader_container_started": None,
        "grader_container_exit_code": None,
        "grader_container_cleanup_succeeded": None,
        "all_container_cleanup_succeeded": False if execution_config.backend == "docker" else True,
        "command_execution_count": 0,
        "resource_limits": configured_resource_limits(execution_config),
        "grader_execution": {
            "status": "not_started" if execution_config.backend == "docker" else "not_applicable",
            "cleanup_succeeded": None if execution_config.backend == "docker" else True,
            "container_started": None,
            "container_exit_code": None,
        },
    }


def write_summary(results_dir: Path, summary: dict):
    summary_path = results_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Codepilot eval cases.")
    parser.add_argument("--cases-dir", default=str(Path(__file__).parent / "cases"))
    parser.add_argument("--results-dir", default=str(Path(__file__).parent / "results"))
    parser.add_argument("--case", action="append", default=[],
                        help="Run only the named case. Can be provided more than once.")
    parser.add_argument("--list-cases", action="store_true",
                        help="List discovered cases and exit.")
    parser.add_argument("--request-timeout", type=float, default=float(os.getenv("MODEL_REQUEST_TIMEOUT", "30")),
                        help="Per model HTTP request timeout in seconds. Default: 30.")
    parser.add_argument("--scripted", action="store_true",
                        help="Use the deterministic local scripted client for offline harness smoke tests. By default evals call the configured model API.")
    parser.add_argument("--execution", choices=("local", "docker"), default="local",
                        help="Where agent bash commands and grading run. Default: local.")
    parser.add_argument("--docker-image", default="codepilot-s20-eval:py311",
                        help="Eval sandbox image name.")
    parser.add_argument("--docker-build", action="store_true",
                        help="Build the pinned eval image before running cases.")
    parser.add_argument("--docker-memory", default="1g",
                        help="Container memory and memory-swap limit.")
    parser.add_argument("--docker-cpus", default="1",
                        help="Container CPU limit.")
    parser.add_argument("--docker-pids-limit", type=int, default=128,
                        help="Container process limit.")
    parser.add_argument("--docker-timeout", type=float, default=120,
                        help="Total wall-clock budget for one Docker case, including Agent, grading, and cleanup.")
    args = parser.parse_args()
    os.environ["MODEL_REQUEST_TIMEOUT"] = str(args.request_timeout)

    cases_dir = Path(args.cases_dir).resolve()
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    run_root = results_dir / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_root.mkdir(parents=True, exist_ok=True)

    started = time.time()
    cases = discover_cases(cases_dir)
    if args.case:
        selected = set(args.case)
        cases = [case for case in cases if case.name in selected]
        missing = sorted(selected - {case.name for case in cases})
        if missing:
            parser.error("unknown eval case(s): " + ", ".join(missing))
    if args.scripted:
        unsupported = [case.name for case in cases
                       if not load_metadata(case).get("scripted_supported", False)]
        if args.case and unsupported:
            parser.error(
                "scripted mode is not supported for: " + ", ".join(unsupported))
        if not args.case:
            cases = [case for case in cases
                     if load_metadata(case).get("scripted_supported", False)]
    if args.list_cases:
        for case in cases:
            metadata = load_metadata(case)
            print(f"{case.name}\tsuite={metadata.get('suite')}\tdifficulty={metadata.get('difficulty')}\tcategory={metadata.get('category')}")
        return 0
    execution_config = EvalExecutionConfig(
        backend=args.execution,
        docker_image=args.docker_image,
        docker_memory=args.docker_memory,
        docker_cpus=args.docker_cpus,
        docker_pids_limit=args.docker_pids_limit,
        docker_timeout=args.docker_timeout,
    )
    results = []
    mode = "scripted" if args.scripted else "real-model"
    print(
        f"[eval] mode={mode} execution={args.execution} cases={len(cases)} request_timeout={args.request_timeout}s "
        f"provider={os.getenv('MODEL_PROVIDER', '')} model={os.getenv('MODEL_ID', '')}",
        flush=True,
    )
    if args.execution == "docker" and args.docker_build:
        try:
            print(f"[eval] building Docker image {args.docker_image}", flush=True)
            build_eval_image(project_root=PROJECT_ROOT, image=args.docker_image)
        except SandboxError as exc:
            print(f"[eval] Docker build failed: {exc}", flush=True)
            results = [case_exception_result(case, run_root, exc, execution_config) for case in cases]
            summary = build_summary(
                started=started, cases_dir=cases_dir, run_root=run_root,
                mode=mode, results=results, execution_config=execution_config,
            )
            summary_path = write_summary(results_dir, summary)
            print(json.dumps({"summary": str(summary_path), "passed": 0,
                              "failed": summary["failed"]}, indent=2))
            return 1
    interrupted = False
    interrupt_reason = ""
    for index, case in enumerate(cases, start=1):
        case_started = time.time()
        print(f"[eval] start {index}/{len(cases)} {case.name}", flush=True)
        try:
            result = run_case(case, run_root, args.scripted, execution_config)
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            reason = f" reason={result['reason']}" if result.get("reason") else ""
            print(
                f"[eval] done  {index}/{len(cases)} {case.name} {status} "
                f"score={result['score']} elapsed={time.time() - case_started:.1f}s{reason}",
                flush=True,
            )
        except KeyboardInterrupt:
            interrupted = True
            interrupt_reason = f"Interrupted while running {case.name}"
            print(f"[eval] interrupted during {case.name}; partial summary will be written", flush=True)
            break
        except Exception as exc:
            result = case_exception_result(case, run_root, exc, execution_config)
            results.append(result)
            print(
                f"[eval] done  {index}/{len(cases)} {case.name} FAIL "
                f"score=0 elapsed={time.time() - case_started:.1f}s reason={result['reason']}",
                flush=True,
            )
        finally:
            write_summary(
                results_dir,
                build_summary(
                    started=started,
                    cases_dir=cases_dir,
                    run_root=run_root,
                    mode=mode,
                    results=results,
                    interrupted=interrupted,
                    interrupt_reason=interrupt_reason,
                    execution_config=execution_config,
                ),
            )

    summary = build_summary(
        started=started,
        cases_dir=cases_dir,
        run_root=run_root,
        mode=mode,
        results=results,
        interrupted=interrupted,
        interrupt_reason=interrupt_reason,
        execution_config=execution_config,
    )
    summary_path = write_summary(results_dir, summary)
    print(json.dumps({
        "summary": str(summary_path),
        "passed": summary["passed"],
        "failed": summary["failed"],
    }, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
