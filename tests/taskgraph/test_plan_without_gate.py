"""``plan`` without ``--gate`` behaves exactly as before ``--gate`` existed.

``plan_without_gate.json`` was written by this test from the code before
``plan --gate`` (commit fc2d146): the prompts, the terminal output, the
report, the graph and the files of a full plan and of a fast-path plan. With
``PLAN_SNAPSHOT_WRITE=<file>`` the test writes the snapshots there instead of
comparing (to make the file from another checkout).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plan_snapshot import CASES, GOLDEN, plan_snapshot


@pytest.mark.parametrize("case", sorted(CASES))
def test_plan_without_gate_is_unchanged(case, toy_repo, tmp_path, monkeypatch, capsys):
    snapshot = plan_snapshot(toy_repo.path, tmp_path, monkeypatch, capsys, CASES[case])
    target = os.environ.get("PLAN_SNAPSHOT_WRITE")
    if target:
        path = Path(target)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[case] = snapshot
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        return
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[case]
    assert snapshot == golden
