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
from evals.docker_sandbox import (  # noqa: E402
    DockerAgentRunner,
    DockerGraderRunner,
    build_eval_image,
)
from codepilot_s20.docker_utils import prepare_disposable_tree  # noqa: E402
from codepilot_s20.config import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MODEL,
    MODEL_PROVIDER,
    get_model_client,
)
from codepilot_s20.model_broker import (  # noqa: E402
    DEFAULT_IPC_DELIVERY_GRACE,
    DEFAULT_PROVIDER_RETRIES,
    DEFAULT_PROVIDER_RETRY_DELAY,
    ModelBroker,
    broker_ipc_wait_timeout,
)
from evals.scoring import (  # noqa: E402
    BREAKDOWN_WEIGHTS,
    PROFILE_METADATA_KEYS,
    apply_harness_scoring,
)


DEFAULT_BREAKDOWN_WEIGHTS = dict(BREAKDOWN_WEIGHTS)
FAILURE_CATEGORIES = {
    None,
    "test_failure",
    "constraint_violation",
    "tool_loop",
    "budget_exhausted",
    "grader_error",
    "model_error",
    "api_timeout",
    "provider_http_timeout",
    "broker_ipc_timeout",
    "case_timeout",
    "test_timeout",
    "command_timeout",
    "sandbox_error",
}
CLEANUP_GRACE_SECONDS = 3.0
DEFAULT_MODEL_CALLS_PER_CASE = 32
MAX_MODEL_CALLS_PER_CASE = 64


def remaining_timeout(deadline: float | None, configured: float | None = None) -> float:
    """Return the remaining shared budget, optionally capped per operation."""
    if deadline is None:
        return float(configured) if configured is not None else 0.0
    remaining = max(0.0, deadline - time.monotonic())
    return min(remaining, float(configured)) if configured is not None else remaining


def model_broker_timeouts(request_timeout: float, remaining: float) -> tuple[float, float]:
    """Return Provider and IPC timeouts within the shared case budget."""
    remaining = max(0.1, float(remaining))
    provider_timeout = min(max(0.1, float(request_timeout)), remaining)
    ipc_timeout = min(
        broker_ipc_wait_timeout(
            provider_timeout,
            max_provider_retries=DEFAULT_PROVIDER_RETRIES,
            retry_delay=DEFAULT_PROVIDER_RETRY_DELAY,
            delivery_grace=DEFAULT_IPC_DELIVERY_GRACE,
        ),
        remaining,
    )
    return provider_timeout, ipc_timeout


def require_case_time(deadline: float | None, stage: str):
    if deadline is not None and remaining_timeout(deadline) <= 0:
        raise CaseTimeoutError(f"eval case deadline exceeded during {stage}")

RUNTIME_IGNORE_PATTERNS = [
    ".git/**",
    ".codepilot/**",
    ".tasks/**",
    ".task_outputs/**",
    ".transcripts/**",
    ".mailboxes/**",
    ".worktrees/**",
    ".pytest_cache/**",
    "__pycache__/**",
    "*/__pycache__/**",
    "*.pyc",
]
CASE_COPY_IGNORE = shutil.ignore_patterns(
    ".pytest_cache", "__pycache__", "*.pyc", "*.pyo",
)
TAMPER_ENTRY_NAMES = {"pytest.py", "conftest.py", "sitecustomize.py", "usercustomize.py"}
TRUSTED_ROOT_FILES = {"task.md", "metadata.yaml", "grader.py"}
TRUSTED_DIRS = {"workspace", "grader_tests", "agent_state"}
DOCKER_EVAL_TOOL_POLICY = {
    "name": "docker_eval_full_harness",
    "allowed_tools": [
        "bash", "read_file", "write_file", "edit_file", "glob", "todo_write",
        "task", "delegate_agent", "load_skill", "compact", "create_task", "list_tasks",
        "get_task", "claim_task", "complete_task", "schedule_cron",
        "schedule_once", "list_crons", "cancel_cron", "spawn_teammate",
        "send_message", "check_inbox", "request_shutdown", "request_plan",
        "review_plan", "create_worktree", "remove_worktree", "keep_worktree",
        "integrate_worktree",
        "connect_mcp",
    ],
    "disabled_tools": [],
    "allow_mcp": True,
    "allow_memory_context": True,
    "allow_skill_context": True,
    "allow_teammate_context": True,
    "background_tasks": True,
    "prompt_runtime": {
        "os": "Linux",
        "platform": "Docker Linux container",
        "shell": "/bin/sh",
        "path_separator": "/",
        "workdir": "/workspace",
        "command_hints": ["Use Linux-compatible shell commands."],
    },
}


