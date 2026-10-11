"""experiments/taskgraph/planner_eval/run_eval.py: case table and summary (no model)."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "experiments" / "taskgraph"
          / "planner_eval" / "run_eval.py")


@pytest.fixture(scope="module")
def run_eval():
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_case_points_at_existing_files(run_eval):
    assert set(run_eval.CASES) == {"jp-modular", "jp-coupled", "jr-modular", "jr-coupled"}
    for request, graph in run_eval.CASES.values():
        assert (run_eval.TASKS / request).is_file()
        assert (run_eval.TASKS / graph).is_file()


def test_parse_case(run_eval, tmp_path):
    assert run_eval.parse_case(f"jp-modular={tmp_path}") == ("jp-modular", tmp_path)
    for bad in (f"unknown={tmp_path}", "jp-modular", f"jp-modular={tmp_path / 'missing'}"):
        with pytest.raises(argparse.ArgumentTypeError):
            run_eval.parse_case(bad)


def test_summary_table(run_eval):
    rows = [
        {"case": "jp-modular", "run": 1, "success": True, "draft_items": 6,
         "fast_path": False, "nodes_before_merge": 9, "revisions": 0, "nodes": 7,
         "critical_path": 2, "max_parallel_width": 6, "test_only_nodes": 0,
         "mean_similarity": 0.8125, "model_calls": 30, "input_tokens": 900,
         "output_tokens": 50, "note": ""},
        {"case": "jp-coupled", "run": 2, "success": False, "note": "plan | timed out"},
    ]
    lines = run_eval.format_summary(rows).splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("| case | run | success | items | fast path | revisions | "
                               "before merge | nodes |")
    assert lines[2] == ("| jp-modular | 1 | yes | 6 | no | 0 | 9 | 7 | 2 | 6 | 0 | 0.81 | 30 "
                        "| 900 / 50 |  |")
    assert lines[3].startswith("| jp-coupled | 2 | no | - | - | - |")
    assert lines[3].endswith("| plan / timed out |")
