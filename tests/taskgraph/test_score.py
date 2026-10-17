"""The ``score`` command, on hand-written run directories."""
from __future__ import annotations

import json
from math import sqrt
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.score import (
    Entry,
    OUTPUT_TOKEN_WEIGHT,
    ScoreError,
    excluded_by,
    format_score,
    junit_key,
    load_exclusions,
    load_manifest,
    score_manifest,
    score_run,
)

FUNCTIONAL = "_hidden_tests.test_feature"
REGRESSION = "_hidden_tests.regression.test_old"
NESTED_REGRESSION = "_hidden_tests.regression.test_utils.test_echo"


def cases(classname: str, passed: int = 0, failed: int = 0, errors: int = 0,
          skipped: int = 0, prefix: str = "test_") -> list[tuple[str, str, str]]:
    """Test cases named ``test_p0.., test_f0.., test_e0.., test_s0..``."""
    return ([(classname, f"{prefix}p{n}", "passed") for n in range(passed)]
            + [(classname, f"{prefix}f{n}", "failed") for n in range(failed)]
            + [(classname, f"{prefix}e{n}", "error") for n in range(errors)]
            + [(classname, f"{prefix}s{n}", "skipped") for n in range(skipped)])


def junit(tests: list[tuple[str, str, str]]) -> str:
    body = {"passed": "", "failed": '<failure message="boom">trace</failure>',
            "error": '<error message="setup">trace</error>',
            "skipped": '<skipped type="pytest.skip" message="why"></skipped>'}
    rows = "".join(f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} "
                   f'time="0.01">{body[outcome]}</testcase>'
                   for classname, name, outcome in tests)
    return (f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
            f'tests="{len(tests)}">{rows}</testsuite></testsuites>')