@dataclass(frozen=True)
class EvalExecutionConfig:
    backend: str = "docker"
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
                return response([
                    tool_block(
                        "bash",
                        {"command": "printf 'written in sandbox\\n' > from_bash.txt"},
                        "call_bash_write",
                    ),
                    tool_block(
                        "create_worktree", {"name": "smoke-wt"},
                        "call_create_worktree",
                    ),
                ])
            return response([text_block("Created from_bash.txt through sandboxed bash.")])

        if self.case_name == "_docker_broker_policy_smoke":
            if not results:
                command = """python - <<'PY'
import json
import os
import time
import uuid
from pathlib import Path

config = json.loads(Path('/runtime/input.json').read_text())
responses = []
for model, max_tokens in (
    ('unauthorized-expensive-model', 8000),
    (config['model'], 1000000000),
):
    request_id = uuid.uuid4().hex
    name = f"{config['broker_nonce']}-{request_id}.json"
    request_path = Path('/broker/requests') / name
    temporary = request_path.with_suffix('.tmp')
    temporary.write_text(json.dumps({
        'version': 1,
        'nonce': config['broker_nonce'],
        'request_id': request_id,
        'method': 'messages.create',
        'params': {
            'model': model,
            'messages': [],
            'max_tokens': max_tokens,
        },
    }))
    os.replace(temporary, request_path)
    response_path = Path('/broker/responses') / name
    deadline = time.monotonic() + 5
    while not response_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    responses.append(json.loads(response_path.read_text()))
Path('/workspace/broker_policy.json').write_text(json.dumps(responses))
PY"""
                return response([tool_block(
                    "bash", {"command": command}, "call_broker_policy")])
            return response([text_block("Broker policy rejections recorded.")])

        if self.case_name == "_docker_background_lifecycle_smoke":
            saw_notification = any(
                "<task_notification>" in str(message.get("content"))
                for message in messages
                if isinstance(message, dict)
            )
            if not results:
                return response([tool_block(
                    "bash",
                    {
                        "command": (
                            "sleep 2.2; printf complete > background_done.txt"),
                        "run_in_background": True,
                    },
                    "call_slow_background",
                )])
            if not saw_notification:
                return response([text_block("finishing before notification")])
            if not any(
                result.get("tool_use_id") == "call_notification_seen"
                for result in results
            ):
                return response([tool_block(
                    "write_file",
                    {"path": "notification_seen.txt", "content": "seen"},
                    "call_notification_seen",
                )])
            return response([text_block("Background completion was observed.")])

        if self.case_name == "_docker_noninteractive_permission_smoke":
            return response([tool_block(
                "bash",
                {"command": "echo unsafe > /etc/codepilot-eval"},
                "call_destructive_noninteractive",
            )])

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
        try:
            return float(text)
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
        "max_model_calls": None,
        "max_model_tokens": None,
        "forbidden_paths": [],
        "expected_artifacts": [],
        "allowed_changes": [],
        "scripted_supported": False,
        **{metadata_key: None for metadata_key in PROFILE_METADATA_KEYS.values()},
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
        elif key in {
            "difficulty", "max_model_calls",
            "max_model_tokens", "scripted_supported",
            *PROFILE_METADATA_KEYS.values(),
        }:
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
    duplicate_tool_calls = 0
    previous_tool_signature = None
    for event in events:
        if event.get("type") != "tool_use":
            continue
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        signature = (
            str(event.get("tool") or event.get("name") or ""),
            json.dumps(tool_input, sort_keys=True, ensure_ascii=False),
        )
        if signature == previous_tool_signature:
            duplicate_tool_calls += 1
        previous_tool_signature = signature
    return {
        "tool_calls": sum(
            1 for event in events if event.get("type") == "tool_use"),
        "llm_requests": sum(
            1 for event in events if event.get("type") == "llm_request"),
        "permission_blocks": sum(
            1 for event in events
            if event.get("type") == "hook"
            and event.get("name") == "PreToolUse"
            and event.get("decision") == "blocked"
        ),
        "duplicate_tool_calls": duplicate_tool_calls,
        "event_count": len(events),
    }


def model_budgets_for_case(metadata: dict) -> tuple[int, int]:
    """Bind broker spend to trusted case metadata, not container requests."""
    raw_calls = metadata.get("max_model_calls")
    if raw_calls is None:
        raw_calls = DEFAULT_MODEL_CALLS_PER_CASE
    calls = int(raw_calls)
    if calls <= 0 or calls > MAX_MODEL_CALLS_PER_CASE:
        raise ValueError(
            f"max_model_calls must be between 1 and {MAX_MODEL_CALLS_PER_CASE}")

    raw_tokens = metadata.get("max_model_tokens")
    if raw_tokens is None:
        # Normal calls request 8k; one 16k recovery is allowed within the same
        # explicit call budget.
        raw_tokens = calls * DEFAULT_MAX_TOKENS + (
            ESCALATED_MAX_TOKENS - DEFAULT_MAX_TOKENS)
    tokens = int(raw_tokens)
    if tokens <= 0 or tokens > calls * ESCALATED_MAX_TOKENS:
        raise ValueError(
            "max_model_tokens must be positive and no greater than "
            "max_model_calls * 16000")
    return calls, tokens


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
        ignore=CASE_COPY_IGNORE,
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
    shutil.copy2(PROJECT_ROOT / "evals" / "scoring.py", trusted_eval_root / "scoring.py")
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
                ignore=CASE_COPY_IGNORE,
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
            try:
                prepare_disposable_tree(path, allowed_root=case_output)
            except OSError as exc:
                raise SandboxError(
                    "unable to prepare disposable workspace for the non-root "
                    f"Docker user at {path}: {type(exc).__name__}: {exc}"
                ) from exc


