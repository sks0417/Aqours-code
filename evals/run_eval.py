from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import fnmatch
import hashlib
from pathlib import Path
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
}

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
                return response([tool_block("bash", {"command": f"{sys.executable} -m pytest -q"}, "call_pytest")])
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
        elif key in {"difficulty", "max_turns", "max_tool_calls"}:
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
    root_resolved = root.resolve()
    path_resolved = path.resolve(strict=False)
    try:
        relative = path_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc
    text = relative.as_posix()
    if Path(text).is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {text}")
    return text


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern.replace("\\", "/")) for pattern in patterns)


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
    for path in sorted(workspace.rglob("*")):
        rel = posix_relative(workspace, path)
        if is_runtime_artifact(rel):
            continue
        try:
            stat = path.lstat()
        except OSError:
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            snapshot[rel] = {
                "path": rel,
                "sha256": None,
                "type": "symlink",
                "size": 0,
            }
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
    proc = subprocess.run(
        [
            sys.executable,
            str(case_dir / "grader.py"),
            "--workspace", str(workspace),
            "--trace", str(trace_path),
            "--final", str(final_path),
            "--stdout", str(stdout_path),
            "--stderr", str(stderr_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return parse_grader_output(proc), proc


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_case(case_dir: Path, run_root: Path, scripted: bool) -> dict:
    case_name = case_dir.name
    metadata = load_metadata(case_dir)
    case_output = run_root / case_name
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

    case_output.mkdir(parents=True, exist_ok=True)
    original_snapshot = workspace_snapshot(case_dir / "workspace")
    copy_case_workspace(case_dir, agent_workspace)
    task = (case_dir / "task.md").read_text(encoding="utf-8")

    start = time.perf_counter()
    agent_error = ""
    run_info = {}
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
            )
    except Exception as exc:
        agent_error = f"{type(exc).__name__}: {exc}"
    finally:
        write_text(stdout_path, stdout_buffer.getvalue())
        write_text(stderr_path, stderr_buffer.getvalue())

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

    try:
        create_grading_workspace(
            case_dir=case_dir,
            agent_workspace=agent_workspace,
            grading_workspace=grading_workspace,
            manifest=change_manifest,
        )
        grader_result, grader_proc = run_grader(
            case_dir, grading_workspace, trace_path, final_path, stdout_path, stderr_path)
    except Exception as exc:
        grader_reason = f"grader failed to run: {type(exc).__name__}: {exc}"
        grader_result = normalize_grader_payload({
            "passed": False,
            "score": 0,
            "reason": grader_reason,
            "failure_category": "grader_error",
        }, subprocess.CompletedProcess([], 1, "", ""))
        grader_proc = subprocess.CompletedProcess([], 1, "", grader_reason)

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
    elif change_manifest["unexpected_changes"] or change_manifest["forbidden_changes"]:
        violations = sorted(set(change_manifest["unexpected_changes"] + change_manifest["forbidden_changes"]))
        grader_result = normalize_grader_payload({
            "passed": False,
            "score": 0,
            "reason": "unexpected or forbidden changes: " + ", ".join(violations),
            "failure_category": "constraint_violation",
        }, subprocess.CompletedProcess([], 1, "", ""))

    metrics = {
        **trace_metrics(trace_path),
        **grader_result.get("metrics", {}),
        "runtime_sec": round(duration_ms / 1000, 3),
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
        "trace": str(trace_path),
        "transcript": str(transcript_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "final": str(final_path),
        "grader": grader_result,
        "run": run_info,
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
    if "timeout" in lowered or "timed out" in lowered:
        return "api_timeout"
    if "urlerror" in lowered or "model request failed" in lowered or "missing api key" in lowered:
        return "model_error"
    return "grader_error"


def build_summary(*, started: float, cases_dir: Path, run_root: Path,
                  mode: str, results: list[dict],
                  interrupted: bool = False, interrupt_reason: str = "") -> dict:
    total_cases = len(results)
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "started_at": started,
        "finished_at": time.time(),
        "duration_ms": int((time.time() - started) * 1000),
        "mode": mode,
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
    if args.list_cases:
        for case in cases:
            metadata = load_metadata(case)
            print(f"{case.name}\tsuite={metadata.get('suite')}\tdifficulty={metadata.get('difficulty')}\tcategory={metadata.get('category')}")
        return 0
    results = []
    mode = "scripted" if args.scripted else "real-model"
    print(
        f"[eval] mode={mode} cases={len(cases)} request_timeout={args.request_timeout}s "
        f"provider={os.getenv('MODEL_PROVIDER', '')} model={os.getenv('MODEL_ID', '')}",
        flush=True,
    )
    interrupted = False
    interrupt_reason = ""
    for index, case in enumerate(cases, start=1):
        case_started = time.time()
        print(f"[eval] start {index}/{len(cases)} {case.name}", flush=True)
        try:
            result = run_case(case, run_root, args.scripted)
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
