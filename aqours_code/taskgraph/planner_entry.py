"""Planner child process: run one planner agent.

    python -m aqours_code.taskgraph.planner_entry [--stage draft|ground] --config <config.json>

The same child-process runner as ``worker_entry`` (``CountingClient``, model
configuration, result file), with a tool policy that cannot change files or
run commands. ``--stage ground`` (the default, Step 2) may read files;
``--stage draft`` (Step 1) has no file tools at all, so it works from the
request alone.
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


DRAFT_TOOL_POLICY: dict = {**PLANNER_TOOL_POLICY, "name": "taskgraph_planner_draft",
                           "allowed_tools": ["compact"]}
STAGE_POLICIES = {"ground": PLANNER_TOOL_POLICY, "draft": DRAFT_TOOL_POLICY}


def run_planner(config: dict, model_client=None, stage: str = "ground") -> WorkerResult:
    """Run one planner round with the tool policy of ``stage``."""
    return run_worker(config, model_client=model_client, tool_policy=STAGE_POLICIES[stage])


def main(argv: list[str] | None = None) -> int:
    """Run the planner round described by ``--config`` and write its result file."""
    parser = argparse.ArgumentParser(prog="python -m aqours_code.taskgraph.planner_entry")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config")
    group.add_argument("--describe", action="store_true")
    parser.add_argument("--stage", choices=sorted(STAGE_POLICIES), default="ground")
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(describe()))
        return 0
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    os.environ["AQOURS_CODE_WORKDIR"] = config["workspace"]
    try:
        result = run_planner(config, stage=args.stage)
    except Exception as exc:  # noqa: BLE001 - always leave a result file
        result = WorkerResult(ok=False, reason="worker_error",
                              error=f"{type(exc).__name__}: {exc}")
    write_json_atomic(Path(config["result_path"]), result.to_dict())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