def make_run(root: Path, name: str, tests: list | None, *, wall: float, tokens: tuple,
             rescored: list | None = None, sandbox: str = "docker", escapes: int = 0) -> Path:
    run = root / "runs" / name
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "run_id": name, "status": "success", "wall_time_s": wall,
        "totals": {"model_calls": 5, "input_tokens": tokens[0], "output_tokens": tokens[1]},
        "sandbox": sandbox, "escape_attempts_total": escapes}), encoding="utf-8")
    if tests is not None:
        (run / "hidden_tests.xml").write_text(junit(tests), encoding="utf-8")
    if rescored is not None:
        (run / "hidden_tests_rescored.xml").write_text(junit(rescored), encoding="utf-8")
    return run


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    """Two tasks, three methods; ``graph`` twice on task A, with a plan report."""
    all_failed = cases(FUNCTIONAL, failed=10) + cases(REGRESSION, failed=5)
    make_run(tmp_path, "a-single", cases(FUNCTIONAL, passed=8, failed=2, skipped=1)
             + cases(REGRESSION, passed=4, errors=1), wall=100, tokens=(1000, 100))
    make_run(tmp_path, "a-graph-1", cases(FUNCTIONAL, passed=10)
             + cases(NESTED_REGRESSION, passed=5, skipped=3), wall=30, tokens=(1500, 200))
    make_run(tmp_path, "a-graph-2", cases(FUNCTIONAL, passed=9, failed=1)
             + cases(REGRESSION, passed=5), wall=80, tokens=(800, 100))
    make_run(tmp_path, "a-team", all_failed, wall=200, tokens=(500, 50),
             rescored=cases(FUNCTIONAL, passed=5, failed=5) + cases(REGRESSION, passed=5))
    make_run(tmp_path, "b-single", cases(FUNCTIONAL, passed=3, failed=1),
             wall=10, tokens=(100, 10))
    make_run(tmp_path, "b-graph", cases(FUNCTIONAL, passed=4), wall=20, tokens=(200, 20))
    make_run(tmp_path, "b-team", cases(FUNCTIONAL, passed=4), wall=1, tokens=(1, 1),
             sandbox="none", escapes=2)
    (tmp_path / "plan.report.json").write_text(json.dumps(
        {"duration_s": 20.0, "totals": {"model_calls": 3, "input_tokens": 200,
                                        "output_tokens": 50}}), encoding="utf-8")
    entries = [
        {"task": "A", "method": "single", "run": "runs/a-single"},
        {"task": "A", "method": "graph", "run": "runs/a-graph-1",
         "plan_report": "plan.report.json"},
        {"task": "A", "method": "graph", "run": "runs/a-graph-2",
         "plan_report": "plan.report.json"},
        {"task": "A", "method": "team", "run": "runs/a-team"},
        {"task": "B", "method": "single", "run": "runs/b-single"},
        {"task": "B", "method": "graph", "run": "runs/b-graph"},
        {"task": "B", "method": "team", "run": "runs/b-team"},
    ]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_quality_time_cost_and_score_follow_the_formula(manifest):
    result = score_manifest(load_manifest(manifest))
    assert OUTPUT_TOKEN_WEIGHT == 4
    a = result["tasks"]["A"]

    single = a["single"]
    assert single["runs"] == 1
    assert single["functional_rate"]["mean"] == pytest.approx(0.8)   # 8 / (8 + 2); skip not counted
    assert single["regression_rate"]["mean"] == pytest.approx(0.8)   # 4 / (4 + 1 error)
    assert single["q"]["mean"] == pytest.approx(0.64)
    assert (single["time_s"]["mean"], single["cost"]["mean"]) == (100, 1000 + 4 * 100)
    assert single["s"] == pytest.approx(0.64)  # the baseline's S is its Q

    graph = a["graph"]  # two runs; the plan report adds 20 s, 200 in, 50 out to each
    assert graph["runs"] == 2 and graph["run_ids"] == ["a-graph-1", "a-graph-2"]
    assert graph["q"] == {"mean": pytest.approx(0.95), "min": pytest.approx(0.9), "max": 1.0}
    assert graph["time_s"] == {"mean": 75.0, "min": 50.0, "max": 100.0}
    assert graph["input_tokens"]["mean"] == (1700 + 1000) / 2
    assert graph["output_tokens"]["mean"] == (250 + 150) / 2
    assert graph["cost"] == {"mean": 2150.0, "min": 1600.0, "max": 2700.0}
    assert graph["s"] == pytest.approx(0.95 * sqrt(100 / 75) * sqrt(1400 / 2150))
    assert graph["s_runs"]["max"] == pytest.approx(1.0 * sqrt(100 / 50) * sqrt(1400 / 2700))
    assert graph["s_runs"]["min"] == pytest.approx(0.9 * sqrt(100 / 100) * sqrt(1400 / 1600))

    team = a["team"]  # hidden_tests_rescored.xml wins over hidden_tests.xml
    assert team["functional_rate"]["mean"] == 0.5 and team["regression_rate"]["mean"] == 1.0
    assert team["s"] == pytest.approx(0.5 * sqrt(100 / 200) * sqrt(1400 / 700))
    run = next(run for run in result["runs"] if run["run_id"] == "a-team")
    assert run["results_file"] == "hidden_tests_rescored.xml"

    b = result["tasks"]["B"]
    assert b["single"]["regression_rate"]["mean"] == 1.0  # no regression tests: rate 1
    assert b["single"]["s"] == pytest.approx(0.75)
    assert b["graph"]["s"] == pytest.approx(1.0 * sqrt(10 / 20) * sqrt(140 / 280))


def test_summary_best_method_oracle_and_invalid_runs(manifest):
    result = score_manifest(load_manifest(manifest))
    graph_a = 0.95 * sqrt(100 / 75) * sqrt(1400 / 2150)
    assert result["best"] == {"A": "graph", "B": "single"}
    assert result["oracle"]["mean_s"] == pytest.approx((graph_a + 0.75) / 2)
    methods = result["methods"]
    assert list(methods) == ["single", "graph", "team"]
    assert methods["single"] == {"tasks": 2, "mean_q": pytest.approx((0.64 + 0.75) / 2),
                                 "mean_s": pytest.approx((0.64 + 0.75) / 2), "best_on": ["B"]}
    assert methods["graph"]["mean_q"] == pytest.approx((0.95 + 1.0) / 2)
    assert methods["graph"]["mean_s"] == pytest.approx((graph_a + 0.5) / 2)
    assert methods["graph"]["best_on"] == ["A"]
    assert methods["team"]["tasks"] == 1 and methods["team"]["best_on"] == []
    # --sandbox none with escape attempts: listed, not counted
    assert "team" not in result["tasks"]["B"]
    assert [(run["task"], run["method"], run["invalid_reason"])
            for run in result["invalid_runs"]] == [
        ("B", "team", "--sandbox none with 2 escape attempt(s)")]


