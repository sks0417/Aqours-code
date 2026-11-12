"""Validate prepared SWE-EVO tasks with the gold patch, and write their exclusion lists.

    python experiments/taskgraph/sweevo/validate_gold.py [<instance_id> ...] [--pilot]
        [--tasks C:\\tg\\sweevo-tasks] [--work C:\\tg\\sweevo-validation]

No model is called. For each instance, two fake run directories are graded
exactly as ``python -m aqours_code.taskgraph sweevo-eval`` grades a real run
(``sweevo.evaluate_run``: copy ``final/``, put the test files back, apply the
test patch, run the test command in the task image, parse the log):

- ``base``: ``final/`` is the base commit, so the release's tests run on the
  old code: every F2P test should fail and every P2P test pass;
- ``gold``: ``final/`` is the base commit plus the gold patch: every F2P
  and P2P test should pass.

Both are graded without exclusions. A listed test that does not pass with
the gold patch cannot be passed in our sandbox by any method, and an F2P
test that already passes on the base commit gives every method the same
point for nothing; both kinds go to ``instances/<id>/sandbox_skips.txt``
(with the status that put them there) and to ``<task>/sandbox_skips.txt``.
The two runs are then graded again with that list, which must leave the gold
run resolved.

``validation/<id>.json`` keeps the counts, the excluded tests, the time of
each grading step and the image sizes; ``validation/README.md`` is the table
of all validated instances.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # the Aqours checkout, for aqours_code.taskgraph

from aqours_code.taskgraph import gitops  # noqa: E402
from aqours_code.taskgraph.cleanup import remove_tree  # noqa: E402
from aqours_code.taskgraph.sweevo import (  # noqa: E402
    KINDS, PASSING, SKIPS_FILE, evaluate_run, grade, load_task, task_checks)
from prepare import selected_ids  # noqa: E402  (this directory is on sys.path)

DEFAULT_TASKS = Path(r"C:\tg\sweevo-tasks")
DEFAULT_WORK = Path(r"C:\tg\sweevo-validation")
SKIPS_DIR = HERE / "instances"
REPORTS = HERE / "validation"


def fake_run(run_dir: Path, task, patches: list[str]) -> Path:
    """A run directory whose ``final/`` is the base commit with ``patches`` applied."""
    remove_tree(run_dir)
    run_dir.mkdir(parents=True)
    final = run_dir / "final"
    gitops.export_commit(task.repo_dir, "HEAD", final)
    for patch in patches:
        gitops.apply_patch(final, patch)
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": run_dir.name, "status": "validation", "wall_time_s": 0.0}),
        encoding="utf-8")
    return run_dir


def statuses(run_dir: Path) -> dict[str, dict[str, str]]:
    return json.loads((run_dir / "sweevo_results.json").read_text(encoding="utf-8"))["tests"]


def regrade(tests: dict[str, dict[str, str]], task, excluded: list[str]) -> dict:
    """Grade saved statuses again with an exclusion list (no test is run)."""
    status_map = {test_id: status for kind in KINDS for test_id, status in tests[kind].items()
                  if status not in ("MISSING", "EXCLUDED")}
    return grade(status_map, task.fail_to_pass, task.pass_to_pass, excluded)


def image_size(image: str) -> int | None:
    proc = subprocess.run(["docker", "image", "inspect", "--format", "{{.Size}}", image],
                          capture_output=True, text=True)
    return int(proc.stdout.strip()) if proc.returncode == 0 else None


def skips_text(instance_id: str, gold_fail: dict[str, str], base_pass: list[str]) -> str:
    lines = [
        f"# Tests of {instance_id} left out of every method's score.",
        "#",
        "# Made by validate_gold.py, which grades the base commit and the base commit",
        "# plus the gold patch in the task image, with the release's test patch applied,",
        "# exactly as sweevo-eval grades a run. One test id per line (as in the dataset),",
        "# matched exactly; '#' starts a comment.",
        "",
    ]
    lines += [
        "# Listed tests that do not pass with the gold patch in our sandbox, by their",
        "# status with the gold patch (MISSING: not in the test log at all).",
    ]
    if not gold_fail:
        lines.append("# (none)")
    for status in sorted(set(gold_fail.values())):
        lines += [f"# gold: {status}"]
        lines += sorted(test_id for test_id, value in gold_fail.items() if value == status)
    lines += ["", "# FAIL_TO_PASS tests that already pass on the base commit in our sandbox:"]
    lines += sorted(base_pass) or ["# (none)"]
    return "\n".join(lines) + "\n"


def validate(instance_id: str, tasks: Path, work: Path) -> dict:
    task_dir = tasks / instance_id
    task = dataclasses.replace(load_task(task_dir), exclusions=())
    checks = task_checks(task)
    gold_patch = (task_dir / "grading" / "gold.patch").read_text(encoding="utf-8")
    report = {"instance_id": instance_id, "validated_at":
              datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "task_commit": gitops.head(task.repo_dir), "sandbox_image": task.sandbox.image,
              "sandbox_env": task.sandbox.env_dict, "command": task.command,
              "f2p_listed": len(task.fail_to_pass), "p2p_listed": len(task.pass_to_pass)}
    runs = {}
    for name, patches in (("base", []), ("gold", [gold_patch])):
        run_dir = fake_run(work / instance_id / name, task, patches)
        print(f"  {name}: grading ...", flush=True)
        entry = evaluate_run(run_dir, task, checks)
        runs[name] = {"entry": entry, "tests": statuses(run_dir), "dir": run_dir}
        print(f"  {name}: F2P {entry['f2p']['passed']}/{entry['f2p']['total']}  "
              f"P2P {entry['p2p']['passed']}/{entry['p2p']['total']}  "
              f"tests in log {entry['tests_in_log']}  exit {entry['exit_code']}  "
              f"test run {entry['duration_s']:.0f}s  grading {entry['total_duration_s']:.0f}s"
              + ("" if entry["ok"] else f"  ERROR: {entry['error']}"), flush=True)
        if not entry["ok"]:
            raise RuntimeError(f"{instance_id} {name}: the tests gave no result: {entry['error']}")
        if not entry["tests_in_log"]:
            raise RuntimeError(f"{instance_id} {name}: no test ran (exit code "
                               f"{entry['exit_code']}); see {run_dir / 'sweevo_tests.txt'}")
    gold_tests, base_tests = runs["gold"]["tests"], runs["base"]["tests"]
    gold_fail = {test_id: status for kind in KINDS for test_id, status in gold_tests[kind].items()
                 if status not in PASSING and status != "SKIPPED"}
    base_pass = [test_id for test_id, status in base_tests["FAIL_TO_PASS"].items()
                 if status in PASSING and test_id not in gold_fail]
    excluded = sorted(set(gold_fail) | set(base_pass))
    text = skips_text(instance_id, gold_fail, base_pass)
    if instance_id not in selected_ids()[2]:  # a dropped instance keeps only its report
        (SKIPS_DIR / instance_id).mkdir(parents=True, exist_ok=True)
        (SKIPS_DIR / instance_id / SKIPS_FILE).write_text(text, encoding="utf-8", newline="\n")
        (task_dir / SKIPS_FILE).write_text(text, encoding="utf-8", newline="\n")
    after = {name: regrade(run["tests"], task, excluded) for name, run in runs.items()}
    base_after, gold_after = after["base"], after["gold"]
    p2p_base_fail = [test_id for test_id, status in base_tests["PASS_TO_PASS"].items()
                     if status not in PASSING and status != "SKIPPED" and test_id not in excluded]
    instance = json.loads((task_dir / "grading" / "instance.json").read_text(encoding="utf-8"))
    report.update({
        "excluded": len(excluded),
        "excluded_gold_fail": {kind: sorted(t for t in gold_fail if t in set(ids))
                               for kind, ids in (("FAIL_TO_PASS", task.fail_to_pass),
                                                 ("PASS_TO_PASS", task.pass_to_pass))},
        "excluded_gold_status": dict(sorted(gold_fail.items())),
        "excluded_f2p_pass_on_base": sorted(base_pass),
        "base": {"f2p": base_after["f2p"], "p2p": base_after["p2p"],
                 "all_f2p_fail": base_after["f2p"]["passed"] == 0,
                 "p2p_failing": p2p_base_fail,
                 "test_run_s": runs["base"]["entry"]["duration_s"],
                 "grading_s": runs["base"]["entry"]["total_duration_s"]},
        "gold": {"f2p": gold_after["f2p"], "p2p": gold_after["p2p"],
                 "resolved": gold_after["resolved"], "fix_rate": gold_after["fix_rate"],
                 "test_run_s": runs["gold"]["entry"]["duration_s"],
                 "grading_s": runs["gold"]["entry"]["total_duration_s"]},
        "images": {"instance_image": instance["image"],
                   "instance_image_bytes": image_size(instance["image"]),
                   "task_image": task.sandbox.image,
                   "task_image_bytes": image_size(task.sandbox.image)},
    })
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{instance_id}.json").write_text(json.dumps(report, indent=1) + "\n",
                                                 encoding="utf-8", newline="\n")
    return report


def table() -> str:
    """validation/README.md from every validation/<id>.json, in instances.txt order."""
    used, pilots, dropped = selected_ids()
    reasons = {}
    for line in (HERE / "instances.txt").read_text(encoding="utf-8").splitlines():
        words, _, comment = line.partition("#")
        if words.split() and words.split()[0] in dropped:
            reasons[words.split()[0]] = comment.strip()
    reports = {instance_id: json.loads((REPORTS / f"{instance_id}.json").read_text(
        encoding="utf-8")) for instance_id in used + dropped
        if (REPORTS / f"{instance_id}.json").is_file()}
    rows = ["# Gold-patch validation", "",
            "Made by `validate_gold.py`; one JSON file per instance has the details. Counts "
            "are after the excluded tests are left out. Base: the base commit with the "
            "release's tests (F2P should all fail, P2P pass). Gold: the base commit plus "
            "the gold patch (all should pass). Grading: one `sweevo-eval` of a run, from "
            "copying `final/` to parsing the log; test run: the test command alone. "
            "Times were measured with up to four instances graded at once.", "",
            "| Instance | F2P | P2P | Excluded | Base F2P passing | Base P2P failing | "
            "Gold F2P | Gold P2P | Gold resolved | Grading (s) | Test run (s) | "
            "Instance image (GB) |", "|" + " --- |" * 12]
    for instance_id in used:
        if instance_id not in reports:
            continue
        r = reports[instance_id]
        size = r["images"]["instance_image_bytes"]
        rows.append("| " + " | ".join([
            instance_id + (" (pilot)" if instance_id in pilots else ""),
            str(r["f2p_listed"]), str(r["p2p_listed"]), str(r["excluded"]),
            f"{r['base']['f2p']['passed']}/{r['base']['f2p']['total']}",
            str(len(r["base"]["p2p_failing"])),
            f"{r['gold']['f2p']['passed']}/{r['gold']['f2p']['total']}",
            f"{r['gold']['p2p']['passed']}/{r['gold']['p2p']['total']}",
            "yes" if r["gold"]["resolved"] else "no",
            f"{r['gold']['grading_s']:.0f}", f"{r['gold']['test_run_s']:.0f}",
            "-" if size is None else f"{size / 1e9:.2f}"]) + " |")
    if any(instance_id in reports for instance_id in dropped):
        rows += ["", "## Dropped", "",
                 "With the gold patch and no exclusions (these are not used):", ""]
        for instance_id in dropped:
            if instance_id in reports:
                r = reports[instance_id]
                f2p_fail = len(r["excluded_gold_fail"]["FAIL_TO_PASS"])
                p2p_fail = len(r["excluded_gold_fail"]["PASS_TO_PASS"])
                rows.append(f"- `{instance_id}`: F2P {r['f2p_listed'] - f2p_fail}/"
                            f"{r['f2p_listed']} and P2P {r['p2p_listed'] - p2p_fail}/"
                            f"{r['p2p_listed']} pass. {reasons.get(instance_id, '')}")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--keep", action="store_true", help="keep the fake run directories")
    parser.add_argument("--table", action="store_true",
                        help="only rewrite validation/README.md from the JSON files")
    args = parser.parse_args(argv)
    if args.table:
        (REPORTS / "README.md").write_text(table(), encoding="utf-8", newline="\n")
        return 0
    every, pilots, _ = selected_ids()
    ids = args.ids or (pilots if args.pilot else every)
    failed = []
    for instance_id in ids:
        print(instance_id, flush=True)
        try:
            report = validate(instance_id, args.tasks, args.work)
        except Exception as exc:  # noqa: BLE001 - report it and go on with the next one
            print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)
            failed.append(instance_id)
            continue
        finally:
            if not args.keep:
                shutil.rmtree(args.work / instance_id, ignore_errors=True)
        print(f"  excluded {report['excluded']}; base: F2P passing "
              f"{report['base']['f2p']['passed']}, P2P failing {len(report['base']['p2p_failing'])}; "
              f"gold resolved: {report['gold']['resolved']}", flush=True)
    (REPORTS / "README.md").write_text(table(), encoding="utf-8", newline="\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