AGENT_STATE_DIRS = (
    "skills", ".memory", ".tasks", ".mailboxes", ".worktrees",
    ".task_outputs", ".transcripts",
)


def prepare_agent_state(case_dir: Path, state_root: Path):
    """Create a fresh per-case Harness state tree from eval-only fixtures."""
    if state_root.exists():
        shutil.rmtree(state_root)
    state_root.mkdir(parents=True)
    for dirname in AGENT_STATE_DIRS:
        (state_root / dirname).mkdir(parents=True, exist_ok=True)
    fixture_root = case_dir / "agent_state"
    if fixture_root.is_dir():
        symlinks = [path for path in fixture_root.rglob("*") if path.is_symlink()]
        if symlinks:
            raise ValueError(
                f"agent state fixture may not contain symlinks: {symlinks[0]}")
        for source in fixture_root.iterdir():
            target = state_root / source.name
            if source.is_symlink():
                raise ValueError(f"agent state fixture may not be a symlink: {source}")
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, symlinks=False)
            elif source.is_file():
                shutil.copy2(source, target, follow_symlinks=False)


def _copy_runtime_artifact(runtime_root: Path, name: str, target: Path):
    source = runtime_root / name
    if source.is_file():
        shutil.copy2(source, target)


def _model_broker_process(payload: dict):
    """Killable host process that owns model credentials and provider I/O."""
    model_client = (
        ScriptedEvalClient(payload["case_name"])
        if payload["scripted"]
        else get_model_client(payload["model_provider"])
    )
    broker = ModelBroker(
        payload["ipc_root"],
        payload["nonce"],
        model_client,
        allowed_model=payload["allowed_model"],
        case_deadline=payload["case_deadline"],
        max_calls=payload["max_calls"],
        max_tokens_per_call=ESCALATED_MAX_TOKENS,
        max_total_tokens=payload["max_total_tokens"],
        provider_timeout=payload["provider_timeout"],
        max_provider_retries=DEFAULT_PROVIDER_RETRIES,
        provider_retry_delay=DEFAULT_PROVIDER_RETRY_DELAY,
        delivery_grace=DEFAULT_IPC_DELIVERY_GRACE,
    )
    broker.serve_forever()