def test_excluded_tests_leave_numerator_and_denominator(manifest, tmp_path):
    exclude = tmp_path / "known_bad.txt"
    exclude.write_text("# These tests disagree with the specification.\n"
                       "test_feature.py::test_f0\n\n"
                       "_hidden_tests/test_feature.py::test_p0\n"
                       "test_old.py::test_e0\n"
                       "test_feature.py::test_missing\n", encoding="utf-8")
    exclusions = load_exclusions(exclude)
    assert exclusions == ["test_feature.py::test_f0", "_hidden_tests/test_feature.py::test_p0",
                          "test_old.py::test_e0", "test_feature.py::test_missing"]
    result = score_manifest(load_manifest(manifest), exclusions)
    single = result["tasks"]["A"]["single"]
    assert single["functional_rate"]["mean"] == pytest.approx(7 / 8)  # (8 - 1) / (10 - 2)
    assert single["regression_rate"]["mean"] == 1.0                   # the error is left out
    assert result["tasks"]["A"]["graph"]["functional_rate"]["mean"] == pytest.approx(
        (1.0 + 1.0) / 2)  # run 2's only failure was test_f0
    run = next(run for run in result["runs"] if run["run_id"] == "a-single")
    assert run["functional"] == {"passed": 7, "failed": 1, "errors": 0, "skipped": 1,
                                 "excluded": 2, "rate": pytest.approx(7 / 8)}
    assert result["excluded_tests"]["test_feature.py::test_missing"] == 0
    assert result["excluded_tests"]["test_feature.py::test_f0"] == 4  # in four valid runs


def test_node_ids_match_junit_names_whatever_the_path_prefix():
    assert junit_key("test_priority.py::test_tenants") == ("test_priority", "test_tenants")
    assert junit_key("a/b/test_x.py::TestC::test_y[p::q- 1]") == (
        "a.b.test_x.TestC", "test_y[p::q- 1]")
    keys = [junit_key("test_priority.py::test_tenants"),
            junit_key("regression/test_x.py::TestC::test_y[1]")]
    assert excluded_by("_hidden_tests.test_priority", "test_tenants", keys) == 0
    assert excluded_by("test_priority", "test_tenants", keys) == 0
    assert excluded_by("_hidden_tests.regression.test_x.TestC", "test_y[1]", keys) == 1
    assert excluded_by("_hidden_tests.other_test_priority", "test_tenants", keys) is None
    # an entry may leave out leading directories, not name other ones
    assert excluded_by("_hidden_tests.test_priority", "test_tenants",
                       [junit_key("elsewhere/test_priority.py::test_tenants")]) is None
    assert excluded_by("_hidden_tests.test_priority", "test_tenants_more", keys) is None
    assert excluded_by("_hidden_tests.test_x.TestC", "test_y[2]", keys) is None


def test_a_task_without_a_single_run_is_an_error(manifest, tmp_path, capsys):
    entries = [entry for entry in json.loads(manifest.read_text(encoding="utf-8"))
               if (entry["task"], entry["method"]) != ("B", "single")]
    manifest.write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(ScoreError, match='task B has no valid "single" run'):
        score_manifest(load_manifest(manifest))
    assert main(["score", str(manifest)]) == 2
    assert 'task B has no valid "single" run' in capsys.readouterr().err


def test_runs_without_results_are_invalid(tmp_path):
    make_run(tmp_path, "single", cases(FUNCTIONAL, passed=1), wall=10, tokens=(10, 1))
    make_run(tmp_path, "crashed", None, wall=10, tokens=(10, 1))
    broken = make_run(tmp_path, "broken", None, wall=10, tokens=(10, 1))
    (broken / "hidden_tests.xml").write_text("<testsuites><testsuite", encoding="utf-8")
    make_run(tmp_path, "escapes-in-docker", cases(FUNCTIONAL, passed=1), wall=10,
             tokens=(10, 1), sandbox="docker", escapes=3)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([
        {"task": "T", "method": "single", "run": "runs/single"},
        {"task": "T", "method": "x", "run": "runs/crashed"},
        {"task": "T", "method": "x", "run": "runs/broken"},
        {"task": "T", "method": "y", "run": str(tmp_path / "runs" / "escapes-in-docker")},
    ]), encoding="utf-8")
    result = score_manifest(load_manifest(path))
    assert list(result["tasks"]["T"]) == ["single", "y"]  # escapes inside Docker still count
    assert [run["invalid_reason"] for run in result["invalid_runs"]] == [
        "no hidden test results (hidden_tests.xml)",
        result["invalid_runs"][1]["invalid_reason"]]
    assert "hidden_tests.xml cannot be read" in result["invalid_runs"][1]["invalid_reason"]


