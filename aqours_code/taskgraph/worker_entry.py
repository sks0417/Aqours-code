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


def run_worker(config: dict, model_client=None) -> WorkerResult:
    """Run one node attempt with ``run_agent_task`` and return its result."""
    from aqours_code.agent_loop import run_agent_task  # noqa: PLC0415
    from aqours_code.command_executor import (  # noqa: PLC0415
        CaseTimeoutError,
        LocalCommandExecutor,
    )

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
    try:
        info = run_agent_task(
            config["task"],
            config["workspace"],
            config["trace_path"],
            model_client=counting,
            model_provider=provider,
            model=model,
            command_executor=LocalCommandExecutor(),
            tool_policy=WORKER_TOOL_POLICY,
            case_deadline=time.monotonic() + timeout_s,
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
