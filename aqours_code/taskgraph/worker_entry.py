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


# Calls left when only a token limit is set and no request size is known yet.
UNKNOWN_CALLS_LEFT = 1_000_000


class BudgetedCountingClient(CountingClient):
    """A CountingClient that shows the Aqours loop a model-call budget.

    Only the planner gets one: its share of the plan's limits. The loop reads
    it through ``budget_snapshot()`` (``aqours_code.model_budget``), the
    interface of the Docker broker's call budget: near the end it asks for
    the final answer, at the last call it disables tools, and with no call
    left it stops before sending a request. An input-token limit is shown as
    the calls it leaves at the size of the last request, at least one while
    any tokens are left, so the agent is asked for its answer before it runs
    out.
    """

    def __init__(self, inner, max_calls: int | None = None,
                 max_input_tokens: int | None = None):
        super().__init__(inner)
        self.max_calls, self.max_input_tokens = max_calls, max_input_tokens
        self.last_input_tokens = 0
        self.ran_out = ""  # the limit that stopped the loop before it answered

    def create(self, **kwargs):
        """Forward one call, remembering the size of its request."""
        before = self.input_tokens
        response = super().create(**kwargs)
        self.last_input_tokens = self.input_tokens - before
        return response

    def calls_left(self) -> tuple[int, str]:
        """The calls left and the limit that sets them."""
        left = [(UNKNOWN_CALLS_LEFT, "")]
        if self.max_calls is not None:
            left.append((max(0, self.max_calls - self.call_count), "model_calls"))
        if self.max_input_tokens is not None:
            tokens = self.max_input_tokens - self.input_tokens
            if tokens <= 0:
                left.append((0, "input_tokens"))
            elif self.last_input_tokens > 0:
                left.append((max(1, tokens // self.last_input_tokens), "input_tokens"))
        return min(left)

    def budget_snapshot(self) -> dict:
        """What ``model_budget.model_budget_snapshot`` reads."""
        left, limit = self.calls_left()
        if left <= 0:
            self.ran_out = limit
        # model_budget ignores a maximum of 0, so no call left before the first
        # one is shown as one call, already used
        maximum = max(1, self.call_count + left)
        return {"source": "taskgraph_plan", "max_calls": maximum,
                "call_count": maximum - left, "max_provider_retries": 0}


def counting_client(inner, budget: dict | None) -> CountingClient:
    """A BudgetedCountingClient for a ``model_budget`` with a limit, else a CountingClient."""
    budget = budget or {}
    if budget.get("max_calls") is None and budget.get("max_input_tokens") is None:
        return CountingClient(inner)
    return BudgetedCountingClient(inner, budget.get("max_calls"), budget.get("max_input_tokens"))


def describe() -> dict:
    """Return the provider, model and output limits the worker would use, without calling it.

    ``max_tokens`` is ``{"initial", "escalated"}``: the output limit of each
    model call and of the retry after a reply cut at it (config.model_max_tokens).
    If AQOURS_CODE_MAX_TOKENS or AQOURS_CODE_ESCALATED_MAX_TOKENS is invalid,
    it is None and ``max_tokens_error`` says why (the worker itself then fails).
    """
    from aqours_code.config import MODEL, MODEL_PROVIDER, model_max_tokens  # noqa: PLC0415

    info = {"model_provider": MODEL_PROVIDER, "model": MODEL}
    try:
        info["max_tokens"] = model_max_tokens(MODEL_PROVIDER, MODEL or "")
    except RuntimeError as exc:
        info["max_tokens"], info["max_tokens_error"] = None, str(exc)
    return info


def command_executor_for(config: dict, deadline: float):
    """The executor for the worker's ``bash`` tool.

    ``config["sandbox"]`` of ``{"kind": "docker", "image": ..., "container": ...}``
    (and optionally ``"env"``, variables set in the container, ``"git_view"``,
    whether git sees the worktree through a view, and ``"nofile"`` and
    ``"pids"``, its limits of open files and processes) gives a Docker
    container that sees only ``config["workspace"]``; anything else runs
    commands on the host (``--sandbox none``).
    """
    sandbox = config.get("sandbox") or {}
    if sandbox.get("kind") == "docker":
        from .sandbox import docker_executor  # noqa: PLC0415

        return docker_executor(config["workspace"], sandbox["image"],
                               sandbox["container"], deadline=deadline,
                               env=sandbox.get("env") or {},
                               nofile=sandbox.get("nofile"), pids=sandbox.get("pids"),
                               git_view=bool(sandbox.get("git_view")))
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


def register_repo_guard(config: dict):
    """For the planner and the team lead: read_file and glob only in the workspace."""
    if not config.get("repo_guard"):
        return None
    from aqours_code import bootstrap  # noqa: PLC0415
    bootstrap()
    from aqours_code.hooks import register_hook, recoverable_tool_rejection  # noqa: PLC0415
    from .repo_guard import RepoGuard  # noqa: PLC0415

    guard = RepoGuard(Path(config["workspace"]))
    register_hook("PreToolUse", lambda block: guard.pre_tool(block, recoverable_tool_rejection))
    return guard


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

    counting = counting_client(inner, config.get("model_budget"))
    timeout_s = float(config["timeout_s"])
    for key in ("trace_storage_root", "runtime_root"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)
    result = WorkerResult(ok=True)
    deadline = time.monotonic() + timeout_s
    wall = register_soft_wall(config)
    guard = register_repo_guard(config)
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
        if guard is not None:
            guard.active = False

    answer = result.final_answer.lstrip()
    if result.ok and (answer.startswith("[Error]")
                      or answer.lower().startswith("permission denied")):
        result.ok, result.reason, result.error = False, "worker_error", answer[:2000]
    ran_out = getattr(counting, "ran_out", "")
    if result.ok and ran_out:
        # the loop stopped before a request over the budget: no answer was given
        result.ok, result.reason = False, "model_budget"
        result.error = f"the {ran_out} limit was reached before the agent answered"
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