@pytest.mark.parametrize("content", [
    "{}", "[]", '[{"task": "A", "method": "single"}]', '[{"task": "A", "method": "", "run": "r"}]',
    '["a"]', "not json",
])
def test_bad_manifests_are_input_errors(tmp_path, content, capsys):
    path = tmp_path / "manifest.json"
    path.write_text(content, encoding="utf-8")
    assert main(["score", str(path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_missing_files_are_input_errors(manifest, tmp_path, capsys):
    assert main(["score", str(tmp_path / "missing.json")]) == 2
    assert main(["score", str(manifest), "--exclude", str(tmp_path / "missing.txt")]) == 2
    (tmp_path / "plan.report.json").unlink()
    assert main(["score", str(manifest)]) == 2
    assert "cannot read plan report" in capsys.readouterr().err


def test_score_command_prints_and_writes_the_three_parts(manifest, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["score", str(manifest), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    text = (out / "score.md").read_text(encoding="utf-8")
    assert printed.startswith(text)
    assert "## By task" in text and "## By method" in text and "## Oracle" in text
    rows = {tuple(cell.strip() for cell in line.strip("|").split("|")[:2]): line
            for line in text.splitlines() if line.startswith("| ")}
    assert rows[("A", "single")] == ("| A | single | 1 | 0.800 | 0.800 | 0.640 | 100 | 1,000 | "
                                     "100 | 1,400 | 0.640 |")
    assert rows[("A", "graph")] == (
        "| A | graph | 2 | 0.950 (0.900–1.000) | 1.000 (1.000–1.000) | 0.950 (0.900–1.000) | "
        "75 (50–100) | 1,350 (1,000–1,700) | 200 (150–250) | 2,150 (1,600–2,700) | "
        "0.885 (0.842–1.018) |")
    assert ("B", "team") not in rows
    assert "| graph | 2 | 0.975 | 0.693 | A |" in text
    assert "| single | 2 | 0.695 | 0.695 | B |" in text
    assert "(A: graph, B: single): mean S = 0.818." in text
    assert "## Invalid runs (not counted)" in text
    assert "B / team" in text and "--sandbox none with 2 escape attempt(s)" in text
    saved = json.loads((out / "score.json").read_text(encoding="utf-8"))
    assert saved["best"] == {"A": "graph", "B": "single"}
    assert saved["oracle"]["mean_s"] == pytest.approx(0.8176, abs=1e-3)
    assert len(saved["runs"]) == 7


# ── task 25e: T leaves out the hidden tests and the escape audit ──

def write_events(run: Path, events: list[dict]) -> None:
    (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events),
                                      encoding="utf-8")


# The click team run 20261015T224558Z-9398: done at 438 s, hidden tests stuck 1787 s.
STUCK_EVENTS = [
    {"t": 427.5, "type": "node_completed", "node": "lead_wrapup"},
    {"t": 437.828, "type": "final_checks_end", "ok": True},
    {"t": 2265.156, "type": "hidden_tests_end", "passed": 3, "timed_out": True,
     "duration_s": 1787.266},
    {"t": 2265.375, "type": "escape_audit_end", "total": 0},  # no duration: the gap
    {"t": 2265.453, "type": "run_end", "status": "success", "wall_time_s": 2265.375},
]


def test_time_leaves_out_the_hidden_tests_and_the_escape_audit(tmp_path):
    run = make_run(tmp_path, "team", cases(FUNCTIONAL, passed=3), wall=2265.375,
                   tokens=(1000, 100))
    write_events(run, STUCK_EVENTS)
    scored = score_run(Entry("click", "team", run), [])
    assert scored["wall_time_s"] == 2265.375
    assert scored["eval_time"] == {"hidden_tests_end": 1787.266,
                                   "escape_audit_end": pytest.approx(0.219)}
    assert scored["eval_time_s"] == pytest.approx(1787.485)
    assert scored["time_s"] == pytest.approx(2265.375 - 1787.485)
    planned = score_run(Entry("click", "graph", run, plan_report=None), [])
    assert planned["time_s"] == scored["time_s"]


def test_planning_time_is_still_added(tmp_path):
    run = make_run(tmp_path, "graph", cases(FUNCTIONAL, passed=3), wall=600,
                   tokens=(1000, 100))
    write_events(run, [{"t": 500.0, "type": "final_checks_end"},
                       {"t": 590.0, "type": "hidden_tests_end", "duration_s": 80.0},
                       {"t": 600.0, "type": "escape_audit_end", "total": 0}])
    report = tmp_path / "plan.json"
    report.write_text(json.dumps({"duration_s": 50.0, "totals": {}}), encoding="utf-8")
    scored = score_run(Entry("click", "graph", run, plan_report=report), [])
    assert scored["eval_time_s"] == 90.0 and scored["time_s"] == 600 - 90 + 50


def test_a_run_without_evaluation_events_keeps_its_wall_time(tmp_path):
    bare = make_run(tmp_path, "bare", cases(FUNCTIONAL, passed=3), wall=300, tokens=(10, 1))
    other = make_run(tmp_path, "other", cases(FUNCTIONAL, passed=3), wall=300, tokens=(10, 1))
    write_events(other, [{"t": 1.0, "type": "run_start"}, "not an object",
                         {"t": 299.0, "type": "final_checks_end"}])
    (other / "events.jsonl").write_text((other / "events.jsonl").read_text(encoding="utf-8")
                                        + "{broken\n", encoding="utf-8")
    for run in (bare, other):
        scored = score_run(Entry("click", "single", run), [])
        assert (scored["wall_time_s"], scored["eval_time_s"], scored["time_s"]) == (300, 0, 300)
        assert scored["eval_time"] == {}


def test_the_evaluation_time_never_exceeds_the_wall_time(tmp_path):
    run = make_run(tmp_path, "odd", cases(FUNCTIONAL, passed=3), wall=100, tokens=(10, 1))
    write_events(run, [{"t": 100.0, "type": "hidden_tests_end", "duration_s": 500.0}])
    scored = score_run(Entry("click", "single", run), [])
    assert scored["eval_time_s"] == 100.0 and not scored["valid"]  # T = 0 cannot count


def fake_rescorable_run(tmp_path: Path) -> tuple[Path, Path]:
    """A finished run whose hidden tests failed, and corrected hidden tests that pass."""
    run = make_run(tmp_path, "rescored", cases(FUNCTIONAL, failed=2), wall=500,
                   tokens=(1000, 100))
    write_events(run, [{"t": 400.0, "type": "final_checks_end"},
                       {"t": 499.0, "type": "hidden_tests_end", "duration_s": 95.0},
                       {"t": 500.0, "type": "escape_audit_end", "total": 0}])
    (run / "final").mkdir()
    (run / "final" / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                           encoding="utf-8")
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_calc.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_add_more():\n    assert add(2, 2) == 4\n", encoding="utf-8")
    return run, hidden


def test_score_uses_the_results_of_the_latest_rescore(tmp_path):
    from aqours_code.taskgraph.checks import HostChecks, rescore
    run, hidden = fake_rescorable_run(tmp_path)
    before = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert score_run(Entry("A", "single", run), [])["q"] == 0.0
    stats = rescore(run, hidden, HostChecks())
    assert stats["passed"] == 2
    after = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert {key: value for key, value in after.items() if key != "hidden_tests_rescored"} \
        == before  # rescore changes no other field of summary.json
    scored = score_run(Entry("A", "single", run), [])
    assert scored["results_file"] == "hidden_tests_rescored.xml"
    assert scored["functional"]["passed"] == 2 and scored["q"] == 1.0
    assert scored["time_s"] == 500 - 96  # the rescore adds no time to the run


def test_a_rescore_that_left_no_results_does_not_fall_back_to_older_ones(tmp_path):
    run, _ = fake_rescorable_run(tmp_path)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    summary["hidden_tests_rescored"] = {"passed": 0, "timed_out": True,
                                        "parse_error": "no report"}
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    scored = score_run(Entry("A", "single", run), [])
    assert not scored["valid"]
    assert "the last rescore left no hidden_tests_rescored.xml" in scored["invalid_reason"]


def test_score_md_states_what_t_is_and_lists_every_run(manifest, tmp_path):
    run = tmp_path / "runs" / "a-single"
    write_events(run, [{"t": 60.0, "type": "final_checks_end"},
                       {"t": 99.0, "type": "hidden_tests_end", "duration_s": 39.0},
                       {"t": 100.0, "type": "escape_audit_end", "total": 0}])
    result = score_manifest(load_manifest(manifest))
    text = format_score(result)
    assert ("T = wall time of the method (planning included, hidden tests and escape audit "
            "excluded)") in text
    assert result["time"] == ("wall time of the method (planning included, hidden tests and "
                              "escape audit excluded)")
    assert "| a-single | A | single | 100 | 40 | 0 | 60 | 0.640 | 0.640 |" in text
    assert "| b-team | B | team | 1 | 0 | 0 | 1 | invalid | - |" in text
    assert "| a-graph-1 | A | graph | 30 | 0 | 20 | 50 |" in text
    text.encode("cp1252")  # score prints it; a Windows console may not take UTF-8
