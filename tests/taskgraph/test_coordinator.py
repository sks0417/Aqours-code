"""Coordinator runs with CommandWorker on the toy repository (no model calls)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import GraphInvalid, RunOptions, run_graph
from aqours_code.taskgraph.workers import CommandWorker
from taskgraph_support import make_edge, make_graph, make_node


def py(code: str) -> str:
    """A cross-platform shell command running Python ``code`` (no double quotes)."""
    assert '"' not in code
    return f'"{sys.executable}" -c "{code}"'


def write(name: str, text: str, sleep: float = 0) -> str:
    pause = f"time.sleep({sleep}); " if sleep else ""
    return py(f"import pathlib, time; {pause}pathlib.Path('{name}').write_text('{text}')")


def has(name: str, text: str) -> str:
    return py(f"import pathlib, sys; sys.exit(0 if pathlib.Path('{name}').read_text() "
              f"== '{text}' else 1)")


FINAL_OK = py("print('final ok')")


def node(node_id: str, create: tuple = (), modify: tuple = (), check: str | None = None):
    target = (create or modify)[0]
    return make_node(node_id, create=create, modify=modify,
                     commands=(check or has(target, node_id),))


def graph_for(toy_repo, nodes, edges=()):
    graph = make_graph(nodes, list(edges), final_checks=(FINAL_OK,))
    return graph.model_copy(update={"base_commit": toy_repo.commit})


def run(toy_repo, tmp_path, graph, commands, **options):
    options.setdefault("workers", 2)
    return run_graph(graph, toy_repo.path, CommandWorker(commands),
                     RunOptions(out_dir=tmp_path / "runs", **options))


def events(result) -> list[dict]:
    lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def event_t(evts, event_type, node_id, attempt=None):
    for event in evts:
        if (event["type"] == event_type and event.get("node") == node_id
                and (attempt is None or event.get("attempt") == attempt)):
            return event["t"]
    raise AssertionError(f"no {event_type} event for {node_id}")


def integration_subjects(result) -> list[str]:
    repo = result.run_dir / "repo"
    return gitops.commit_subjects(repo, result.summary["config"]["base_commit"])


DIAMOND_EDGES = [make_edge("A", "B"), make_edge("A", "C"),
                 make_edge("B", "D"), make_edge("C", "D")]


def diamond(toy_repo):
    return graph_for(toy_repo, [node(n, create=(f"{n.lower()}.txt",)) for n in "ABCD"],
                     DIAMOND_EDGES)


def diamond_commands(sleep: float = 0, b_command: str | None = None) -> dict:
    return {"A": write("a.txt", "A"),
            "B": b_command or write("b.txt", "B", sleep),
            "C": write("c.txt", "C", sleep),
            "D": write("d.txt", "D")}


def test_parallel_diamond(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, diamond(toy_repo), diamond_commands(sleep=0.5),
                 workers=2)
    summary = result.summary
    assert summary["status"] == "success"
    assert {n: s["status"] for n, s in summary["nodes"].items()} == dict.fromkeys(
        "ABCD", "merged")
    evts = events(result)
    b = (event_t(evts, "worker_start", "B"), event_t(evts, "worker_end", "B"))
    c = (event_t(evts, "worker_start", "C"), event_t(evts, "worker_end", "C"))
    assert b[0] < c[1] and c[0] < b[1], (b, c)
    a_merged = event_t(evts, "node_merged", "A")
    assert event_t(evts, "node_start", "B") >= a_merged
    assert event_t(evts, "node_start", "C") >= a_merged
    assert event_t(evts, "node_start", "D") >= max(event_t(evts, "node_merged", "B"),
                                                    event_t(evts, "node_merged", "C"))
    subjects = integration_subjects(result)
    assert len(subjects) == 4
    assert subjects[0] == "[taskgraph] A: A" and subjects[-1] == "[taskgraph] D: D"
    assert sorted(subjects[1:3]) == ["[taskgraph] B: B", "[taskgraph] C: C"]
    for name in ("config.json", "graph.json", "summary.json", "nodes/A/prompt_1.md",
                 "nodes/A/worker_1.json", "nodes/A/check_1.txt",
                 "nodes/A/post_merge_check.txt", "nodes/A/diff.patch"):
        assert (result.run_dir / name).is_file(), name
    assert not (result.run_dir / "wt" / "A").exists()
    types = {event["type"] for event in evts}
    assert {"run_start", "node_ready", "node_start", "worker_start", "worker_end",
            "check_start", "check_end", "merge_start", "merge_end",
            "post_merge_check_end", "node_merged", "final_checks_end",
            "run_end"} <= types
    assert summary["final_checks"][0]["ok"] is True


def test_sequential_runs_never_overlap(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, diamond(toy_repo), diamond_commands(), workers=1)
    assert result.summary["status"] == "success"
    evts = events(result)
    spans = sorted((event_t(evts, "node_start", n), event_t(evts, "node_merged", n))
                   for n in "ABCD")
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end <= start


def test_failure_skips_downstream_only(toy_repo, tmp_path):
    result = run(toy_repo, tmp_path, diamond(toy_repo),
                 diamond_commands(b_command=py("import sys; sys.exit(1)")))
    nodes = result.summary["nodes"]
    assert nodes["B"]["status"] == "failed" and nodes["B"]["reason"] == "worker_error"
    assert nodes["B"]["attempts"] == 2
    assert nodes["B"]["worker_reasons"] == ["worker_error", "worker_error"]
    assert nodes["D"]["status"] == "skipped" and nodes["D"]["reason"] == "upstream_failed"
    assert nodes["C"]["status"] == "merged" and nodes["A"]["status"] == "merged"
    assert result.summary["status"] == "partial"
    assert any(e["type"] == "node_skipped" and e["node"] == "D" for e in events(result))


def test_retry_succeeds_and_prompt_carries_check_output(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("R", create=("r.txt",), check=py(
        "import pathlib, sys; t = pathlib.Path('r.txt').read_text(); "
        "print('CHECK-SAW', t); sys.exit(0 if t == 'good' else 1)"))])
    command = py("import os, pathlib; pathlib.Path('r.txt').write_text("
                 "'good' if os.environ['TG_ATTEMPT'] == '2' else 'bad')")
    result = run(toy_repo, tmp_path, graph, {"R": command})
    record = result.summary["nodes"]["R"]
    assert record["status"] == "merged" and record["attempts"] == 2
    assert record["worker_reasons"] == ["", ""]
    prompt = (result.run_dir / "nodes" / "R" / "prompt_2.md").read_text(encoding="utf-8")
    assert "check_failed" in prompt and "CHECK-SAW bad" in prompt
    assert "CHECK-SAW bad" not in (result.run_dir / "nodes/R/prompt_1.md").read_text(
        encoding="utf-8")
    assert integration_subjects(result) == ["[taskgraph] R: R"]


def test_worker_timeout_with_finished_work_is_checked_and_merged(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("T", create=("t.txt",))])
    command = py("import pathlib, time; pathlib.Path('t.txt').write_text('T'); "
                 "time.sleep(30)")
    result = run(toy_repo, tmp_path, graph, {"T": command}, worker_timeout_s=1)
    record = result.summary["nodes"]["T"]
    assert record["status"] == "merged", record
    assert record["attempts"] == 1 and record["worker_reasons"] == ["worker_timeout"]
    assert integration_subjects(result) == ["[taskgraph] T: T"]


def test_worker_timeout_without_changes_fails(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("T", create=("t.txt",))])
    result = run(toy_repo, tmp_path, graph, {"T": py("import time; time.sleep(30)")},
                 worker_timeout_s=1, max_attempts=1)
    record = result.summary["nodes"]["T"]
    assert record["status"] == "failed" and record["reason"] == "worker_timeout"
    assert record["worker_reasons"] == ["worker_timeout"]


def test_no_changes_fails(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("N", create=("n.txt",))])
    result = run(toy_repo, tmp_path, graph, {"N": py("print('nothing')")})
    record = result.summary["nodes"]["N"]
    assert record["status"] == "failed" and record["reason"] == "no_changes"
    assert record["attempts"] == 2
    assert result.summary["status"] == "failed"


def test_out_of_scope_files_are_recorded_but_merged(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("O", create=("o.txt",))])
    command = py("import pathlib; pathlib.Path('o.txt').write_text('O'); "
                 "pathlib.Path('README.md').write_text('changed')")
    result = run(toy_repo, tmp_path, graph, {"O": command})
    record = result.summary["nodes"]["O"]
    assert record["status"] == "merged"
    assert record["changed_files"] == ["README.md", "o.txt"]
    assert record["out_of_scope_files"] == ["README.md"]


def test_merge_conflict_fails_later_node_and_keeps_integration_clean(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("X", create=("x.txt",)), node("Y", create=("y.txt",))])
    commands = {
        "X": py("import pathlib; pathlib.Path('x.txt').write_text('X'); "
                "pathlib.Path('README.md').write_text('from X')"),
        "Y": py("import pathlib, time; time.sleep(0.5); pathlib.Path('y.txt').write_text('Y'); "
                "pathlib.Path('README.md').write_text('from Y')"),
    }
    result = run(toy_repo, tmp_path, graph, commands, workers=2)
    nodes = result.summary["nodes"]
    assert nodes["X"]["status"] == "merged"
    assert nodes["Y"]["status"] == "failed" and nodes["Y"]["reason"] == "merge_conflict"
    repo = result.run_dir / "repo"
    assert gitops.is_clean(repo)
    assert integration_subjects(result) == ["[taskgraph] X: X"]


def test_post_merge_check_failure_is_undone(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [node("P", create=("p.txt",), check=py(
        "import pathlib, sys; sys.exit(0 if pathlib.Path('.task_outputs/flag').exists() "
        "else 1)"))])
    command = py("import pathlib; pathlib.Path('p.txt').write_text('P'); "
                 "pathlib.Path('.task_outputs').mkdir(); "
                 "pathlib.Path('.task_outputs/flag').write_text('x')")
    result = run(toy_repo, tmp_path, graph, {"P": command})
    record = result.summary["nodes"]["P"]
    assert record["status"] == "failed" and record["reason"] == "post_merge_check_failed"
    assert record["attempts"] == 1
    assert integration_subjects(result) == []
    assert gitops.is_clean(result.run_dir / "repo")


def slow_post_merge_scenario(toy_repo, tmp_path, post_merge_ok: bool):
    """X's post-merge check takes 1.5 s; F fails fast and frees a slot for Y.

    X's worker leaves a marker in an excluded directory, so its check is fast
    in the worktree and slow (and, if requested, failing) after the merge.
    """
    exit_code = "0" if post_merge_ok else "0 if here else 1"
    x_check = py("import pathlib, sys, time; here = pathlib.Path('.task_outputs/here')"
                 f".exists(); time.sleep(0 if here else 1.5); sys.exit({exit_code})")
    graph = graph_for(toy_repo, [node("X", create=("x.txt",), check=x_check),
                                 node("F", create=("f.txt",)),
                                 node("Y", create=("y.txt",))])
    commands = {
        "X": py("import pathlib; pathlib.Path('x.txt').write_text('X'); "
                "pathlib.Path('.task_outputs').mkdir(); "
                "pathlib.Path('.task_outputs/here').write_text('1')"),
        "F": py("import sys, time; time.sleep(0.2); sys.exit(1)"),
        "Y": write("y.txt", "Y"),
    }
    result = run(toy_repo, tmp_path, graph, commands, workers=2)
    evts = events(result)
    # Y got F's slot while X's post-merge check was still running.
    assert event_t(evts, "worker_start", "Y") < event_t(evts, "post_merge_check_end", "X")
    assert event_t(evts, "worker_start", "Y") > event_t(evts, "merge_end", "X")
    return result


def test_slow_post_merge_check_does_not_block_other_nodes(toy_repo, tmp_path):
    result = slow_post_merge_scenario(toy_repo, tmp_path, post_merge_ok=True)
    nodes = result.summary["nodes"]
    assert nodes["X"]["status"] == "merged" and nodes["Y"]["status"] == "merged"
    repo = result.run_dir / "repo"
    assert (repo / "x.txt").is_file() and (repo / "y.txt").is_file()


def test_undone_merge_does_not_leak_into_a_node_started_during_its_check(
        toy_repo, tmp_path):
    result = slow_post_merge_scenario(toy_repo, tmp_path, post_merge_ok=False)
    nodes = result.summary["nodes"]
    assert nodes["X"]["status"] == "failed"
    assert nodes["X"]["reason"] == "post_merge_check_failed"
    assert nodes["Y"]["status"] == "merged" and nodes["Y"]["changed_files"] == ["y.txt"]
    repo = result.run_dir / "repo"
    assert not (repo / "x.txt").exists() and (repo / "y.txt").is_file()
    assert integration_subjects(result) == ["[taskgraph] Y: Y"]


def test_invalid_graph_is_not_executed(toy_repo, tmp_path, capsys):
    graph = graph_for(toy_repo, [node("C", modify=("runner.py",)),
                                 node("D", modify=("runner.py",))])
    with pytest.raises(GraphInvalid):
        run(toy_repo, tmp_path, graph, {})
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text("{}", encoding="utf-8")
    code = main(["run", str(graph_path), "--repo", str(toy_repo.path), "--worker",
                 "command", "--command-map", str(command_map), "--out",
                 str(tmp_path / "cli_runs")])
    assert code == 1
    assert "[V5] C, D" in capsys.readouterr().out
    assert not (tmp_path / "cli_runs").exists()


def test_hidden_tests_are_counted_and_never_visible_to_workers(toy_repo, tmp_path):
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text(
        "from runner import MAX_ATTEMPTS\n\n"
        "def test_ok():\n    assert MAX_ATTEMPTS == 3\n\n"
        "def test_also_ok():\n    assert True\n\n"
        "def test_fails():\n    assert False\n", encoding="utf-8")
    graph = graph_for(toy_repo, [node("H", create=("seen.txt",), check=has("seen.txt", "False"))])
    command = py("import pathlib; pathlib.Path('seen.txt').write_text("
                 "str(pathlib.Path('_hidden_tests').exists()))")
    result = run(toy_repo, tmp_path, graph, {"H": command}, hidden_tests=hidden)
    assert result.summary["nodes"]["H"]["status"] == "merged"
    stats = result.summary["hidden_tests"]
    assert (stats["passed"], stats["failed"], stats["errors"], stats["skipped"]) == (2, 1, 0, 0)
    assert (result.run_dir / "hidden_tests.xml").is_file()
    assert any(e["type"] == "hidden_tests_end" for e in events(result))
    assert result.summary["status"] == "success"


def test_cli_run_with_command_worker(toy_repo, tmp_path, capsys):
    graph = graph_for(toy_repo, [node("A", create=("a.txt",))])
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps({"A": write("a.txt", "A")}), encoding="utf-8")
    code = main(["run", str(graph_path), "--repo", str(toy_repo.path), "--worker",
                 "command", "--command-map", str(command_map), "--out",
                 str(tmp_path / "cli_runs"), "--workers", "1"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "A: merged attempts=1" in out and "status: success" in out
    run_dirs = list((tmp_path / "cli_runs").iterdir())
    assert len(run_dirs) == 1
    config = json.loads((run_dirs[0] / "config.json").read_text(encoding="utf-8"))
    assert config["workers"] == 1 and config["worker"] == {"worker": "command"}
