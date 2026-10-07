"""Worker child process: run one node attempt with the Aqours single-agent path.

    python -m aqours_code.taskgraph.worker_entry --config <worker_N_config.json>

This is the only task graph module that touches the Aqours runtime, and it
imports it lazily inside functions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .workers import WorkerResult, write_json_atomic

# Applied identically to every scheme (single node, sequential, parallel).
WORKER_TOOL_POLICY: dict = {
    "name": "taskgraph_worker",
    "allowed_tools": [
        "bash", "read_file", "write_file", "edit_file", "glob", "todo_write", "compact",
    ],
    "allow_mcp": False,
    "allow_memory_context": False,
    "allow_skill_context": False,
    "allow_teammate_context": False,
    "background_tasks": False,
}

def _usage_value(usage, *names: str) -> int:
    for name in names:
        value = (usage.get(name) if isinstance(usage, dict)
                 else getattr(usage, name, None))
        if isinstance(value, (int, float)):
            return int(value)
    return 0


class CountingClient:
    """Wrap a model client to count calls and tokens without limiting them.

    It exposes no budget information, so the Aqours loop runs exactly as an
    ordinary single-agent run would.
    """

    def __init__(self, inner):
        self.inner = inner
        self.call_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.messages = self

    def create(self, **kwargs):
        """Forward one ``messages.create`` call, counting it first."""
        self.call_count += 1
        response = self.inner.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            # OpenAI-compatible providers report prompt/completion tokens.
            self.input_tokens += _usage_value(usage, "input_tokens", "prompt_tokens")
            self.output_tokens += _usage_value(usage, "output_tokens", "completion_tokens")
        return response


def describe() -> dict:
    """Return the provider and model the worker would use, without calling it."""
    from aqours_code.config import MODEL, MODEL_PROVIDER  # noqa: PLC0415

    return {"model_provider": MODEL_PROVIDER, "model": MODEL}


def command_executor_for(config: dict, deadline: float):
    """The executor for the worker's ``bash`` tool.

    ``config["sandbox"]`` of ``{"kind": "docker", "image": ..., "container": ...}``
    gives a Docker container that sees only ``config["workspace"]``; anything
    else runs commands on the host (``--sandbox none``).
    """
    sandbox = config.get("sandbox") or {}
    if sandbox.get("kind") == "docker":
        from .sandbox import docker_executor  # noqa: PLC0415

        return docker_executor(config["workspace"], sandbox["image"],
                               sandbox["container"], deadline=deadline)
    from aqours_code.command_executor import LocalCommandExecutor  # noqa: PLC0415

    return LocalCommandExecutor()


class ObservedExecutor:
    """Delegate the public executor interface and retain Bash success for the wall."""

    def __init__(self, inner, wall):
        self.inner, self.wall = inner, wall

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, command, cwd, timeout):
        result = self.inner.execute(command, cwd, timeout)
        self.wall.bash_results[command] = (result["exit_code"] == 0 and not result["timed_out"])
        return result


def register_soft_wall(config: dict):
    """Register only for multi-node worker attempts, using public Aqours APIs.

    bootstrap is idempotent; run_agent_task's isolated collections exclude
    hooks.HOOKS. Calling bootstrap before registration also completes initial
    runtime wiring before our callbacks are appended. No global hook is removed.
    """
    settings = config.get("soft_wall") or {}
    if not settings.get("enabled"):
        return None
    from aqours_code import bootstrap  # noqa: PLC0415
    bootstrap()
    from aqours_code.hooks import register_hook, recoverable_tool_rejection  # noqa: PLC0415
    from .soft_wall import SoftWall  # noqa: PLC0415

    wall = SoftWall(Path(config["workspace"]), settings["own_files"],
                    settings["full_files"], Path(settings["log_path"]))
    register_hook("PreToolUse", lambda block: wall.pre_tool(block, recoverable_tool_rejection))
    register_hook("PostToolUse", wall.post_tool)
    return wall


def run_worker(config: dict, model_client=None,
               tool_policy: dict | None = None) -> WorkerResult:
    """Run one node attempt with ``run_agent_task`` and return its result.

    ``tool_policy`` defaults to :data:`WORKER_TOOL_POLICY`.
    """
    from aqours_code.agent_loop import run_agent_task  # noqa: PLC0415
    from aqours_code.command_executor import CaseTimeoutError  # noqa: PLC0415

    started = time.monotonic()
    if model_client is None:
        from aqours_code.config import (  # noqa: PLC0415
            MODEL,
            MODEL_PROVIDER,
            client,
            validate_runtime_configuration,
        )
        try:
            validate_runtime_configuration()
        except RuntimeError as exc:
            return WorkerResult(ok=False, reason="worker_error", error=str(exc))
        inner, provider, model = client, MODEL_PROVIDER, MODEL
    else:
        inner = model_client
        provider = config.get("model_provider", "scripted")
        model = config.get("model", "scripted")

    counting = CountingClient(inner)
    timeout_s = float(config["timeout_s"])
    for key in ("trace_storage_root", "runtime_root"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)
    result = WorkerResult(ok=True)
    deadline = time.monotonic() + timeout_s
    wall = register_soft_wall(config)
    try:
        executor = command_executor_for(config, deadline)
        if wall is not None:
            executor = ObservedExecutor(executor, wall)
        info = run_agent_task(
            config["task"],
            config["workspace"],
            config["trace_path"],
            model_client=counting,
            model_provider=provider,
            model=model,
            command_executor=executor,
            tool_policy=tool_policy or WORKER_TOOL_POLICY,
            case_deadline=deadline,
            trace_storage_root=config["trace_storage_root"],
            runtime_root=config["runtime_root"],
            manage_lifecycle=True,
            approval_mode="non_interactive",
        )
        result.final_answer = str(info.get("final_answer", ""))
    except CaseTimeoutError as exc:
        result = WorkerResult(ok=False, reason="worker_timeout", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported to the coordinator
        result = WorkerResult(ok=False, reason="worker_error",
                              error=f"{type(exc).__name__}: {exc}")

    finally:
        if wall is not None:
            # Production workers exit; in-process scripted tests must not leave
            # active callbacks affecting later workers or planner runs.
            wall.active = False

    answer = result.final_answer.lstrip()
    if result.ok and (answer.startswith("[Error]")
                      or answer.lower().startswith("permission denied")):
        result.ok, result.reason, result.error = False, "worker_error", answer[:2000]
    result.model_calls = counting.call_count
    result.input_tokens = counting.input_tokens
    result.output_tokens = counting.output_tokens
    result.duration_s = time.monotonic() - started
    return result


def main(argv: list[str] | None = None) -> int:
    """Run the worker described by ``--config`` and write its result file."""
    parser = argparse.ArgumentParser(prog="python -m aqours_code.taskgraph.worker_entry")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config")
    group.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(describe()))
        return 0
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    os.environ["AQOURS_CODE_WORKDIR"] = config["workspace"]
    try:
        result = run_worker(config)
    except Exception as exc:  # noqa: BLE001 - always leave a result file
        result = WorkerResult(ok=False, reason="worker_error",
                              error=f"{type(exc).__name__}: {exc}")
    write_json_atomic(Path(config["result_path"]), result.to_dict())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
