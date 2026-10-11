"""Merge repair: a node whose post-merge check fails is fixed on the merged state.

A and B run in parallel on different files. A's file changes a shared
behaviour: B's check passes in B's own worktree (where A's file is absent)
and fails once both are merged. No model is called (``CommandWorker``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.prompting import (
    FAILURE_EXCERPT_CHARS,
    AttemptFailure,
    build_node_prompt,
)
from aqours_code.taskgraph.workers import CommandWorker
from taskgraph_support import make_edge, make_graph, make_node


def py(code: str) -> str:
    """A cross-platform shell command running Python ``code`` (no double quotes)."""
    assert '"' not in code
    return f'"{sys.executable}" -c "{code}"'


WRITE_A = py("import pathlib; pathlib.Path('a.txt').write_text('A')")
# Python statements that wait until A's file is in the integration clone
# (runs/<id>/repo, next to runs/<id>/wt/B), so that A is always merged first.
# ``merged`` says whether A's work is already in B's own workspace (a repair).
WAIT_FOR_A = ("import os, pathlib, time; merged = os.path.exists('a.txt'); "
              "end = time.time() + 60; "
              "[time.sleep(0.05) for _ in iter(lambda: merged or "
              "os.path.exists('../../repo/a.txt') or time.time() > end, True)]; ")
# B writes b.txt; in a repair round it writes the version that works with A.
WRITE_B = py(WAIT_FOR_A + "pathlib.Path('b.txt').write_text('fixed' if merged else 'B')")
# The same, but B never adapts to A.
WRITE_B_STUBBORN = py(WAIT_FOR_A + "pathlib.Path('b.txt').write_text('B'); "
                      "pathlib.Path('note.txt').write_text('tried')")
# B's check: fine alone, broken by A unless b.txt is the fixed version.
CHECK_B = py("import os, pathlib, sys; ok = (not os.path.exists('a.txt')) or "
             "pathlib.Path('b.txt').read_text() == 'fixed'; "
             "print('B-OK' if ok else 'B-BROKEN-BY-A'); sys.exit(0 if ok else 1)")
# A check that passes in any worktree and fails in the integration clone once
# A is there: the repair round passes its own check, and merging fails again.
CHECK_B_ONLY_FAILS_MERGED = py(
    "import os, sys; bad = os.path.exists('a.txt') and "
    "os.path.basename(os.getcwd()) == 'repo'; "
    "print('STILL-BROKEN' if bad else 'fine'); sys.exit(1 if bad else 0)")
OK = py("print('ok')")


def graph_for(toy_repo, b_check: str = CHECK_B, with_downstream: bool = False):
    nodes = [make_node("A", create=("a.txt",), commands=(OK,)),
             make_node("B", create=("b.txt", "note.txt"), commands=(b_check,))]
    edges = []
    if with_downstream:
        nodes.append(make_node("D", create=("d.txt",), commands=(OK,)))
        edges.append(make_edge("B", "D"))
    graph = make_graph(nodes, edges, final_checks=(OK,))
    return graph.model_copy(update={"base_commit": toy_repo.commit})


def run(toy_repo, tmp_path, graph, b_command: str = WRITE_B, **options):
    commands = {"A": WRITE_A, "B": b_command,
                "D": py("import pathlib; pathlib.Path('d.txt').write_text('D')")}
    return run_graph(graph, toy_repo.path, CommandWorker(commands),
                     RunOptions(out_dir=tmp_path / "runs", workers=2, **options))


def events(result, node: str | None = None) -> list[dict]:
    lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    found = [json.loads(line) for line in lines]
    return [event for event in found if node is None or event.get("node") == node]


def subjects(result) -> list[str]:
    return gitops.commit_subjects(result.run_dir / "repo",
                                  result.summary["config"]["base_commit"])


def test_repair_fixes_a_node_broken_by_the_merge(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, graph_for(toy_repo))
    summary = result.summary
    assert summary["status"] == "success"
    record = summary["nodes"]["B"]
    assert (record["status"], record["reason"], record["attempts"]) == ("merged", "", 2)
    assert (record["merge_repairs"], record["merge_repair_ok"]) == (1, True)
    assert (summary["nodes"]["A"]["merge_repairs"],
            summary["nodes"]["A"]["merge_repair_ok"]) == (0, None)
    assert summary["merge_repairs_total"] == 1
    assert summary["config"]["merge_repairs"] == 1
    # the integration branch holds both nodes' work, one commit each
    assert subjects(result) == ["[taskgraph] A: A", "[taskgraph] B: B"]
    repo = result.run_dir / "repo"
    assert (repo / "a.txt").read_text() == "A" and (repo / "b.txt").read_text() == "fixed"
    assert gitops.is_clean(repo)
    # B's own changes are measured against the merged-in integration commit
    assert record["changed_files"] == ["b.txt"] and record["out_of_scope_files"] == []
    patch = (result.run_dir / "nodes" / "B" / "diff.patch").read_text(encoding="utf-8")
    assert "b.txt" in patch and "a.txt" not in patch

    types = [(event["type"], event.get("ok")) for event in events(result, "B")]
    assert types == [
        ("node_ready", None), ("node_start", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None),
        ("check_end", True),
        ("merge_start", None), ("merge_end", True), ("post_merge_check_end", False),
        ("merge_repair_start", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None),
        ("check_end", True),
        ("merge_start", None), ("merge_end", True), ("post_merge_check_end", True),
        ("merge_repair_end", True), ("node_merged", None)]
    start = next(e for e in events(result, "B") if e["type"] == "merge_repair_start")
    assert start["attempt"] == 2
    log = result.run_dir / "nodes" / "B"
    assert "B-BROKEN-BY-A" in (log / "post_merge_check.txt").read_text(encoding="utf-8")
    assert "B-OK" in (log / "post_merge_check_repair_1.txt").read_text(encoding="utf-8")
    assert "B-OK" in (log / "check_2.txt").read_text(encoding="utf-8")
    assert (log / "context_2.md").is_file()


def test_repair_prompt_describes_the_merge_failure(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, graph_for(toy_repo))
    log = result.run_dir / "nodes" / "B"
    first = (log / "prompt_1.md").read_text(encoding="utf-8")
    repair = (log / "prompt_2.md").read_text(encoding="utf-8")
    assert "# Your work broke after merging" not in first
    assert "# Your work broke after merging" in repair
    assert "# Previous attempt failed" not in repair
    assert "B-BROKEN-BY-A" in repair
    assert ("Fix the failure by changing only your own files (listed above). The other\n"
            "sub-tasks' code is finished and must stay as it is. If the failure cannot\n"
            "be fixed from your own files, change nothing and say why in your final\n"
            "answer.") in repair
    assert ("Your sub-task passed its own checks, but after it was merged with the work\n"
            "that other sub-tasks finished in the meantime, the checks failed. Your\n"
            "workspace now holds your work together with theirs.") in repair


def test_repair_prompt_trims_the_output_like_a_retry(toy_index):
    graph = make_graph([make_node("A", modify=("models.py",)),
                        make_node("B", modify=("runner.py",))])
    output = "x" * (FAILURE_EXCERPT_CHARS + 500) + "TAIL-MARKER"
    repair = build_node_prompt(graph, graph.nodes[0], toy_index, 2,
                               AttemptFailure("post_merge_check_failed", output,
                                              after_merge=True))
    assert "# Your work broke after merging" in repair and "TAIL-MARKER" in repair
    assert "x" * (FAILURE_EXCERPT_CHARS + 100) not in repair
    assert "# Previous attempt failed" not in repair
    retry = build_node_prompt(graph, graph.nodes[0], toy_index, 2,
                              AttemptFailure("check_failed", output))
    assert "# Previous attempt failed" in retry and "broke after merging" not in retry


def test_repair_that_breaks_again_after_merging_fails_the_node(toy_repo, tmp_path):
    graph = graph_for(toy_repo, CHECK_B_ONLY_FAILS_MERGED, with_downstream=True)
    result = run(toy_repo, tmp_path, graph)
    nodes = result.summary["nodes"]
    assert (nodes["B"]["status"], nodes["B"]["reason"]) == ("failed", "post_merge_check_failed")
    assert (nodes["B"]["merge_repairs"], nodes["B"]["merge_repair_ok"]) == (1, False)
    assert nodes["B"]["attempts"] == 2
    assert (nodes["D"]["status"], nodes["D"]["reason"]) == ("skipped", "upstream_failed")
    assert nodes["A"]["status"] == "merged" and result.summary["status"] == "partial"
    assert subjects(result) == ["[taskgraph] A: A"]      # only A is on the integration branch
    repo = result.run_dir / "repo"
    assert not (repo / "b.txt").exists() and gitops.is_clean(repo)
    kinds = [event["type"] for event in events(result, "B")]
    assert kinds.count("merge_repair_start") == 1 and kinds.count("worker_start") == 2
    assert kinds[-2:] == ["merge_repair_end", "node_failed"]
    assert [e["ok"] for e in events(result, "B") if e["type"] == "merge_repair_end"] == [False]
    assert "STILL-BROKEN" in (result.run_dir / "nodes" / "B"
                              / "post_merge_check_repair_1.txt").read_text(encoding="utf-8")


def test_repair_whose_own_check_fails_is_not_merged_again(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, graph_for(toy_repo), WRITE_B_STUBBORN)
    record = result.summary["nodes"]["B"]
    assert (record["status"], record["reason"]) == ("failed", "repair_check_failed")
    assert (record["merge_repairs"], record["merge_repair_ok"]) == (1, False)
    kinds = [event["type"] for event in events(result, "B")]
    assert kinds.count("merge_start") == 1           # no second merge was tried
    assert kinds[-2:] == ["merge_repair_end", "node_failed"]
    assert subjects(result) == ["[taskgraph] A: A"]
    assert "B-BROKEN-BY-A" in (result.run_dir / "nodes" / "B" / "check_2.txt").read_text(
        encoding="utf-8")


def test_no_merge_repairs_keeps_the_old_behaviour(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, graph_for(toy_repo), merge_repairs=0)
    record = result.summary["nodes"]["B"]
    assert (record["status"], record["reason"]) == ("failed", "post_merge_check_failed")
    assert record["attempts"] == 1
    assert (record["merge_repairs"], record["merge_repair_ok"]) == (0, None)
    assert result.summary["merge_repairs_total"] == 0
    assert result.summary["config"]["merge_repairs"] == 0
    kinds = [event["type"] for event in events(result)]
    assert "merge_repair_start" not in kinds and "merge_repair_end" not in kinds
    assert [event["type"] for event in events(result, "B")] == [
        "node_ready", "node_start", "worker_start", "worker_end", "check_start",
        "check_end", "merge_start", "merge_end", "post_merge_check_end", "node_failed"]
    assert subjects(result) == ["[taskgraph] A: A"]
    assert not (result.run_dir / "nodes" / "B" / "prompt_2.md").exists()


def test_node_check_retries_and_merge_repairs_are_independent(toy_repo, tmp_path):
    # B's first attempt fails its own check (writes nothing useful), the second
    # passes it; the merge then needs one repair on top of the two attempts.
    command = py(WAIT_FOR_A + "n = os.environ['TG_ATTEMPT']; "
                 "pathlib.Path('b.txt').write_text('fixed' if merged else 'B' + n)")
    check = py("import os, pathlib, sys; text = pathlib.Path('b.txt').read_text(); "
               "ok = text == 'fixed' or (text == 'B2' and not os.path.exists('a.txt')); "
               "sys.exit(0 if ok else 1)")
    result = run(toy_repo, tmp_path, graph_for(toy_repo, check), command)
    record = result.summary["nodes"]["B"]
    assert (record["status"], record["attempts"]) == ("merged", 3)
    assert (record["merge_repairs"], record["merge_repair_ok"]) == (1, True)
    assert record["worker_reasons"] == ["", "", ""]


def test_merge_commit_into_restores_the_worktree_after_a_conflict(toy_repo, tmp_path):
    repo = tmp_path / "clone"
    base = gitops.clone_for_run(toy_repo.path, repo, toy_repo.commit)
    worktree = tmp_path / "wt"
    gitops.add_node_worktree(repo, "N", worktree, base)
    (repo / "README.md").write_text("integration side\n", encoding="utf-8")
    gitops.commit_all(repo, "integration change")
    other = gitops.head(repo)
    (worktree / "README.md").write_text("node side\n", encoding="utf-8")
    gitops.commit_all(worktree, "node change")
    assert gitops.merge_commit_into(worktree, other, "merge for repair") is False
    assert gitops.is_clean(worktree)
    assert (worktree / "README.md").read_text(encoding="utf-8") == "node side\n"
    # without a conflict the merge brings the other side's work in
    clean = tmp_path / "wt2"
    gitops.add_node_worktree(repo, "M", clean, base)
    (clean / "extra.txt").write_text("node file\n", encoding="utf-8")
    gitops.commit_all(clean, "node change")
    assert gitops.merge_commit_into(clean, other, "merge for repair") is True
    assert (clean / "README.md").read_text(encoding="utf-8") == "integration side\n"
    assert (clean / "extra.txt").is_file() and gitops.is_clean(clean)
    assert gitops.changed_files(clean, other) == ["extra.txt"]


def test_run_command_takes_merge_repairs_and_reports_them(toy_repo, tmp_path, capsys):
    graph = graph_for(toy_repo)
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps({"A": WRITE_A, "B": WRITE_B}), encoding="utf-8")
    base = ["run", str(graph_path), "--repo", str(toy_repo.path), "--worker", "command",
            "--command-map", str(command_map), "--workers", "2"]
    assert main([*base, "--out", str(tmp_path / "with")]) == 0
    assert "merge repairs: 1 (B; repaired: B)" in capsys.readouterr().out
    assert main([*base, "--out", str(tmp_path / "without"), "--merge-repairs", "0"]) == 1
    output = capsys.readouterr().out
    assert "merge repairs: 0" in output and "B: failed (post_merge_check_failed)" in output
    assert main([*base, "--out", str(tmp_path / "bad"), "--merge-repairs", "-1"]) == 2
    assert not Path(tmp_path / "bad").exists()
