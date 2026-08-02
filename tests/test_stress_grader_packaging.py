from pathlib import Path

from evals import run_eval


ROOT = Path(__file__).resolve().parents[1]


def test_trusted_copy_includes_shared_stress_grader(tmp_path):
    case = ROOT / "evals" / "cases" / "stress_timezone_series_scheduling"
    trusted = tmp_path / "trusted"

    run_eval.copy_trusted_case(case, trusted, case.name)

    assert (trusted / "stress_grader.py").is_file()
    assert (trusted / "grader_common.py").is_file()
    assert (trusted / "scoring.py").is_file()
    assert (trusted / "cases" / case.name / "grader.py").is_file()