def _run_docker_agent_phase(
    *,
    task: str,
    case_name: str,
    agent_workspace: Path,
    agent_state: Path,
    runtime_root: Path,
    ipc_root: Path,
    trace_path: Path,
    timeline_path: Path,
    timeline_md_path: Path,
    metadata_path: Path,
    final_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    scripted: bool,
    model_call_budget: int,
    model_token_budget: int,
    config: EvalExecutionConfig,
    case_deadline: float,
) -> tuple[dict, str, dict]:
    """Run the full Agent Runtime in a one-shot container."""
    nonce = uuid.uuid4().hex
    runtime_root.mkdir(parents=True, exist_ok=True)
    ipc_root.mkdir(parents=True, exist_ok=True)
    (ipc_root / "requests").mkdir(parents=True, exist_ok=True)
    (ipc_root / "responses").mkdir(parents=True, exist_ok=True)
    (ipc_root / "stats").mkdir(parents=True, exist_ok=True)
    remaining = remaining_timeout(case_deadline)
    provider_timeout, broker_request_timeout = model_broker_timeouts(
        float(os.getenv("MODEL_REQUEST_TIMEOUT", "30")), remaining)
    input_payload = {
        "task": task,
        "workspace": "/workspace",
        "state_root": "/state",
        "runtime_root": "/runtime",
        "ipc_root": "/broker",
        "broker_nonce": nonce,
        "model_call_budget": model_call_budget,
        "broker_max_provider_retries": DEFAULT_PROVIDER_RETRIES,
        "model": "scripted-eval" if scripted else MODEL,
        # request_timeout remains the Provider timeout for backward-compatible
        # runtime configuration. The container waits through one Broker-owned
        # retry plus the final response delivery grace.
        "request_timeout": provider_timeout,
        "broker_request_timeout": broker_request_timeout,
        "case_timeout_seconds": remaining,
        "cleanup_grace": CLEANUP_GRACE_SECONDS,
        "tool_policy": DOCKER_EVAL_TOOL_POLICY,
    }
    write_text(runtime_root / "input.json", json.dumps(input_payload, indent=2))
    try:
        prepare_docker_disposable_paths(
            runtime_root.parent, agent_workspace, agent_state, runtime_root, ipc_root)
    except BaseException:
        for path in (ipc_root, agent_state):
            try:
                shutil.rmtree(path)
            except OSError:
                pass
        raise

    broker_context = multiprocessing.get_context("spawn")
    broker_process = broker_context.Process(
        target=_model_broker_process,
        args=({
            "ipc_root": str(ipc_root),
            "nonce": nonce,
            "case_name": case_name,
            "scripted": scripted,
            "model_provider": MODEL_PROVIDER,
            "allowed_model": input_payload["model"],
            "max_calls": model_call_budget,
            "max_total_tokens": model_token_budget,
            "case_deadline": case_deadline,
            "provider_timeout": provider_timeout,
        },),
        name=f"codepilot-model-broker-{case_name}",
    )
    try:
        broker_process.start()
    except (OSError, RuntimeError) as exc:
        for path in (ipc_root, agent_state):
            try:
                shutil.rmtree(path)
            except OSError:
                pass
        raise SandboxError(
            f"Model Broker failed to start: {type(exc).__name__}: {exc}") from exc
    runner = DockerAgentRunner(
        image=config.docker_image,
        case_name=case_name,
        memory=config.docker_memory,
        cpus=config.docker_cpus,
        pids_limit=config.docker_pids_limit,
        timeout=remaining,
        operation_deadline=case_deadline,
        cleanup_deadline=case_deadline + CLEANUP_GRACE_SECONDS,
    )
    proc = subprocess.CompletedProcess([], 1, "", "Agent did not start")
    metadata = {}
    broker_stopped = False
    unexpected_error = None
    try:
        proc, metadata = runner.run(
            agent_workspace=agent_workspace,
            agent_state=agent_state,
            runtime_root=runtime_root,
            ipc_root=ipc_root,
        )
    except SandboxError as exc:
        metadata = {
            "execution_backend": "docker",
            "docker_image": config.docker_image,
            "container_started": runner.container_started,
            "container_exit_code": runner.container_exit_code,
            "container_timed_out": runner.timed_out,
            "container_cleanup_succeeded": runner.cleanup_succeeded,
            "resource_limits": runner.resource_limits,
            "sandbox_error": str(exc),
            "container_entrypoint": "python -m codepilot_s20.eval_container_entry",
        }
        proc = subprocess.CompletedProcess([], 125, "", str(exc))
    except BaseException as exc:
        unexpected_error = exc
    finally:
        broker_cleanup_deadline = case_deadline + CLEANUP_GRACE_SECONDS
        if broker_process.is_alive():
            broker_process.terminate()
            broker_process.join(remaining_timeout(broker_cleanup_deadline))
        if broker_process.is_alive():
            broker_process.kill()
            broker_process.join(remaining_timeout(broker_cleanup_deadline))
        broker_stopped = not broker_process.is_alive()

    if unexpected_error is not None:
        for path in (ipc_root, agent_state):
            try:
                shutil.rmtree(path)
            except OSError:
                pass
        raise unexpected_error

    write_text(stdout_path, proc.stdout or "")
    write_text(stderr_path, proc.stderr or "")
    for name, target in (
        ("trace.jsonl", trace_path),
        ("timeline.jsonl", timeline_path),
        ("timeline.md", timeline_md_path),
        ("metadata.json", metadata_path),
        ("final.md", final_path),
    ):
        _copy_runtime_artifact(runtime_root, name, target)

    result_payload = {}
    result_path = runtime_root / "result.json"
    if result_path.is_file():
        try:
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result_payload = {"ok": False, "error": f"invalid Agent result: {exc}"}
    if not result_payload:
        if metadata.get("container_timed_out"):
            error = "CaseTimeoutError: Agent container exceeded the case deadline"
        else:
            error = (proc.stderr or f"Agent container exited {proc.returncode}").strip()
        result_payload = {"ok": False, "error": error}

    run_info = result_payload.get("run_info", {})
    command_metadata = result_payload.get("execution")
    command_metadata_source = "result"
    if not isinstance(command_metadata, dict):
        command_metadata = (
            run_info.get("execution", {}) if isinstance(run_info, dict) else {})
        command_metadata_source = (
            "run_info" if isinstance(command_metadata, dict) and command_metadata
            else "unknown")
    command_count_known = "command_execution_count" in command_metadata
    broker_stats = {}
    broker_stats_path = ipc_root / "stats" / "broker_stats.json"
    if broker_stats_path.is_file():
        try:
            broker_stats = json.loads(broker_stats_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            broker_stats = {}
    metadata.update({
        "model_broker_requests": broker_stats.get("request_count", 0),
        "model_broker_calls": broker_stats.get("call_count", 0),
        "model_broker_rejected_calls": broker_stats.get("rejected_count", 0),
        "model_broker_retries": broker_stats.get("retry_count", 0),
        "model_broker_provider_errors": broker_stats.get(
            "provider_error_count", 0),
        "model_broker_requested_tokens": broker_stats.get(
            "requested_token_count", 0),
        "model_broker_actual_input_tokens": broker_stats.get(
            "actual_input_token_count"),
        "model_broker_actual_output_tokens": broker_stats.get(
            "actual_output_token_count"),
        "model_broker_actual_cache_creation_input_tokens": broker_stats.get(
            "actual_cache_creation_input_token_count"),
        "model_broker_actual_cache_read_input_tokens": broker_stats.get(
            "actual_cache_read_input_token_count"),
        "model_broker_actual_total_tokens": broker_stats.get(
            "actual_total_token_count"),
        "model_broker_usage_responses": broker_stats.get(
            "usage_response_count", 0),
        "model_broker_usage_missing_responses": broker_stats.get(
            "usage_missing_response_count", 0),
        "model_broker_call_budget": broker_stats.get(
            "max_calls", model_call_budget),
        "model_broker_token_budget": broker_stats.get(
            "max_total_tokens", model_token_budget),
        "model_broker_max_tokens_per_call": broker_stats.get(
            "max_tokens_per_call", ESCALATED_MAX_TOKENS),
        "model_broker_error": broker_stats.get("last_error", ""),
        "model_broker_error_kind": broker_stats.get("last_error_kind", ""),
        "model_broker_last_provider_error": broker_stats.get(
            "last_provider_error", ""),
        "model_broker_retry_skipped_reason": broker_stats.get(
            "retry_skipped_reason", ""),
        "model_broker_provider_timeout": broker_stats.get(
            "provider_timeout", provider_timeout),
        "model_broker_ipc_timeout": broker_request_timeout,
        "model_broker_stopped": broker_stopped,
        "model_broker_exit_code": broker_process.exitcode,
        "model_broker_ipc_cleaned": False,
        # Keep the legacy numeric field for old consumers while making an old
        # or malformed result schema distinguishable from a true zero.
        "command_execution_count": command_metadata.get("command_execution_count", 0),
        "command_execution_count_known": command_count_known,
        "command_execution_metadata_source": command_metadata_source,
        "tool_policy": DOCKER_EVAL_TOOL_POLICY,
    })
    try:
        shutil.rmtree(ipc_root)
        metadata["model_broker_ipc_cleaned"] = not ipc_root.exists()
    except OSError:
        metadata["model_broker_ipc_cleaned"] = False
    try:
        shutil.rmtree(agent_state)
        metadata["agent_state_cleaned"] = not agent_state.exists()
    except OSError:
        metadata["agent_state_cleaned"] = False

    error = "" if result_payload.get("ok") else str(
        result_payload.get("error") or f"Agent container exited {proc.returncode}")
    if proc.returncode != 0 and not error:
        error = f"Agent container exited {proc.returncode}"
    if not broker_stopped and not error:
        error = "ModelBrokerCleanupError: host broker process did not stop"
    return run_info, error, metadata


def _isolated_local_agent_process(payload: dict, result_connection):
    """Compatibility child-process entry for killable local-only tests."""
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
                tool_policy=payload.get("tool_policy"),
                case_deadline=deadline,
                trace_storage_root=payload["trace_storage_root"],
                approval_mode="non_interactive",
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
    """Run the explicit local compatibility Agent in a killable process."""
    if config.backend != "local":
        raise ValueError(
            "_run_isolated_agent_phase is local-only; Docker must use the "
            "one-shot eval_container_entry path")
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
        "tool_policy": None,
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
        target=_isolated_local_agent_process,
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
    agent_state = case_output / "agent_state"
    agent_runtime = case_output / "agent_runtime"
    broker_ipc = case_output / "broker_ipc"
    trace_path = case_output / "trace.jsonl"
    timeline_path = case_output / "timeline.jsonl"
    timeline_md_path = case_output / "timeline.md"
    run_metadata_path = case_output / "metadata.json"
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
    model_call_budget, model_token_budget = model_budgets_for_case(metadata)
    original_snapshot = workspace_snapshot(trusted_case_dir / "workspace")
    copy_case_workspace(trusted_case_dir, agent_workspace)
    if execution_config.backend == "docker":
        try:
            prepare_agent_state(trusted_case_dir, agent_state)
            agent_runtime.mkdir(parents=True, exist_ok=True)
            broker_ipc.mkdir(parents=True, exist_ok=True)
            prepare_docker_disposable_paths(
                case_output, agent_workspace, agent_state, agent_runtime, broker_ipc)
        except BaseException:
            for disposable in (agent_state, broker_ipc):
                try:
                    shutil.rmtree(disposable)
                except OSError:
                    pass
            raise
    require_case_time(case_deadline, "workspace preparation")
    task = (trusted_case_dir / "task.md").read_text(encoding="utf-8")
    command_executor = None

    agent_error = ""
    run_info = {}
    agent_started = time.perf_counter()
    if execution_config.backend == "docker":
        run_info, agent_error, execution_metadata = _run_docker_agent_phase(
            task=task,
            case_name=case_name,
            agent_workspace=agent_workspace,
            agent_state=agent_state,
            runtime_root=agent_runtime,
            ipc_root=broker_ipc,
            trace_path=trace_path,
            timeline_path=timeline_path,
            timeline_md_path=timeline_md_path,
            metadata_path=run_metadata_path,
            final_path=final_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            scripted=scripted,
            model_call_budget=model_call_budget,
            model_token_budget=model_token_budget,
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
                    approval_mode="non_interactive",
                )
        except Exception as exc:
            agent_error = f"{type(exc).__name__}: {exc}"
        finally:
            write_text(stdout_path, stdout_buffer.getvalue())
            write_text(stderr_path, stderr_buffer.getvalue())
        execution_metadata = command_executor.execution_metadata()
    agent_duration_sec = time.perf_counter() - agent_started

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
    diagnostic_grader_result = None
    grading_attempted = False
    agent_category = agent_failure_category(agent_error) if agent_error else None
    manifest_violations = sorted(set(
        change_manifest["unexpected_changes"]
        + change_manifest["forbidden_changes"]
    ))
    diagnostic_grading_allowed = bool(
        agent_error
        and agent_category not in {"case_timeout", "sandbox_error"}
        and not execution_metadata.get("sandbox_error")
        and not execution_metadata.get("overall_timed_out")
        and not manifest_violations
    )
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
    elif agent_error and not diagnostic_grading_allowed:
        grader_result = failure_result(
            reason=f"agent failed before grading: {agent_error}",
            failure_category=agent_category,
        )
        grader_proc = subprocess.CompletedProcess([], 1, "", grader_result["reason"])
    else:
        grading_attempted = True
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

    if diagnostic_grading_allowed and grading_attempted:
        diagnostic_grader_result = grader_result

    write_text(grader_stdout_path, grader_proc.stdout or "")
    write_text(grader_stderr_path, grader_proc.stderr or "")

    duration_ms = int((time.perf_counter() - start) * 1000)
    if agent_error:
        grader_result = normalize_grader_payload({
            "passed": False,
            "score": 0,
            "reason": f"agent failed: {agent_error}",
            "failure_category": agent_category,
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
    elif manifest_violations:
        grader_result = failure_result(
            reason="unexpected or forbidden changes: " + ", ".join(manifest_violations),
            failure_category="constraint_violation",
        )

    metrics = {
        **trace_metrics(trace_path),
        **grader_result.get("metrics", {}),
        "trusted_model_calls": (
            execution_metadata.get("model_broker_calls")
            if execution_config.backend == "docker" else None),
        "model_broker_retries": execution_metadata.get(
            "model_broker_retries", 0),
        "model_broker_provider_errors": execution_metadata.get(
            "model_broker_provider_errors", 0),
        "model_broker_requested_tokens": execution_metadata.get(
            "model_broker_requested_tokens"),
        "model_broker_actual_input_tokens": execution_metadata.get(
            "model_broker_actual_input_tokens"),
        "model_broker_actual_output_tokens": execution_metadata.get(
            "model_broker_actual_output_tokens"),
        "model_broker_actual_cache_creation_input_tokens": execution_metadata.get(
            "model_broker_actual_cache_creation_input_tokens"),
        "model_broker_actual_cache_read_input_tokens": execution_metadata.get(
            "model_broker_actual_cache_read_input_tokens"),
        "model_broker_actual_total_tokens": execution_metadata.get(
            "model_broker_actual_total_tokens"),
        "model_broker_usage_responses": execution_metadata.get(
            "model_broker_usage_responses", 0),
        "model_broker_usage_missing_responses": execution_metadata.get(
            "model_broker_usage_missing_responses", 0),
        "agent_runtime_sec": round(agent_duration_sec, 3),
        "runtime_sec": round(duration_ms / 1000, 3),
        "trusted_violations": trusted_violations,
    }
    grader_result = apply_harness_scoring(
        grader_result,
        metrics,
        metadata=metadata,
        simulated=scripted,
    )
    metrics = grader_result["metrics"]

    if execution_config.backend == "docker":
        agent_cleanup = execution_metadata.get("container_cleanup_succeeded")
        agent_phase_cleanup = (
            agent_cleanup is True
            and execution_metadata.get("model_broker_stopped") is True
            and execution_metadata.get("model_broker_ipc_cleaned") is True
            and execution_metadata.get("agent_state_cleaned") is True
        )
        grader_attempted = grader_execution.get("status") != "not_started"
        grader_cleanup = (grader_execution.get("cleanup_succeeded")
                          if grader_attempted else None)
        started_cleanup_values = []
        if execution_metadata.get("container_started") is True:
            started_cleanup_values.append(agent_phase_cleanup)
        if grader_execution.get("container_started") is True:
            started_cleanup_values.append(grader_cleanup)
        started_cleanup_succeeded = all(
            value is True for value in started_cleanup_values)
        lifecycle_complete = (
            execution_metadata.get("container_started") is True
            and (execution_metadata.get("container_exit_code") is not None
                 or execution_metadata.get("container_timed_out") is True)
            and grader_execution.get("status") == "completed"
        )
        lifecycle = {
            "agent_container_started": execution_metadata.get("container_started", False),
            "agent_container_exit_code": execution_metadata.get("container_exit_code"),
            "agent_container_cleanup_succeeded": agent_cleanup,
            "agent_phase_cleanup_succeeded": agent_phase_cleanup,
            "grader_container_started": (grader_execution.get("container_started")
                                           if grader_attempted else None),
            "grader_container_exit_code": (grader_execution.get("container_exit_code")
                                             if grader_attempted else None),
            "grader_container_cleanup_succeeded": grader_cleanup,
            "all_container_cleanup_succeeded": (
                agent_phase_cleanup is True and grader_cleanup is True),
            "all_started_containers_cleanup_succeeded": (
                started_cleanup_succeeded),
            "lifecycle_complete": lifecycle_complete,
        }
    else:
        lifecycle = {
            "agent_container_started": None,
            "agent_container_exit_code": None,
            "agent_container_cleanup_succeeded": None,
            "agent_phase_cleanup_succeeded": True,
            "grader_container_started": None,
            "grader_container_exit_code": None,
            "grader_container_cleanup_succeeded": None,
            "all_container_cleanup_succeeded": True,
            "all_started_containers_cleanup_succeeded": True,
            "lifecycle_complete": True,
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
        "timeline": str(timeline_path),
        "timeline_markdown": str(timeline_md_path),
        "run_metadata": str(run_metadata_path),
        "transcript": str(transcript_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "final": str(final_path),
        "grader": grader_result,
        "diagnostic_score": (
            diagnostic_grader_result["score"]
            if diagnostic_grader_result is not None else None),
        "diagnostic_breakdown": (
            diagnostic_grader_result["breakdown"]
            if diagnostic_grader_result is not None else None),
        "diagnostic_grader": diagnostic_grader_result,
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
        metered = [
            item for item in items
            if item["metrics"].get("model_broker_usage_responses", 0) > 0
        ]
        stats[key] = {
            "total_cases": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total else 0,
            "avg_score": sum(item["score"] for item in items) / total if total else 0,
            "avg_model_calls": sum(
                item["metrics"].get("trusted_model_calls") or 0 for item in items
            ) / total if total else 0,
            "avg_tool_calls": sum(
                item["metrics"].get("tool_calls", 0) for item in items
            ) / total if total else 0,
            "avg_runtime_sec": sum(item["metrics"].get("runtime_sec", 0) for item in items) / total if total else 0,
            "metered_cases": len(metered),
            "avg_actual_tokens": (
                sum(item["metrics"].get("model_broker_actual_total_tokens", 0)
                    for item in metered) / len(metered)
                if metered else None
            ),
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
    if ("model broker call limit exceeded" in lowered
            or "broker model call limit exceeded" in lowered
            or "model broker token budget exceeded" in lowered
            or "broker token budget exceeded" in lowered):
        return "budget_exhausted"
    if ("casetimeouterror" in lowered or "agent case exceeded" in lowered
            or "case_timeout" in lowered or "case deadline" in lowered):
        return "case_timeout"
    if "sandboxerror" in lowered or "docker sandbox" in lowered or "docker daemon" in lowered:
        return "sandbox_error"
    if "broker_ipc_timeout" in lowered:
        return "broker_ipc_timeout"
    if "provider_timeout" in lowered:
        return "provider_http_timeout"
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
    diagnostic_results = [
        result for result in results
        if result.get("diagnostic_score") is not None
    ]
    metered_results = [
        result for result in results
        if result["metrics"].get("model_broker_usage_responses", 0) > 0
    ]
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
        "all_started_containers_cleanup_succeeded": (
            True if execution_config.backend != "docker" else bool(results) and all(
                result.get(
                    "all_started_containers_cleanup_succeeded",
                    result.get("all_container_cleanup_succeeded"),
                ) is True
                for result in results)),
        "lifecycle_complete": (
            True if execution_config.backend != "docker" else bool(results) and all(
                result.get("lifecycle_complete", True) is True
                for result in results)),
        "agent_container_exit_code": next(
            (result.get("agent_container_exit_code") for result in reversed(results)
             if result.get("agent_container_exit_code") is not None), None),
        "grader_container_exit_code": next(
            (result.get("grader_container_exit_code") for result in reversed(results)
             if result.get("grader_container_exit_code") is not None), None),
        "command_execution_count": sum(
            result.get("command_execution_count", 0) for result in results
        ),
        "model_broker_calls": sum(
            result.get("model_broker_calls", 0) for result in results
        ),
        "model_broker_requests": sum(
            result.get("model_broker_requests", 0) for result in results
        ),
        "model_broker_retries": sum(
            result.get("model_broker_retries", 0) for result in results
        ),
        "model_broker_stopped": (
            None if execution_config.backend != "docker" else bool(results) and all(
                result.get("model_broker_stopped") is True for result in results)),
        "model_broker_ipc_cleaned": (
            None if execution_config.backend != "docker" else bool(results) and all(
                result.get("model_broker_ipc_cleaned") is True for result in results)),
        "container_entrypoint": (
            "python -m codepilot_s20.eval_container_entry"
            if execution_config.backend == "docker" else None),
        "tool_policy": (
            DOCKER_EVAL_TOOL_POLICY if execution_config.backend == "docker" else None),
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
        "diagnostically_graded_cases": len(diagnostic_results),
        "avg_diagnostic_score": (
            sum(result["diagnostic_score"] for result in diagnostic_results)
            / len(diagnostic_results)
            if diagnostic_results else None
        ),
        "avg_model_calls": sum(
            result["metrics"].get("trusted_model_calls") or 0
            for result in results
        ) / total_cases if total_cases else 0,
        "avg_tool_calls": sum(
            result["metrics"].get("tool_calls", 0) for result in results
        ) / total_cases if total_cases else 0,
        "avg_runtime_sec": sum(result["metrics"].get("runtime_sec", 0) for result in results) / total_cases if total_cases else 0,
        "metered_cases": len(metered_results),
        "actual_input_tokens": sum(
            result["metrics"].get("model_broker_actual_input_tokens", 0)
            for result in metered_results
        ),
        "actual_output_tokens": sum(
            result["metrics"].get("model_broker_actual_output_tokens", 0)
            for result in metered_results
        ),
        "actual_cache_creation_input_tokens": sum(
            result["metrics"].get(
                "model_broker_actual_cache_creation_input_tokens", 0)
            for result in metered_results
        ),
        "actual_cache_read_input_tokens": sum(
            result["metrics"].get(
                "model_broker_actual_cache_read_input_tokens", 0)
            for result in metered_results
        ),
        "actual_total_tokens": sum(
            result["metrics"].get("model_broker_actual_total_tokens", 0)
            for result in metered_results
        ),
        "avg_actual_tokens": (
            sum(result["metrics"].get("model_broker_actual_total_tokens", 0)
                for result in metered_results) / len(metered_results)
            if metered_results else None
        ),
        "avg_runtime_efficiency_score": (
            sum((result.get("breakdown") or {}).get("runtime_efficiency", 0)
                for result in results) / total_cases if total_cases else 0
        ),
        "avg_token_cost_score": (
            sum((result.get("breakdown") or {}).get("token_cost", 0)
                for result in results) / total_cases if total_cases else 0
        ),
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
        "timeline": str(run_root / case.name / "timeline.jsonl"),
        "timeline_markdown": str(run_root / case.name / "timeline.md"),
        "run_metadata": str(run_root / case.name / "metadata.json"),
        "transcript": str(run_root / case.name / "transcript.md"),
        "stdout": str(run_root / case.name / "stdout.txt"),
        "stderr": str(run_root / case.name / "stderr.txt"),
        "final": str(run_root / case.name / "final.md"),
        "grader": result,
        "diagnostic_score": None,
        "diagnostic_breakdown": None,
        "diagnostic_grader": None,
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
        "all_started_containers_cleanup_succeeded": True,
        "lifecycle_complete": False if execution_config.backend == "docker" else True,
        "command_execution_count": 0,
        "command_execution_count_known": False,
        "command_execution_metadata_source": "unknown",
        "model_broker_calls": 0,
        "model_broker_requests": 0,
        "model_broker_retries": 0,
        "model_broker_stopped": False if execution_config.backend == "docker" else None,
        "model_broker_ipc_cleaned": False if execution_config.backend == "docker" else None,
        "container_entrypoint": (
            "python -m codepilot_s20.eval_container_entry"
            if execution_config.backend == "docker" else None),
        "tool_policy": (
            DOCKER_EVAL_TOOL_POLICY if execution_config.backend == "docker" else None),
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
    parser.add_argument("--execution", choices=("local", "docker"), default="docker",
                        help=("Run the full Agent Runtime and grader in Docker. "
                              "Use local only as an explicit development compatibility mode. "
                              "Default: docker."))
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
            diagnostic = (
                f" diagnostic_score={result['diagnostic_score']}"
                if result.get("diagnostic_score") is not None else ""
            )
            actual_tokens = result["metrics"].get(
                "model_broker_actual_total_tokens")
            token_text = (
                f" actual_tokens={actual_tokens}"
                if result["metrics"].get("model_broker_usage_responses", 0) > 0
                else " actual_tokens=unavailable"
            )
            print(
                f"[eval] done  {index}/{len(cases)} {case.name} {status} "
                f"score={result['score']}{diagnostic} "
                f"elapsed={time.time() - case_started:.1f}s{token_text}{reason}",
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
