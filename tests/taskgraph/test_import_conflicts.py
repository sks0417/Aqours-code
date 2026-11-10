"""The Coordinator resolves conflicts that touch only imports without the worker.

x and y edit app.py in parallel (``edit_set.shared``): each adds a name to
its import line and fills in its own function. The functions merge cleanly;
the import line conflicts when the second node merges, as the import lines
of jobrunner/runner.py did in the jp-modular v3 run. No model is called
(``CommandWorker``); a worker round is visible as attempt 2.
"""
from __future__ import annotations

import json
import sys
import time

from aqours_code.taskgraph import gitops
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import Coordinator, RunOptions, run_graph
from aqours_code.taskgraph.workers import CommandWorker
from taskgraph_support import commit_files, make_graph, make_node

APP = '''\
from collections import OrderedDict

from models import Job


def x_feature() -> str:
    return "x: todo"


def y_feature() -> str:
    return "y: todo"


# registrations
'''
SIDES = {
    "x": ("from collections import Counter, OrderedDict", "str(Counter('aab')['a'])", "2"),
    "y": ("from collections import OrderedDict, deque", "str(len(deque([1, 2, 3])))", "3"),
}
MERGED_IMPORTS = "from collections import Counter, OrderedDict, deque\n\nfrom models import Job\n"

# Attempt 1 does the node's work; a later attempt is a conflict round or a
# merge repair: it saves the file it was given, writes ``files`` and drops
# the conflict markers (keeping both sides).
WORKER = """\
import os, pathlib, time
app = pathlib.Path('app.py')
if os.environ['TG_ATTEMPT'] == '1':
    time.sleep({sleep})
    end = time.time() + 60
    while {wait_for!r} and time.time() < end and (
            {wait_for!r} not in pathlib.Path('../../repo/app.py').read_text()):
        time.sleep(0.05)
    text = app.read_bytes().decode()
    text = text.replace('from collections import OrderedDict', {import_line!r})
    text = text.replace('return "{name}: todo"', 'return ' + {body!r})
    app.write_bytes((text + {tail!r}).encode())
else:
    pathlib.Path({seen!r}).write_bytes(app.read_bytes())
    for name, content in {files!r}.items():
        pathlib.Path(name).write_text(content)
    app.write_bytes(b''.join(line for line in app.read_bytes().splitlines(True)
                             if not line.startswith((b'<<<<<<<', b'|||||||', b'=======',
                                                     b'>>>>>>>'))))
"""
OK = f'"{sys.executable}" -c "print(0)"'


def py(code: str) -> str:
    """A cross-platform shell command running Python ``code`` (no double quotes)."""
    assert '"' not in code
    return f'"{sys.executable}" -c "{code}"'


