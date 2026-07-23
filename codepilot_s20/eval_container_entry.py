from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


_SENSITIVE_ENV_NAMES = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY", "MODEL_API_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AZURE_OPENAI_API_KEY",
}


def _atomic_write_json(path: Path, payload: dict):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _copy_artifact(source: str | Path | None, target: Path):
    if not source:
        return
    source_path = Path(source)
    if source_path.is_file():
        shutil.copy2(source_path, target)


def _initialize_case_repository(workspace: Path):
    """Create a disposable Git baseline so Worktree tools run in-container."""
    if (workspace / ".git").exists():
        return
    commands = (
        ["git", "init", "--initial-branch=eval-main"],
        ["git", "config", "user.name", "CodePilot Eval"],
        ["git", "config", "user.email", "eval@localhost"],
        ["git", "add", "-A"],
        ["git", "commit", "--allow-empty", "-m", "eval baseline"],
    )
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    })
    for command in commands:
        proc = subprocess.run(
            command, cwd=workspace, capture_output=True, text=True,
            timeout=30, env=env)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "git failed").strip()
            raise RuntimeError(f"unable to initialize eval Git repository: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated CodePilot eval Agent.")
    parser.add_argument("--config", default="/runtime/input.json")
    args = parser.parse_args(argv)

    for name in _SENSITIVE_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"

    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    workspace = Path(config["workspace"]).resolve()
    state_root = Path(config["state_root"]).resolve()
    runtime_root = Path(config["runtime_root"]).resolve()
    ipc_root = Path(config["ipc_root"]).resolve()
    for directory in (workspace, state_root, runtime_root, ipc_root):
        if not directory.is_dir():
            raise RuntimeError(f"required eval directory is missing: {directory}")

    timeout = max(0.1, float(config["case_timeout_seconds"]))
    case_deadline = time.monotonic() + timeout
    provider_timeout = max(0.1, float(config.get("request_timeout", 30)))
    broker_request_timeout = max(
        provider_timeout,
        float(config.get("broker_request_timeout", provider_timeout)),
    )
    os.environ["CODEPILOT_S20_WORKDIR"] = str(workspace)
    os.environ["MODEL_PROVIDER"] = "broker"
    os.environ["MODEL_ID"] = str(config["model"])
    # Provider calls happen in the Host Broker and retain the configured
    # per-attempt timeout. The container IPC client waits long enough for the
    # Broker-owned retry and final response delivery.
    os.environ["MODEL_REQUEST_TIMEOUT"] = str(provider_timeout)
    # python-dotenv searches the process cwd. Import the runtime from the
    # isolated non-workspace directory so a case-provided /workspace/.env can
    # never become process configuration.
    os.chdir(runtime_root)
    _initialize_case_repository(workspace)

    from . import bootstrap
    bootstrap()
    from .agent_loop import run_agent_task
    from .command_executor import LocalCommandExecutor
    from .model_broker import BrokerModelClient

    client = BrokerModelClient(
        ipc_root,
        config["broker_nonce"],
        request_timeout=broker_request_timeout,
        case_deadline=case_deadline,
        max_calls=config.get("model_call_budget"),
        max_provider_retries=config.get("broker_max_provider_retries", 0),
    )
    result_path = runtime_root / "result.json"
    command_executor = LocalCommandExecutor()
    try:
        result = run_agent_task(
            str(config["task"]),
            str(workspace),
            str(runtime_root / "trace.jsonl"),
            model_client=client,
            model_provider="broker",
            model=str(config["model"]),
            command_executor=command_executor,
            tool_policy=config.get("tool_policy"),
            case_deadline=case_deadline,
            cleanup_grace=float(config.get("cleanup_grace", 2.0)),
            trace_storage_root=str(runtime_root),
            runtime_root=str(state_root),
            manage_lifecycle=True,
            approval_mode="non_interactive",
        )
        _copy_artifact(result.get("timeline_path"), runtime_root / "timeline.jsonl")
        run_dir = Path(result["run_dir"])
        _copy_artifact(run_dir / "timeline.md", runtime_root / "timeline.md")
        _copy_artifact(run_dir / "metadata.json", runtime_root / "metadata.json")
        _copy_artifact(result.get("final_path"), runtime_root / "final.md")
        _atomic_write_json(result_path, {
            "ok": True,
            "run_info": result,
            "execution": command_executor.execution_metadata(),
        })
        return 0
    except BaseException as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "execution": command_executor.execution_metadata(),
        }
        try:
            _atomic_write_json(result_path, payload)
        except OSError:
            pass
        print(payload["error"], file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
