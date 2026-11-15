"""Command-line entry point: ``python -m aqours_code.taskgraph <command>``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .derive import derive_edges
from .plan_budget import (DEFAULT_INVESTIGATOR_CALLS, DEFAULT_INVESTIGATOR_INPUT_TOKENS,
                          DEFAULT_INVESTIGATOR_S, DEFAULT_MAX_INPUT_TOKENS,
                          DEFAULT_MAX_MODEL_CALLS, DEFAULT_MAX_PLAN_S)
from .planner import DEFAULT_MAX_INVESTIGATORS, FACTS_MODES
from .repo_index import RepoIndex, build_index
from .schema import DEFAULT_SCHEMA_PATH, Graph, dump_graph, export_json_schema, load_graph
from .validate import validate

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_INPUT_ERROR = 2


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_INPUT_ERROR


def _load(path: str) -> Graph | None:
    try:
        return load_graph(path)
    except FileNotFoundError:
        _error(f"graph file not found: {path}")
    except OSError as exc:
        _error(f"cannot read graph file {path}: {exc.strerror or exc}")
    except UnicodeDecodeError:
        _error(f"graph file {path} is not valid UTF-8")
    except ValidationError as exc:
        print(f"error: {path} does not match the task graph schema:\n{exc}",
              file=sys.stderr)
    return None


def _index(repo: str, commit: str) -> RepoIndex | None:
    try:
        index = build_index(Path(repo), commit)
    except RuntimeError as exc:
        _error(f"cannot index {repo} at {commit}: {exc}")
        return None
    for warning in index.warnings:
        print(f"index warning: {warning}", file=sys.stderr)
    return index


def _cmd_validate(args: argparse.Namespace) -> int:
    graph = _load(args.graph)
    if graph is None:
        return EXIT_INPUT_ERROR
    index = None
    if args.repo:
        index = _index(args.repo, graph.base_commit)
        if index is None:
            return EXIT_INPUT_ERROR
    report = validate(graph, index)
    print(report.format())
    return EXIT_OK if report.ok else EXIT_INVALID


def _cmd_derive(args: argparse.Namespace) -> int:
    graph = _load(args.graph)
    if graph is None:
        return EXIT_INPUT_ERROR
    index = _index(args.repo, graph.base_commit)
    if index is None:
        return EXIT_INPUT_ERROR
    derived, entries = derive_edges(graph, index)
    try:
        dump_graph(derived, args.out)
    except OSError as exc:
        return _error(f"cannot write {args.out}: {exc.strerror or exc}")
    print(f"revisions ({len(entries)}):")
    for entry in entries:
        print(f"[{entry.action}] {entry.reason}")
    report = validate(derived, index)
    print(report.format())
    print(f"wrote {args.out}")
    return EXIT_OK if report.ok else EXIT_INVALID


def _cmd_index(args: argparse.Namespace) -> int:
    index = _index(args.repo, args.commit)
    if index is None:
        return EXIT_INPUT_ERROR
    for symbol in sorted(index.symbols):
        print(symbol)
    return EXIT_OK


def _cmd_export_schema(args: argparse.Namespace) -> int:
    try:
        target = export_json_schema(args.out)
    except OSError as exc:
        return _error(f"cannot write {args.out}: {exc.strerror or exc}")
    print(f"wrote {target}")
    return EXIT_OK


NO_SANDBOX_WARNING = (
    "WARNING: --sandbox none: worker shell commands and all checks run on this machine "
    "and can read any file on it. Use this only for tests; such a run is not a valid "
    "experiment.")


def _banner(lines: list[str]) -> str:
    width = max(len(line) for line in lines)
    rule = "!" * (width + 4)
    return "\n".join([rule, *(f"! {line.ljust(width)} !" for line in lines), rule])


def _sandbox(args: argparse.Namespace):
    """The sandbox of ``--sandbox``, ``--sandbox-image`` and ``--sandbox-config``.

    Returns a ``SandboxConfig``, or an error message.
    """
    from .sandbox import DEFAULT_IMAGE, SandboxConfig, load_sandbox_config

    if args.sandbox_config:
        if args.sandbox != "docker":
            return "--sandbox-config needs --sandbox docker"
        if args.sandbox_image:
            return "give the image in --sandbox-config or --sandbox-image, not both"
        try:
            return load_sandbox_config(Path(args.sandbox_config))
        except (OSError, ValueError) as exc:
            return f"cannot read sandbox config {args.sandbox_config}: {exc}"
    return SandboxConfig(kind=args.sandbox, image=args.sandbox_image or DEFAULT_IMAGE)


def _cmd_run(args: argparse.Namespace) -> int:
    from .coordinator import GraphInvalid, RunOptions, run_graph
    from .gitops import GitError
    from .sandbox import SandboxUnavailable
    from .workers import AqoursWorker, CommandWorker

    graph = _load(args.graph)
    if graph is None:
        return EXIT_INPUT_ERROR
    if args.workers < 1 or args.max_attempts < 1:
        return _error("--workers and --max-attempts must be >= 1")
    if args.merge_repairs < 0:
        return _error("--merge-repairs must be >= 0")
    if args.conflict_repairs < 0:
        return _error("--conflict-repairs must be >= 0")
    if args.worker == "command":
        if not args.command_map:
            return _error("--worker command needs --command-map")
        try:
            commands = json.loads(Path(args.command_map).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _error(f"cannot read command map {args.command_map}: {exc}")
        worker = CommandWorker(commands)  # runs on the host whatever --sandbox says
    else:
        sandbox = _sandbox(args)
        if isinstance(sandbox, str):
            return _error(sandbox)
        worker = AqoursWorker(sandbox=sandbox)
    if worker.sandbox.kind == "none":
        print(NO_SANDBOX_WARNING, file=sys.stderr)
    hidden = Path(args.hidden_tests) if args.hidden_tests else None
    if hidden is not None and not hidden.is_dir():
        return _error(f"hidden tests directory not found: {hidden}")
    options = RunOptions(out_dir=Path(args.out), workers=args.workers,
                         max_attempts=args.max_attempts,
                         worker_timeout_s=args.worker_timeout, hidden_tests=hidden,
                         merge_repairs=args.merge_repairs,
                         conflict_repairs=args.conflict_repairs)
    try:
        result = run_graph(graph, Path(args.repo), worker, options)
    except GraphInvalid as exc:
        print(exc.report.format())
        return EXIT_INVALID
    except SandboxUnavailable as exc:
        return _error(f"{exc} (the run did not start; it never falls back to "
                      "--sandbox none)")
    except (GitError, RuntimeError, OSError) as exc:
        return _error(str(exc))
    summary = result.summary
    for node_id, node in summary["nodes"].items():
        reason = f" ({node['reason']})" if node["reason"] else ""
        print(f"{node_id}: {node['status']}{reason} attempts={node['attempts']}")
    totals = summary["totals"]
    print(f"status: {summary['status']}  wall_time: {summary['wall_time_s']:.1f}s  "
          f"model_calls: {totals['model_calls']}  tokens: "
          f"{totals['input_tokens']} in / {totals['output_tokens']} out")
    print(f"context_chars: {totals['context_chars']}  "
          f"reads_outside_pack: {totals['reads_outside_pack']}  "
          f"calls_before_first_write: {totals['calls_before_first_write']}")
    print(f"soft_wall_blocked: {totals['soft_wall_blocked']}  "
          f"confirmed_reads: {totals['confirmed_reads']}")
    if summary["hidden_tests"] is not None:
        hidden_stats = summary["hidden_tests"]
        print(f"hidden tests: {hidden_stats['passed']} passed, {hidden_stats['failed']} "
              f"failed, {hidden_stats['errors']} errors, {hidden_stats['skipped']} skipped")
    repaired = [node_id for node_id, node in summary["nodes"].items()
                if node["merge_repairs"]]
    fixed = [node_id for node_id in repaired if summary["nodes"][node_id]["merge_repair_ok"]]
    detail = (f" ({', '.join(repaired)}; repaired: {', '.join(fixed) or 'none'})"
              if repaired else "")
    print(f"merge repairs: {summary['merge_repairs_total']}{detail}")
    conflicted = [node_id for node_id, node in summary["nodes"].items()
                  if node["merge_conflicts"]]
    resolved = [node_id for node_id in conflicted
                if summary["nodes"][node_id]["conflict_resolved"]]
    detail = (f" ({', '.join(conflicted)}; resolved: {', '.join(resolved) or 'none'})"
              if conflicted else "")
    print(f"merge conflicts: {summary['merge_conflicts_total']}{detail}")
    print(f"check timeout retries: {summary['check_timeout_retries_total']}")
    escapes = summary["escape_attempts_total"]
    print(f"sandbox: {summary['sandbox']}  escape attempts: {escapes}")
    print(f"checks ran in: {_where(summary['checks_sandbox'])}")
    if escapes and summary["sandbox"] == "none":
        print(_banner([f"{escapes} escape attempt(s) without a sandbox: workers may have "
                       "read files outside their workspace.",
                       "THE RESULTS OF THIS RUN ARE INVALID. See escape_samples in "
                       "summary.json."]))
    for warning in result.cleanup_warnings:
        print(f"warning: cleanup: {warning}", file=sys.stderr)
    print(f"run directory: {result.run_dir}")
    return EXIT_OK if summary["status"] == "success" else EXIT_INVALID


def _cmd_team(args: argparse.Namespace) -> int:
    from . import planner
    from .gitops import GitError
    from .sandbox import SandboxUnavailable
    from .team import TeamOptions, run_team
    from .workers import AqoursWorker

    request_path = Path(args.request)
    if not request_path.is_file():
        return _error(f"request file not found: {request_path}")
    if not Path(args.repo).is_dir():
        return _error(f"repository not found: {args.repo}")
    if args.workers < 1 or args.max_attempts < 1:
        return _error("--workers and --max-attempts must be >= 1")
    if args.worker_timeout <= 0 or args.lead_timeout <= 0:
        return _error("--worker-timeout and --lead-timeout must be > 0")
    hidden = Path(args.hidden_tests) if args.hidden_tests else None
    if hidden is not None and not hidden.is_dir():
        return _error(f"hidden tests directory not found: {hidden}")
    sandbox = _sandbox(args)
    if isinstance(sandbox, str):
        return _error(sandbox)
    worker = AqoursWorker(sandbox=sandbox)
    if worker.sandbox.kind == "none":
        print(NO_SANDBOX_WARNING, file=sys.stderr)
    options = TeamOptions(request_path=request_path, repo=Path(args.repo),
                          out_dir=Path(args.out), workers=args.workers,
                          max_attempts=args.max_attempts,
                          worker_timeout_s=args.worker_timeout,
                          lead_timeout_s=args.lead_timeout,
                          final_checks=list(args.final_check), hidden_tests=hidden)
    try:
        result = run_team(options, worker, planner.planner_worker())
    except SandboxUnavailable as exc:
        return _error(f"{exc} (the run did not start; it never falls back to "
                      "--sandbox none)")
    except (GitError, RuntimeError, OSError, UnicodeDecodeError) as exc:
        return _error(str(exc))
    summary = result.summary
    team = summary["team"]
    if summary["reason"] == "lead_plan_invalid":
        print("the lead gave no usable task list:")
        for problem in team["plan_problems"]:
            print(f"- {problem}")
    print(f"tasks: {team['n_tasks']}  max parallel: {team['max_parallel']}")
    for task in team["tasks"]:
        reason = f" ({task['reason']})" if task["reason"] else ""
        attempts = summary["nodes"][task["id"]]["attempts"]
        print(f"{task['id']}: {task['status']}{reason} attempts={attempts}  {task['title']}")
    print(f"conflicts: {team['conflicts']} (resolved: {team['conflicts_resolved']})  "
          f"out-of-scope files: {team['out_of_scope_total']}  "
          f"lead wrap-up changed: {len(team['wrapup_changed_files'])} file(s)")
    print(f"check timeout retries: {summary['check_timeout_retries_total']}")
    totals = summary["totals"]
    print(f"status: {summary['status']}  wall_time: {summary['wall_time_s']:.1f}s  "
          f"model_calls: {totals['model_calls']}  tokens: "
          f"{totals['input_tokens']} in / {totals['output_tokens']} out")
    for check in summary["final_checks"]:
        print(f"final check: {'ok' if check['ok'] else 'FAILED'}  {check['command']}")
    if summary["hidden_tests"] is not None:
        hidden_stats = summary["hidden_tests"]
        print(f"hidden tests: {hidden_stats['passed']} passed, {hidden_stats['failed']} "
              f"failed, {hidden_stats['errors']} errors, {hidden_stats['skipped']} skipped")
    escapes = summary["escape_attempts_total"]
    print(f"sandbox: {summary['sandbox']}  escape attempts: {escapes}")
    print(f"checks ran in: {_where(summary['checks_sandbox'])}")
    if escapes and summary["sandbox"] == "none":
        print(_banner([f"{escapes} escape attempt(s) without a sandbox: workers may have "
                       "read files outside their workspace.",
                       "THE RESULTS OF THIS RUN ARE INVALID. See escape_samples in "
                       "summary.json."]))
    for warning in result.cleanup_warnings:
        print(f"warning: cleanup: {warning}", file=sys.stderr)
    print(f"run directory: {result.run_dir}")
    return EXIT_OK if summary["status"] == "success" else EXIT_INVALID


def _cmd_score(args: argparse.Namespace) -> int:
    from .score import ScoreError, format_score, load_exclusions, load_manifest
    from .score import score_manifest, write_score

    try:
        entries = load_manifest(Path(args.manifest))
        exclusions = load_exclusions(Path(args.exclude)) if args.exclude else []
        result = score_manifest(entries, exclusions)
        if args.out:
            write_score(result, Path(args.out))
    except (ScoreError, OSError) as exc:
        return _error(str(exc))
    print(format_score(result), end="")
    if args.out:
        print(f"\nwrote {Path(args.out) / 'score.md'} and {Path(args.out) / 'score.json'}")
    return EXIT_OK


def _where(checks_sandbox: str) -> str:
    return "docker" if checks_sandbox == "docker" else "the host (no sandbox)"


def _cmd_rescore(args: argparse.Namespace) -> int:
    from .checks import checks_for, rescore
    from .sandbox import DEFAULT_IMAGE, SandboxConfig, SandboxUnavailable, check_docker
    from .sandbox import load_sandbox_config

    run_dir, tests_dir = Path(args.run_dir), Path(args.hidden_tests)
    if not run_dir.is_dir():
        return _error(f"run directory not found: {run_dir}")
    if not tests_dir.is_dir():
        return _error(f"hidden tests directory not found: {tests_dir}")
    image, env, nofile, pids, git_view = args.sandbox_image, {}, None, None, False
    if args.sandbox_config:
        if args.sandbox_image or args.sandbox != "docker":
            return _error("--sandbox-config needs --sandbox docker and no --sandbox-image")
        try:
            configured = load_sandbox_config(Path(args.sandbox_config))
        except (OSError, ValueError) as exc:
            return _error(f"cannot read sandbox config {args.sandbox_config}: {exc}")
        image, env, nofile = configured.image, configured.env_dict, configured.nofile
        pids, git_view = configured.pids, configured.git_view
    elif image is None:  # the image and options the run itself used, if any
        try:
            recorded = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            image = recorded.get("sandbox_image")
            env = recorded.get("sandbox_env") or {}
            nofile = recorded.get("sandbox_nofile")
            pids = recorded.get("sandbox_pids")
            git_view = recorded.get("sandbox_git_view") is True
        except (OSError, ValueError, AttributeError):
            image, env, nofile, pids, git_view = None, {}, None, None, False
        if not isinstance(env, dict):
            env = {}
        if not isinstance(nofile, int):
            nofile = None
        if not isinstance(pids, int):
            pids = None
    sandbox = SandboxConfig(kind=args.sandbox, image=image or DEFAULT_IMAGE,
                            env=tuple(env.items()), nofile=nofile, pids=pids,
                            git_view=git_view)
    if sandbox.kind == "docker":
        try:
            check_docker(sandbox.image)
        except SandboxUnavailable as exc:
            return _error(f"{exc} (nothing was run; pass --sandbox none to run the "
                          "hidden tests on this machine)")
    else:
        print("WARNING: --sandbox none: the hidden tests run the run's final code on "
              "this machine.", file=sys.stderr)
    try:
        stats = rescore(run_dir.resolve(), tests_dir.resolve(), checks_for(sandbox))
    except (OSError, ValueError) as exc:
        return _error(str(exc))
    print(f"hidden tests (rescored, {_where(stats['sandbox'])}): {stats['passed']} passed, "
          f"{stats['failed']} failed, {stats['errors']} errors, {stats['skipped']} skipped")
    if "cleanup_error" in stats:
        print(f"warning: cleanup: could not remove the copied tests from "
              f"{run_dir / 'final'}: {stats['cleanup_error']}", file=sys.stderr)
    print(f"wrote {run_dir / 'hidden_tests_rescored.txt'}, "
          f"{run_dir / 'hidden_tests_rescored.xml'}, and hidden_tests_rescored in "
          f"{run_dir / 'summary.json'}")
    if "parse_error" in stats or stats["timed_out"]:
        why = "timed out" if stats["timed_out"] else f"exit code {stats['exit_code']}"
        print(f"error: the hidden tests gave no result ({why}); see "
              f"{run_dir / 'hidden_tests_rescored.txt'}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_OK


def _cmd_sweevo_eval(args: argparse.Namespace) -> int:
    from .gitops import GitError
    from .sandbox import SandboxUnavailable, check_docker
    from .sweevo import SweevoError, evaluate_run, load_task

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        return _error(f"run directory not found: {run_dir}")
    if args.timeout <= 0:
        return _error("--timeout must be > 0")
    try:
        task = load_task(Path(args.task), Path(args.exclude) if args.exclude else None)
        check_docker(task.sandbox.image)
        entry = evaluate_run(run_dir, task, timeout=args.timeout,
                             keep_scratch=args.keep_scratch)
    except SandboxUnavailable as exc:
        return _error(f"{exc} (nothing was run)")
    except (SweevoError, GitError, OSError) as exc:
        return _error(str(exc))
    f2p, p2p = entry["f2p"], entry["p2p"]
    print(f"{task.instance_id}: F2P {f2p['passed']}/{f2p['total']}  "
          f"P2P {p2p['passed']}/{p2p['total']}  resolved: {entry['resolved']}  "
          f"fix rate: {entry['fix_rate']:.3f}  Q: {entry['q']:.3f}")
    print(f"excluded tests: {entry['excluded_tests']}  tests in the log: "
          f"{entry['tests_in_log']}  test run: {entry['duration_s']:.1f}s  "
          f"(evaluation {entry['total_duration_s']:.1f}s, code from {entry['code']})")
    print(f"wrote {run_dir / 'sweevo_tests.txt'}, {run_dir / 'sweevo_results.json'}, and "
          f"sweevo in {run_dir / 'summary.json'}")
    if not entry["ok"]:
        print(f"error: the SWE-EVO tests gave no result: {entry['error']}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace) -> int:
    from .escapes import audit_runs, format_audit, run_dirs

    paths = [Path(path) for path in args.runs]
    missing = [str(path) for path in paths if not run_dirs(path)]
    if missing:
        return _error(f"no run directory found in: {', '.join(missing)}")
    rows = audit_runs(paths)
    print(format_audit(rows))
    return EXIT_INVALID if any(row.escapes for row in rows) else EXIT_OK


def _cmd_single(args: argparse.Namespace) -> int:
    from .single import single_graph
    from .gitops import GitError

    try:
        request = Path(args.request).read_text(encoding="utf-8")
        graph = single_graph(request, Path(args.repo), list(args.final_check))
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        dump_graph(graph, out)
    except (GitError, OSError, UnicodeDecodeError, ValueError) as exc:
        return _error(str(exc))
    print(f"wrote {out}")
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace) -> int:
    from .gitops import GitError
    from . import planner
    from .planner import PlanOptions, run_plan
    from .synthesize import parse_ablate

    request_path = Path(args.request)
    if not request_path.is_file():
        return _error(f"request file not found: {request_path}")
    if not Path(args.repo).is_dir():
        return _error(f"repository not found: {args.repo}")
    if args.timeout <= 0:
        return _error("--timeout must be > 0")
    if args.fast_path_lines < 0:
        return _error("--fast-path-lines must be >= 0")
    if args.max_calls < 0 or args.max_input_tokens < 0 or args.max_time < 0:
        return _error("--max-calls, --max-input-tokens and --max-time must be >= 0 "
                      "(0: no limit)")
    try:
        ablate = parse_ablate(args.ablate)
    except ValueError as exc:
        return _error(f"--ablate: {exc}")
    if args.planner == "v2" and ablate:
        return _error("--ablate applies only to --planner v3")
    if args.planner == "v3" and args.no_merge:
        return _error("--no-merge applies only to --planner v2 (v3 does not merge by M1/M2)")
    if args.planner == "v2" and args.outline == "two-level":
        return _error("--outline two-level applies only to --planner v3 "
                      "(v2 always has the full outline)")
    per_item_flags = {"--max-investigators": args.max_investigators,
                      "--investigator-max-calls": args.investigator_max_calls,
                      "--investigator-max-input-tokens": args.investigator_max_input_tokens,
                      "--investigator-max-time": args.investigator_max_time}
    if args.facts == "per-item":
        if args.planner == "v2":
            return _error("--facts per-item applies only to --planner v3")
        if args.outline == "full":
            return _error("--facts per-item builds a two-level outline for every item; it "
                          "cannot use --outline full")
        if args.max_investigators is not None and args.max_investigators < 1:
            return _error("--max-investigators must be >= 1")
        if any(value is not None and value < 0 for flag, value in per_item_flags.items()
               if flag != "--max-investigators"):
            return _error("--investigator-max-calls, --investigator-max-input-tokens and "
                          "--investigator-max-time must be >= 0 (0: no limit)")
    else:
        given = [flag for flag, value in per_item_flags.items() if value is not None]
        if given:
            return _error(f"{', '.join(given)} applies only to --facts per-item")
    outline = args.outline or ("two-level" if args.planner == "v3" else "full")
    investigator = {}
    if args.facts == "per-item":
        for name, value in (("max_investigators", args.max_investigators),
                            ("investigator_max_calls", args.investigator_max_calls),
                            ("investigator_max_input_tokens",
                             args.investigator_max_input_tokens),
                            ("investigator_max_s", args.investigator_max_time)):
            if value is not None:  # 0 turns an investigator limit off
                investigator[name] = value if name == "max_investigators" else (value or None)
    options = PlanOptions(request_path=request_path, repo=Path(args.repo),
                          out=Path(args.out), final_checks=list(args.final_check),
                          timeout_s=args.timeout, request_id=args.request_id,
                          fast_path_lines=None if args.no_fast_path else args.fast_path_lines,
                          merge=not args.no_merge, planner=args.planner, ablate=ablate,
                          outline=outline, max_model_calls=args.max_calls or None,
                          max_input_tokens=args.max_input_tokens or None,
                          max_plan_s=args.max_time or None, facts=args.facts,
                          **investigator)
    try:
        result = run_plan(options, planner.planner_worker(), planner.draft_worker())
    except (GitError, RuntimeError, OSError, UnicodeDecodeError) as exc:
        return _error(str(exc))
    if args.planner == "v3":
        return _print_plan_v3(args, result)
    return _print_plan_v2(args, result)


def _print_plan_start(report: dict) -> None:
    fast = report["fast_path"]
    estimate = fast["estimated_changed_lines"]
    print(f"draft items: {len(report['draft_items'])}  estimated changed lines: "
          f"{'-' if estimate is None else estimate}")
    if fast["threshold"] is None:
        print("fast path: off")
    elif fast["taken"]:
        print(f"fast path: taken ({estimate} < {fast['threshold']} lines): "
              "single-agent graph, steps 2 and 3 skipped")
    elif estimate is not None:
        print(f"fast path: not taken ({estimate} >= {fast['threshold']} lines)")


def _print_plan_end(args: argparse.Namespace, result, report: dict) -> None:
    from .planner import REPORTED_GOAL_CHARS

    totals = report["totals"]
    print(f"model calls: {totals['model_calls']}  tokens: {totals['input_tokens']} in / "
          f"{totals['output_tokens']} out  time: {report['duration_s']:.1f}s")
    for entry in report.get("round_usage", []):
        if "item" in entry:
            continue  # an investigator: printed below with its details
        print(f"  step {entry['step']} round {entry['round']}: {entry['model_calls']} calls, "
              f"{entry['input_tokens']:,} in / {entry['output_tokens']:,} out, "
              f"{entry['duration_s']:.0f}s, {entry['ended']}")
    if report.get("facts_mode") == "per-item" and "investigators" in report:
        _print_investigators(report)
    elif report.get("outline_mode") == "per-item":  # resynth of a per-item plan
        print(f"outlines: one per draft item ({len(report.get('outline_per_item') or {})} "
              "items, see outline_per_item in the plan's report)")
    elif report.get("outline_mode") == "two-level":
        cut = report.get("outline_level2_cut") or []
        print(f"outline (two-level): {report['outline_chars']} characters, level 1 "
              f"{report['outline_level1_chars']}, level 2 {report['outline_level2_chars']} "
              f"({len(report['outline_level2_files'])} files; {len(cut)} cut by the budget); "
              f"file list {report['file_list_chars']}")
    else:
        print(f"outline: {report['outline_chars']} characters "
              f"(shortened: {report['outline_reduction']})")
    for node_id, chars in report["long_goals"].items():
        print(f"note: node {node_id} goal has {chars} characters (over "
              f"{REPORTED_GOAL_CHARS}): goal repeats the request; the worker already "
              "receives it")
    if not result.success:
        print(f"errors in the last round ({len(report['errors'])}):")
        for issue in report["errors"]:
            nodes = f" {', '.join(issue['nodes'])}:" if issue["nodes"] else ""
            print(f"[{issue['code']}]{nodes} {issue['message']}")
    if report["graph_written"]:
        print(f"wrote {args.out}")


def _count(value) -> str:
    return "-" if value is None else str(value)


def investigator_line(record: dict) -> str:
    """One investigator run of a per-item plan, on one line."""
    rerun = ", run again alone" if record["rerun"] else ""
    return (f"  investigator item {record['item']} (round {record['round']}{rerun}): "
            f"{record['model_calls']} calls, {record['input_tokens']:,} in / "
            f"{record['output_tokens']:,} out, {record['duration_s']:.0f}s, "
            f"{_count(record['compactions'])} compactions, "
            f"{_count(record['read_file'])} read_file, prompt {record['prompt_chars']:,} "
            f"characters, level 2: {len(record['level2_files'])} files, {record['ended']}")


def _print_investigators(report: dict) -> None:
    limits = report["investigator_limits"]
    print(f"facts: per item, at most {report['max_investigators']} investigators at once; "
          f"each within {_count(limits['model_calls'])} calls, "
          f"{_count(limits['input_tokens'])} input tokens, {_count(limits['seconds'])} s")
    for record in report["investigators"]:
        print(investigator_line(record))
    print(f"step 2: {report['step2_wall_s']:.0f}s wall time, "
          f"{report['step2_investigators_s']:.0f}s in investigators together")
    revisions = "  ".join(f"{item}: {count}"
                          for item, count in report["revisions_per_item"].items())
    print(f"revisions per item: {revisions}")
    print(f"items with an answer: {', '.join(report['items_finished']) or 'none'}"
          + (f"  without: {', '.join(report['items_unfinished'])}"
             if report["items_unfinished"] else ""))


def _parallelism_line(parallelism: dict, p1_off: bool) -> str:
    """``parallelism: critical path 2405 / 2825 lines (0.85 > 0.70) -> single agent``."""
    ratio, limit = parallelism["ratio"], parallelism["threshold"]
    compare = ">" if ratio > limit else "<="
    if parallelism["collapsed"]:
        outcome = "single agent"
    elif p1_off:
        outcome = "split (p1 off)"
    else:
        outcome = "split"
    return (f"parallelism: critical path {parallelism['critical']} / {parallelism['total']} "
            f"lines ({ratio:.2f} {compare} {limit:.2f}) -> {outcome}")


def _print_plan_v3(args: argparse.Namespace, result) -> int:
    report = result.report
    _print_plan_start(report)
    print(f"nodes: {report['nodes']}  edges: {report['edges']}  "
          f"PRE: {report['pre_node'] or 'no'}  REG: {report['reg_node'] or 'no'}  "
          f"revision rounds: {report['revision_rounds']}  "
          f"success: {'yes' if result.success else 'no'}")
    if report["decision_counts"]:
        counts = "  ".join(f"{prop}: {count}"
                           for prop, count in report["decision_counts"].items())
        print(f"units: {report['units']}  decisions: {counts}")
    parallelism = report.get("parallelism")
    if parallelism:
        print(_parallelism_line(parallelism, "p1" in report["ablate"]))
    if report["ablate"]:
        print(f"ablated: {', '.join(report['ablate'])} (rules turned off)")
    _print_plan_end(args, result, report)
    print(f"wrote {result.report_path}")
    return EXIT_OK if result.success else EXIT_INVALID


def _print_plan_v2(args: argparse.Namespace, result) -> int:
    report = result.report
    fast = report["fast_path"]
    _print_plan_start(report)
    print(f"nodes: {report['nodes']}  edges: {report['edges']}  "
          f"revision rounds: {report['revision_rounds']}  "
          f"success: {'yes' if result.success else 'no'}")
    if not fast["taken"] and report["graph_written"]:
        merging = "" if report["merge"] else " (--no-merge)"
        print(f"nodes before merge: {report['nodes_before_merge']}  after: "
              f"{report['nodes']}{merging}")
        for merge in report["merges"]:
            print(f"merged ({merge['rule']}): {' + '.join(merge['members'])} -> "
                  f"{merge['into']}")
    _print_plan_end(args, result, report)
    if report["unmerged_graph"]:
        print(f"wrote {report['unmerged_graph']}")
    print(f"wrote {result.report_path}")
    return EXIT_OK if result.success else EXIT_INVALID


def read_draft_items(path: Path):
    """Draft items from a plan report, a Step 1 result file or a Step 1 answer.

    Raises ``ValueError`` when ``path`` has none.
    """
    from .planner import DraftError, DraftItem, parse_items

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("draft_items"):
        return [DraftItem.model_validate(item) for item in data["draft_items"]]
    text = data.get("final_answer") if isinstance(data, dict) else None
    try:
        return parse_items(text if isinstance(text, str) else json.dumps(data)).items
    except DraftError as exc:
        raise ValueError(f"{path} has no draft items: {exc}") from None


def _cmd_item_prompts(args: argparse.Namespace) -> int:
    import tempfile

    from . import gitops
    from .cleanup import remove_tree
    from .planner_items import item_prompts
    from .workers import write_json_atomic

    request_path, repo = Path(args.request), Path(args.repo)
    if not request_path.is_file():
        return _error(f"request file not found: {request_path}")
    if not repo.is_dir():
        return _error(f"repository not found: {repo}")
    try:
        items = read_draft_items(Path(args.draft_items))
        request = request_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _error(str(exc))
    tmp = Path(tempfile.mkdtemp(prefix="tg-item-prompts-"))
    try:
        clone = tmp / "repo"
        base_commit = gitops.head(repo.resolve())
        gitops.clone_for_run(repo.resolve(), clone, base_commit)
        index = build_index(clone, base_commit)
        prompts = item_prompts(request, clone, index, items)
    except (gitops.GitError, RuntimeError, OSError) as exc:
        return _error(str(exc))
    finally:
        remove_tree(tmp)
    out = Path(args.out) if args.out else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    summary = []
    print(f"{len(prompts)} investigators (no model is called); base commit {base_commit}")
    for entry in prompts:
        outline = entry.outline
        files = [chosen.path for chosen in outline.chosen]
        print(f"item {entry.item.id} ({entry.item.title}): prompt {len(entry.prompt):,} "
              f"characters, outline {outline.chars:,} (level 1 {outline.level1_chars:,}"
              + (f", {len(outline.collapsed)} directories collapsed" if outline.collapsed
                 else "")
              + f", level 2 {outline.level2_chars:,}: {len(files)} files"
              + (f", {len(outline.cut)} cut" if outline.cut else "") + ")")
        for path in files:
            print(f"    {path}")
        summary.append({"item": entry.item.id, "title": entry.item.title,
                        "prompt_chars": len(entry.prompt), **outline.report()})
        if out is not None:
            name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in entry.item.id)
            (out / f"item-{name}.md").write_text(entry.prompt, encoding="utf-8")
    if out is not None:
        write_json_atomic(out / "item_prompts.json", {
            "request": str(request_path), "repo": str(repo), "base_commit": base_commit,
            "draft_items": str(args.draft_items), "items": summary})
        print(f"wrote the prompts and item_prompts.json to {out}")
    return EXIT_OK


def _cmd_resynth(args: argparse.Namespace) -> int:
    from .gitops import GitError
    from .planner_v3 import ResynthError, run_resynth
    from .synthesize import parse_ablate

    try:
        ablate = parse_ablate(args.ablate)
    except ValueError as exc:
        return _error(f"--ablate: {exc}")
    plan_path = Path(args.graph)
    try:
        result = run_resynth(plan_path, Path(args.out), ablate,
                             final_checks=list(args.final_check) or None)
    except ResynthError as exc:
        return _error(str(exc))
    except (GitError, RuntimeError, OSError, UnicodeDecodeError) as exc:
        return _error(str(exc))
    print(f"synthesized again from the facts of {plan_path} (no model calls)")
    return _print_plan_v3(args, result)


def _cmd_compare(args: argparse.Namespace) -> int:
    from .compare import compare_graphs, format_comparison

    planner = _load(args.planner_graph)
    handwritten = _load(args.handwritten_graph)
    if planner is None or handwritten is None:
        return EXIT_INPUT_ERROR
    if not Path(args.repo).is_dir():
        return _error(f"repository not found: {args.repo}")
    result = compare_graphs(planner, handwritten, Path(args.repo))
    text = format_comparison(result, Path(args.planner_graph).name,
                             Path(args.handwritten_graph).name)
    print(text, end="")
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(result, indent=2) + "\n",
                                       encoding="utf-8")
        except OSError as exc:
            return _error(f"cannot write {args.json}: {exc.strerror or exc}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(prog="python -m aqours_code.taskgraph")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_cmd = commands.add_parser("validate", help="validate a task graph")
    validate_cmd.add_argument("graph")
    validate_cmd.add_argument("--repo", help="repository indexed at the graph's base_commit")
    validate_cmd.set_defaults(func=_cmd_validate)

    derive_cmd = commands.add_parser("derive", help="add rule-derived edges")
    derive_cmd.add_argument("graph")
    derive_cmd.add_argument("--repo", required=True)
    derive_cmd.add_argument("--out", required=True)
    derive_cmd.set_defaults(func=_cmd_derive)

    index_cmd = commands.add_parser("index", help="print the symbols of a commit")
    index_cmd.add_argument("--repo", required=True)
    index_cmd.add_argument("--commit", required=True)
    index_cmd.set_defaults(func=_cmd_index)

    schema_cmd = commands.add_parser("export-schema", help="regenerate the JSON Schema")
    schema_cmd.add_argument("--out", default=str(DEFAULT_SCHEMA_PATH))
    schema_cmd.set_defaults(func=_cmd_export_schema)

    run_cmd = commands.add_parser("run", help="execute a task graph with workers")
    run_cmd.add_argument("graph")
    run_cmd.add_argument("--repo", required=True)
    run_cmd.add_argument("--out", default="runs")
    run_cmd.add_argument("--workers", type=int, default=2)
    run_cmd.add_argument("--max-attempts", type=int, default=2)
    run_cmd.add_argument("--worker", choices=("aqours", "command"), default="aqours")
    run_cmd.add_argument("--command-map",
                         help="JSON file mapping node id to a shell command (--worker command)")
    run_cmd.add_argument("--worker-timeout", type=float, default=1800.0)
    run_cmd.add_argument("--hidden-tests")
    run_cmd.add_argument("--merge-repairs", type=int, default=1,
                         help="repair rounds for a node whose post-merge check fails "
                              "(default 1; 0 fails the node at once)")
    run_cmd.add_argument("--conflict-repairs", type=int, default=1,
                         help="conflict resolutions in each round of merging: the first "
                              "merge and the merge after each merge repair (default 1; "
                              "0 fails the node at once on a conflict)")
    run_cmd.add_argument("--sandbox", choices=("docker", "none"), default="docker",
                         help="where worker shell commands, checks and hidden tests run "
                              "(none: on this machine, tests only)")
    run_cmd.add_argument("--sandbox-image",
                         help="Docker image for --sandbox docker "
                              "(default aqours-code-eval:py311)")
    run_cmd.add_argument("--sandbox-config",
                         help="a task's sandbox.json: the image, environment variables, "
                              "open-files and process limits and git view of every container "
                              "(instead of --sandbox-image)")
    run_cmd.set_defaults(func=_cmd_run)

    team_cmd = commands.add_parser(
        "team", help="plan-then-parallel baseline: a lead plans, teammates work in parallel")
    team_cmd.add_argument("request", help="request text (Markdown)")
    team_cmd.add_argument("--repo", required=True)
    team_cmd.add_argument("--out", default="runs")
    team_cmd.add_argument("--workers", type=int, default=4,
                          help="teammates working at the same time (default 4)")
    team_cmd.add_argument("--max-attempts", type=int, default=2)
    team_cmd.add_argument("--worker-timeout", type=float, default=1800.0)
    team_cmd.add_argument("--lead-timeout", type=float, default=1800.0,
                          help="timeout in seconds of each lead call (planning, wrap-up)")
    team_cmd.add_argument("--final-check", action="append", default=[],
                          help="command run on the merged work (repeatable)")
    team_cmd.add_argument("--hidden-tests")
    team_cmd.add_argument("--sandbox", choices=("docker", "none"), default="docker",
                          help="where teammates' shell commands, checks and hidden tests "
                               "run (none: on this machine, tests only)")
    team_cmd.add_argument("--sandbox-image",
                          help="Docker image for --sandbox docker "
                               "(default aqours-code-eval:py311)")
    team_cmd.add_argument("--sandbox-config",
                          help="a task's sandbox.json: the image, environment variables, "
                               "open-files and process limits and git view of every container "
                               "(instead of --sandbox-image)")
    team_cmd.set_defaults(func=_cmd_team)

    score_cmd = commands.add_parser(
        "score", help="composite score of runs against the single-agent baseline")
    score_cmd.add_argument("manifest", help="JSON list of {task, method, run[, plan_report]}")
    score_cmd.add_argument("--exclude", help="file of pytest node ids to leave out")
    score_cmd.add_argument("--out", help="directory for score.md and score.json")
    score_cmd.set_defaults(func=_cmd_score)

    rescore_cmd = commands.add_parser(
        "rescore", help="run the hidden tests again on the final code of a finished run")
    rescore_cmd.add_argument("run_dir", help="a run directory (it has final/ and summary.json)")
    rescore_cmd.add_argument("--hidden-tests", required=True)
    rescore_cmd.add_argument("--sandbox", choices=("docker", "none"), default="docker",
                             help="where the tests run (default docker)")
    rescore_cmd.add_argument("--sandbox-image",
                             help="Docker image (default: the run's own, else "
                                  "aqours-code-eval:py311)")
    rescore_cmd.add_argument("--sandbox-config",
                             help="a task's sandbox.json (default: the run's own image "
                                  "and container options)")
    rescore_cmd.set_defaults(func=_cmd_rescore)

    sweevo_cmd = commands.add_parser(
        "sweevo-eval", help="grade a run of a SWE-EVO task with the instance's F2P/P2P tests")
    sweevo_cmd.add_argument("run_dir", help="a finished run directory (final/ or repo/)")
    sweevo_cmd.add_argument("--task", required=True,
                            help="the prepared task directory (grading/, sandbox.json)")
    sweevo_cmd.add_argument("--exclude",
                            help="test ids to leave out (default <task>/sandbox_skips.txt)")
    sweevo_cmd.add_argument("--timeout", type=float, default=3600.0,
                            help="seconds the test command may run (default 3600)")
    sweevo_cmd.add_argument("--keep-scratch", action="store_true",
                            help="keep sweevo_eval/, the graded copy of the code")
    sweevo_cmd.set_defaults(func=_cmd_sweevo_eval)

    audit_cmd = commands.add_parser(
        "audit", help="find workers that reached outside their workspace in past runs")
    audit_cmd.add_argument("runs", nargs="+",
                           help="a run directory, or a directory of run directories")
    audit_cmd.set_defaults(func=_cmd_audit)

    single_cmd = commands.add_parser("single", help="generate an unrestricted single-agent control")
    single_cmd.add_argument("request", help="original request text (Markdown)")
    single_cmd.add_argument("--repo", required=True)
    single_cmd.add_argument("--out", required=True)
    single_cmd.add_argument("--final-check", action="append", default=[],
                            help="test command for the node and final_checks (repeatable)")
    single_cmd.set_defaults(func=_cmd_single)

    plan_cmd = commands.add_parser("plan", help="generate a task graph with the planner")
    plan_cmd.add_argument("request", help="request text (Markdown)")
    plan_cmd.add_argument("--repo", required=True)
    plan_cmd.add_argument("--out", required=True)
    plan_cmd.add_argument("--final-check", action="append", default=[],
                          help="command for the graph's final_checks (repeatable)")
    plan_cmd.add_argument("--timeout", type=float, default=1800.0,
                          help="timeout in seconds of each planner round")
    plan_cmd.add_argument("--max-calls", type=int, default=DEFAULT_MAX_MODEL_CALLS,
                          help="model calls of the whole plan, all rounds of both steps "
                               f"(default {DEFAULT_MAX_MODEL_CALLS}; 0: no limit)")
    plan_cmd.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS,
                          help="input tokens of the whole plan "
                               f"(default {DEFAULT_MAX_INPUT_TOKENS}; 0: no limit)")
    plan_cmd.add_argument("--max-time", type=float, default=DEFAULT_MAX_PLAN_S,
                          help="seconds of the whole plan; each round's timeout is cut to "
                               f"the time left (default {DEFAULT_MAX_PLAN_S:.0f}; 0: no limit)")
    plan_cmd.add_argument("--fast-path-lines", type=int, default=500,
                          help="estimated changed lines below which the request is not "
                               "split (default 500)")
    plan_cmd.add_argument("--no-fast-path", action="store_true",
                          help="always split, whatever the estimate")
    plan_cmd.add_argument("--planner", choices=("v2", "v3"), default="v3",
                          help="planner version (default v3: the model reports task facts, "
                               "the program builds the graph)")
    plan_cmd.add_argument("--ablate", default="",
                          help="v3 only: turn off the rules of properties, e.g. p1,p3 "
                               "(each falls back to its simplest behaviour)")
    plan_cmd.add_argument("--outline", choices=("two-level", "full"),
                          help="v3 only: the outline in the planner's prompt: two-level "
                               "(default: every source file's top-level names, details only "
                               "for files the request is about) or full (every definition, "
                               "as in planner v3.1)")
    plan_cmd.add_argument("--facts", choices=FACTS_MODES, default="single",
                          help="v3 only: who describes the work in step 2: single (default: "
                               "one agent for the whole request) or per-item (one "
                               "investigator per draft item, in parallel, each with its own "
                               "outline; the program merges their answers)")
    plan_cmd.add_argument("--max-investigators", type=int,
                          help=f"--facts per-item: investigators running at once "
                               f"(default {DEFAULT_MAX_INVESTIGATORS})")
    plan_cmd.add_argument("--investigator-max-calls", type=int,
                          help=f"--facts per-item: model calls of one investigator run "
                               f"(default {DEFAULT_INVESTIGATOR_CALLS}; 0: no limit); the "
                               "plan's --max-calls covers all of them together")
    plan_cmd.add_argument("--investigator-max-input-tokens", type=int,
                          help=f"--facts per-item: input tokens of one investigator run "
                               f"(default {DEFAULT_INVESTIGATOR_INPUT_TOKENS}; 0: no limit)")
    plan_cmd.add_argument("--investigator-max-time", type=float,
                          help=f"--facts per-item: seconds of one investigator run "
                               f"(default {DEFAULT_INVESTIGATOR_S:.0f}; 0: no limit)")
    plan_cmd.add_argument("--no-merge", action="store_true",
                          help="v2 only: skip step 3 (rule-based merging)")
    plan_cmd.add_argument("--request-id", help="request_id of the graph "
                          "(default: the --out file name without extension)")
    plan_cmd.set_defaults(func=_cmd_plan)

    prompts_cmd = commands.add_parser(
        "item-prompts", help="build the prompts of plan --facts per-item for saved draft "
                             "items, without calling a model (a dry run of step 2)")
    prompts_cmd.add_argument("request", help="request text (Markdown)")
    prompts_cmd.add_argument("--repo", required=True)
    prompts_cmd.add_argument("--draft-items", required=True,
                             help="a plan report (draft_items), or the result file of a "
                                  "step 1 agent (its final answer)")
    prompts_cmd.add_argument("--out", help="directory for the prompts (item-<id>.md) and "
                                           "item_prompts.json")
    prompts_cmd.set_defaults(func=_cmd_item_prompts)

    resynth_cmd = commands.add_parser(
        "resynth", help="build a v3 graph again from the facts saved with it (no model)")
    resynth_cmd.add_argument("graph", help="a planner v3 graph, or its report "
                                          "(<graph>.report.json, which has the facts); "
                                          "a failed plan has only the report")
    resynth_cmd.add_argument("--out", required=True)
    resynth_cmd.add_argument("--ablate", default="",
                             help="turn off the rules of properties, e.g. p1,p3")
    resynth_cmd.add_argument("--final-check", action="append", default=[],
                             help="command for the graph's final_checks (repeatable); "
                                  "required when there is no saved graph, replaces the "
                                  "saved graph's otherwise")
    resynth_cmd.set_defaults(func=_cmd_resynth)

    compare_cmd = commands.add_parser("compare", help="compare two graphs' structure")
    compare_cmd.add_argument("planner_graph")
    compare_cmd.add_argument("handwritten_graph")
    compare_cmd.add_argument("--repo", required=True)
    compare_cmd.add_argument("--json", help="also write the comparison as JSON")
    compare_cmd.set_defaults(func=_cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)