def worker(tmp_path, name: str, sleep: float = 0, wait_for: str = "", tail: str = "",
           files: dict | None = None) -> str:
    import_line, body, _ = SIDES[name]
    code = WORKER.format(sleep=sleep, wait_for=wait_for, import_line=import_line, name=name,
                         body=body, tail=tail, seen=str(seen(tmp_path, name)),
                         files=files or {})
    path = tmp_path / f"worker_{name}.py"
    path.write_text(code, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def seen(tmp_path, name: str):
    """Where a node's later attempt saves the app.py it was given."""
    return tmp_path / f"seen_{name}.txt"


def app_node(name: str, check: str | None = None, create: tuple = ()) -> dict:
    expected = SIDES[name][2]
    data = make_node(name, modify=("app.py",), create=create, commands=(check or py(
        f"import app, sys; sys.exit(0 if app.{name}_feature() == '{expected}' else 1)"),))
    data["edit_set"]["shared"] = ["app.py"]
    return data


def graph_for(toy_repo, nodes):
    base = commit_files(toy_repo.path, {"app.py": APP}, "add app.py")
    graph = make_graph(nodes, final_checks=(OK,))
    return graph.model_copy(update={"base_commit": base})


def run(toy_repo, tmp_path, graph, commands, **options):
    return run_graph(graph, toy_repo.path, CommandWorker(commands),
                     RunOptions(out_dir=tmp_path / "runs", workers=2, **options))


def events(result, node: str | None = None) -> list[dict]:
    lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    found = [json.loads(line) for line in lines]
    return [event for event in found if node is None or event.get("node") == node]


def kinds(result, node: str) -> list[tuple]:
    return [(event["type"], event.get("ok")) for event in events(result, node)]


def subjects(result) -> list[str]:
    return gitops.commit_subjects(result.run_dir / "repo",
                                  result.summary["config"]["base_commit"])


def final_app(result) -> str:
    return (result.run_dir / "repo" / "app.py").read_bytes().decode()


def x_then_y(tmp_path, **y_options) -> dict:
    """x merges first; y does its work once x's import line is on the integration branch."""
    return {"x": worker(tmp_path, "x"),
            "y": worker(tmp_path, "y", wait_for="Counter", **y_options)}


def test_an_import_only_conflict_is_merged_without_the_worker(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [app_node("x"), app_node("y")])
    result = run(toy_repo, tmp_path, graph, x_then_y(tmp_path))
    summary = result.summary
    assert summary["status"] == "success", summary["nodes"]
    y = summary["nodes"]["y"]
    assert (y["status"], y["attempts"], y["worker_reasons"]) == ("merged", 1, [""])
    assert (y["merge_conflicts"], y["conflicts_auto_resolved"], y["conflict_resolved"]) == (
        1, 1, True)
    assert summary["nodes"]["x"]["conflicts_auto_resolved"] == 0
    assert (summary["merge_conflicts_total"], summary["conflicts_auto_resolved_total"],
            summary["conflicts_resolved_total"]) == (1, 1, 1)
    assert summary["config"]["auto_resolve_imports"] is True
    assert kinds(result, "y") == [
        ("node_ready", None), ("node_start", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None), ("check_end", True),
        ("merge_start", None), ("merge_end", False), ("merge_conflict", None),
        ("merge_conflict_auto_resolved", None), ("check_start", None), ("check_end", True),
        ("merge_conflict_resolved", True),
        ("merge_start", None), ("merge_end", True), ("post_merge_check_end", True),
        ("node_merged", None)]
    auto = next(e for e in events(result, "y") if e["type"] == "merge_conflict_auto_resolved")
    assert (auto["files"], auto["hunks"], auto["files_left"]) == (["app.py"], 1, [])
    checks = [e for e in events(result, "y") if e["type"] == "check_end"]
    assert [(e["attempt"], e.get("auto_resolved")) for e in checks] == [(1, None), (1, True)]
    log = result.run_dir / "nodes" / "y"
    assert (log / "check_auto_resolved_1.txt").is_file()
    assert not (log / "prompt_2.md").exists() and not seen(tmp_path, "y").exists()
    final = final_app(result)
    assert final.startswith(MERGED_IMPORTS) and "<<<<<<<" not in final
    assert "return str(Counter('aab')['a'])" in final and "return str(len(deque([1, 2, 3])))" in final
    assert subjects(result) == ["[taskgraph] x: x", "[taskgraph] y: y"]
    assert y["changed_files"] == ["app.py"]


def test_a_conflict_that_also_touches_code_goes_to_the_worker(toy_repo, tmp_path):
    # both also append a line to the end of app.py: a second hunk, in code
    graph = graph_for(toy_repo, [app_node("x"), app_node("y")])
    commands = {"x": worker(tmp_path, "x", tail="X_DONE = True\n"),
                "y": worker(tmp_path, "y", wait_for="Counter", tail="Y_DONE = True\n")}
    result = run(toy_repo, tmp_path, graph, commands)
    y = result.summary["nodes"]["y"]
    assert result.summary["status"] == "success", result.summary["nodes"]
    assert (y["attempts"], y["merge_conflicts"], y["conflicts_auto_resolved"],
            y["conflict_resolved"]) == (2, 1, 0, True)
    auto = next(e for e in events(result, "y") if e["type"] == "merge_conflict_auto_resolved")
    assert (auto["files"], auto["hunks"], auto["files_left"]) == (["app.py"], 1, ["app.py"])
    assert "check_auto_resolved_1.txt" not in {p.name for p in (result.run_dir / "nodes" / "y").iterdir()}
    prompt = (result.run_dir / "nodes" / "y" / "prompt_2.md").read_text(encoding="utf-8")
    assert "conflict markers (<<<<<<<, =======, >>>>>>>): app.py" in prompt
    # the worker got the imports merged and the code hunk as git wrote it
    given = seen(tmp_path, "y").read_bytes().decode()
    assert given.startswith(MERGED_IMPORTS)
    assert given.count("<<<<<<<") == 1 and "|||||||" not in given
    assert "<<<<<<< HEAD\nY_DONE = True\n=======\nX_DONE = True\n>>>>>>> " in given
    final = final_app(result)
    assert final.startswith(MERGED_IMPORTS) and "X_DONE" in final and "Y_DONE" in final


def test_without_the_option_every_conflict_goes_to_the_worker(toy_repo, tmp_path):
    graph = graph_for(toy_repo, [app_node("x"), app_node("y")])
    result = run(toy_repo, tmp_path, graph, x_then_y(tmp_path), auto_resolve_imports=False)
    summary = result.summary
    assert summary["status"] == "success", summary["nodes"]
    y = summary["nodes"]["y"]
    assert (y["attempts"], y["merge_conflicts"], y["conflicts_auto_resolved"],
            y["conflict_resolved"]) == (2, 1, 0, True)
    assert summary["conflicts_auto_resolved_total"] == 0
    assert summary["config"]["auto_resolve_imports"] is False
    assert not any(e["type"].startswith("merge_conflict_auto") for e in events(result))
    # the flow before this option: the conflict round is the worker's second attempt
    assert kinds(result, "y") == [
        ("node_ready", None), ("node_start", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None), ("check_end", True),
        ("merge_start", None), ("merge_end", False), ("merge_conflict", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None), ("check_end", True),
        ("merge_conflict_resolved", True),
        ("merge_start", None), ("merge_end", True), ("post_merge_check_end", True),
        ("node_merged", None)]
    given = seen(tmp_path, "y").read_bytes().decode()
    assert given.startswith("<<<<<<< HEAD\nfrom collections import OrderedDict, deque\n=======\n"
                            "from collections import Counter, OrderedDict\n>>>>>>> ")
    assert "|||||||" not in given


def test_a_failing_check_after_the_program_s_resolution_goes_to_the_worker(toy_repo,
                                                                          tmp_path):
    # y's check rejects x's Counter unless y has adapted to it, which only the
    # worker's conflict round does
    check = py("import app, os, pathlib, sys; text = pathlib.Path('app.py').read_text(); "
               "ok = app.y_feature() == '3' and ('Counter' not in text "
               "or os.path.exists('y_adapted.txt')); sys.exit(0 if ok else 1)")
    graph = graph_for(toy_repo, [app_node("x"), app_node("y", check, create=("y_adapted.txt",))])
    result = run(toy_repo, tmp_path, graph,
                 x_then_y(tmp_path, files={"y_adapted.txt": "adapted"}))
    y = result.summary["nodes"]["y"]
    assert result.summary["status"] == "success", result.summary["nodes"]
    assert (y["attempts"], y["merge_conflicts"], y["conflicts_auto_resolved"],
            y["conflict_resolved"], y["reason"]) == (2, 1, 0, True, "")
    assert kinds(result, "y")[8:] == [
        ("merge_conflict", None), ("merge_conflict_auto_resolved", None),
        ("check_start", None), ("check_end", False), ("merge_conflict_auto_undone", None),
        ("worker_start", None), ("worker_end", True), ("check_start", None), ("check_end", True),
        ("merge_conflict_resolved", True),
        ("merge_start", None), ("merge_end", True), ("post_merge_check_end", True),
        ("node_merged", None)]
    undone = next(e for e in events(result, "y") if e["type"] == "merge_conflict_auto_undone")
    assert (undone["files"], undone["reason"]) == (["app.py"], "check_failed")
    # the worker saw the conflict exactly as without the program
    given = seen(tmp_path, "y").read_bytes().decode()
    assert given.startswith("<<<<<<< HEAD\nfrom collections import OrderedDict, deque\n=======\n")
    prompt = (result.run_dir / "nodes" / "y" / "prompt_2.md").read_text(encoding="utf-8")
    assert "conflict markers (<<<<<<<, =======, >>>>>>>): app.py" in prompt
    log = result.run_dir / "nodes" / "y"
    assert (log / "check_auto_resolved_1.txt").is_file() and (log / "check_2.txt").is_file()


def test_a_merge_repair_gets_no_conflict_the_program_resolved(toy_repo, tmp_path, monkeypatch):
    # x's post-merge check needs xfix.txt, which only its repair writes; y
    # merges while x waits to repair, so the repair's merge conflicts in imports
    real = Coordinator._repair

    def after_y(self, node, *args, **kwargs):
        deadline = time.monotonic() + 60
        while (node.id == "x" and self.records["y"].status != "merged"
               and time.monotonic() < deadline):
            time.sleep(0.05)
        return real(self, node, *args, **kwargs)

    monkeypatch.setattr(Coordinator, "_repair", after_y)
    x = app_node("x", create=("xfix.txt",))
    x["check"]["post_merge_commands"] = [py(
        "import pathlib, sys; sys.exit(0 if pathlib.Path('xfix.txt').exists() else 1)")]
    graph = graph_for(toy_repo, [x, app_node("y")])
    commands = {"x": worker(tmp_path, "x", files={"xfix.txt": "fixed"}),
                "y": worker(tmp_path, "y", sleep=1)}
    result = run(toy_repo, tmp_path, graph, commands)
    record = result.summary["nodes"]["x"]
    assert record["status"] == "merged", record
    assert (record["merge_conflicts"], record["merge_repairs"], record["conflicts_auto_resolved"],
            record["conflict_resolved"], record["attempts"]) == (1, 1, 1, True, 2)
    auto = next(e for e in events(result, "x") if e["type"] == "merge_conflict_auto_resolved")
    assert (auto["hunks"], auto["files_left"]) == (1, [])
    prompt = (result.run_dir / "nodes" / "x" / "prompt_2.md").read_text(encoding="utf-8")
    assert "# Your work broke after merging" in prompt
    assert "# Your work conflicts with other nodes' work" not in prompt
    assert seen(tmp_path, "x").read_bytes().decode().startswith(MERGED_IMPORTS)
    assert final_app(result).startswith(MERGED_IMPORTS)
    assert subjects(result) == ["[taskgraph] y: y", "[taskgraph] x: x"]


def test_run_command_turns_the_resolution_off(toy_repo, tmp_path, capsys):
    graph = graph_for(toy_repo, [app_node("x"), app_node("y")])
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(graph.model_dump_json(by_alias=True), encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps(x_then_y(tmp_path)), encoding="utf-8")
    base = ["run", str(graph_path), "--repo", str(toy_repo.path), "--worker", "command",
            "--command-map", str(command_map), "--sandbox", "none"]
    assert main([*base, "--out", str(tmp_path / "on")]) == 0
    output = capsys.readouterr().out
    assert "merge conflicts: 1 (y; resolved: y)  resolved by the program: 1" in output
    assert main([*base, "--out", str(tmp_path / "off"), "--no-auto-resolve-imports"]) == 0
    output = capsys.readouterr().out
    assert "merge conflicts: 1 (y; resolved: y)  resolved by the program: 0" in output
    configs = [json.loads(next((tmp_path / where).iterdir()).joinpath("config.json").read_text(
        encoding="utf-8")) for where in ("on", "off")]
    assert [config["auto_resolve_imports"] for config in configs] == [True, False]
