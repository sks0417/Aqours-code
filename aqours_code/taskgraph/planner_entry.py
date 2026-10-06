"""Planner child process: run the planner agent with read-only tools.

    python -m aqours_code.taskgraph.planner_entry --config <worker_N_config.json>

The same child-process runner as ``worker_entry`` (``CountingClient``, model
configuration, result file), with a tool policy that cannot change files or
run commands.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .worker_entry import describe, run_worker
from .workers import WorkerResult, write_json_atomic

PLANNER_TOOL_POLICY: dict = {
    "name": "taskgraph_planner",
    "allowed_tools": ["read_file", "glob", "compact"],
    "allow_mcp": False,
    "allow_memory_context": False,
    "allow_skill_context": False,
    "allow_teammate_context": False,
    "background_tasks": False,
}


def run_planner(config: dict, model_client=None) -> WorkerResult:
    """Run one planner round with :data:`PLANNER_TOOL_POLICY`."""
    return run_worker(config, model_client=model_client, tool_policy=PLANNER_TOOL_POLICY)


def main(argv: list[str] | None = None) -> int:
    """Run the planner round described by ``--config`` and write its result file."""
    parser = argparse.ArgumentParser(prog="python -m aqours_code.taskgraph.planner_entry")
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
        result = run_planner(config)
    except Exception as exc:  # noqa: BLE001 - always leave a result file
        result = WorkerResult(ok=False, reason="worker_error",
                              error=f"{type(exc).__name__}: {exc}")
    write_json_atomic(Path(config["result_path"]), result.to_dict())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
