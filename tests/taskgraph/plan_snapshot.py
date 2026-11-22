"""A normalised snapshot of one ``plan`` command with scripted models.

Everything a run shows or sends: the exit code, the terminal output, every
prompt and request field the workers got, the report, the graph and the
files written next to it. Paths under the test's temporary directory become
``<tmp>`` and durations become 0, so two runs of the same code give the same
snapshot. ``plan_without_gate.json`` holds the snapshots of the code before
``plan --gate`` existed (``test_plan_without_gate.py`` compares the current
code with it). This module uses only what existed before ``--gate``, so the
same test can write that file from the older code (``PLAN_SNAPSHOT_WRITE``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from aqours_code.taskgraph.cli import main
from test_planner import REQUEST, ScriptedPlanner, answer, items_answer, patch_workers
from test_planner_v3 import FINAL, TWO_UNITS

GOLDEN = Path(__file__).resolve().parent / "plan_without_gate.json"
# name -> step-1 estimate of changed lines: the full plan, and the fast path
CASES = {"plan": 800, "fast_path": 100}
_VOLATILE = {"duration_s", "seconds"}


def _replace_tmp(text: str, tmp: str) -> str:
    """Temporary paths as ``<tmp>/...`` with forward slashes; other text untouched."""
    for form in (tmp, tmp.replace("\\", "/"), tmp.replace("\\", "\\\\")):
        text = text.replace(form, "<tmp>")
    return re.sub(r"<tmp>[\\/\w.\-]*", lambda match: match.group().replace("\\", "/"), text)


def normalise(value, tmp: str):
    """``value`` with temporary paths replaced and durations set to 0."""
    if isinstance(value, dict):
        return {key: 0 if key in _VOLATILE else normalise(item, tmp)
                for key, item in value.items()}
    if isinstance(value, list):
        return [normalise(item, tmp) for item in value]
    if isinstance(value, str):
        return _replace_tmp(value, tmp)
    return value


def output_lines(text: str, tmp: str) -> list[str]:
    """The plan command's own lines; the scripted runtime's hook lines are left out."""
    lines = []
    for line in text.splitlines():
        if "\x1b[" in line or "[HOOK]" in line:
            continue
        line = _replace_tmp(line, tmp)
        line = re.sub(r"time: [\d.]+s", "time: <t>s", line)
        line = re.sub(r", \d+s, ", ", <t>s, ", line)
        lines.append(line)
    return lines


def _requests(worker) -> list[dict]:
    return [{"node_id": request.node_id, "attempt": request.attempt, "prompt": request.prompt,
             "timeout_s": request.timeout_s, "model_budget": request.model_budget,
             "repo_guard": request.repo_guard, "log_dir": str(request.log_dir)}
            for request in worker.requests]


def plan_snapshot(repo: Path, work: Path, monkeypatch, capsys, estimate: int,
                  flags: tuple[str, ...] = ()) -> dict:
    """Run ``plan`` on ``repo`` with scripted answers under ``work``; return its snapshot."""
    request = work / "request.md"
    request.write_text(REQUEST, encoding="utf-8")
    out = work / "out" / "graph.json"
    worker = ScriptedPlanner([answer(TWO_UNITS)])
    drafter = patch_workers(monkeypatch, worker, [items_answer(["S", "R"], estimate)])
    capsys.readouterr()
    code = main(["plan", str(request), "--repo", str(repo), "--out", str(out),
                 "--final-check", FINAL, *flags])
    report = json.loads((out.parent / "graph.json.report.json").read_text(encoding="utf-8"))
    graph = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    tmp = str(work)
    return normalise({
        "exit_code": code,
        "output": output_lines(capsys.readouterr().out, tmp),
        "step1_requests": _requests(drafter),
        "step2_requests": _requests(worker),
        "report": report,
        "graph": graph,
        "files": sorted(path.name for path in out.parent.iterdir()),
    }, tmp)
