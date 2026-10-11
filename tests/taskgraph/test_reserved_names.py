"""Files named like a Windows device, and a clean-up that cannot fail the run.

A worker's shell runs in a Linux container, where ``dir /b src 2>nul`` creates
a file named ``nul``. A Windows host cannot commit it or delete it by its plain
path. No model is called (``CommandWorker``).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aqours_code.taskgraph import cleanup, coordinator
from aqours_code.taskgraph.cleanup import (
    extended_path,
    is_reserved_name,
    remove_reserved_files,
    remove_tree,
)
from aqours_code.taskgraph.cli import main
from aqours_code.taskgraph.coordinator import RunOptions, run_graph
from aqours_code.taskgraph.prompting import build_node_prompt
from aqours_code.taskgraph.workers import CommandWorker
from taskgraph_support import make_graph, make_node

RESERVED = ("nul", "sub/NUL.txt", "con", "Aux.tar.gz")
KEPT = ("nul_like.py", "sub/console.txt")
# A worker that leaves reserved-name files next to its real work. On Windows
# such a file can only be created through the \\?\ form of its path.
LITTER = f"""
import os

for relative in {RESERVED + KEPT!r}:
    path = os.path.join(os.getcwd(), *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open("\\\\\\\\?\\\\" + path if os.name == "nt" else path, "w") as handle:
        handle.write("x")
"""
OK = f'"{sys.executable}" -c "print(1)"'
LINUX_SENTENCE = ("Your shell is Linux: use /dev/null, never nul, and Linux commands "
                  "(ls, cat, grep), not Windows ones (dir, type, findstr).")


def write_literal(path: Path, text: str = "x") -> None:
    """Create ``path`` even if its name is a Windows device name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(extended_path(path), "w", encoding="utf-8") as handle:
        handle.write(text)


def names(root: Path) -> list[str]:
    """Every file under ``root`` (``.git`` aside), from directory listings.

    ``Path.exists`` is no use here: on Windows it is true for any ``nul``.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        found += [(Path(dirpath).relative_to(root) / name).as_posix() for name in filenames]
    return sorted(found)


def litter_run(toy_repo, tmp_path):
    script = tmp_path / "litter.py"
    script.write_text(LITTER, encoding="utf-8")
    graph = make_graph([make_node("N", create=KEPT, commands=(OK,))], final_checks=(OK,))
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    worker = CommandWorker({"N": f'"{sys.executable}" "{script}"'})
    return graph, worker


def events(result) -> list[dict]:
    lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("name", ["nul", "NUL", "Nul.txt", "con", "PRN", "aux.tar.gz",
                                  "com1", "COM9.log", "lpt1", "LPT9", "nul .txt"])
def test_device_names_are_reserved(name):
    assert is_reserved_name(name)


@pytest.mark.parametrize("name", ["nul_like.py", "null", "console.txt", "com0", "com10",
                                  "lpt", "auxiliary.py", "a.nul", ".nul", "connect.py"])
def test_other_names_are_not_reserved(name):
    assert not is_reserved_name(name)


def test_remove_reserved_files_searches_every_directory_but_not_git(tmp_path):
    for relative in (*RESERVED, *KEPT, "a/b/c/LPT1", ".git/nul"):
        write_literal(tmp_path / relative)
    assert remove_reserved_files(tmp_path) == ["Aux.tar.gz", "a/b/c/LPT1", "con", "nul",
                                               "sub/NUL.txt"]
    assert names(tmp_path) == ["nul_like.py", "sub/console.txt"]
    assert os.listdir(tmp_path / ".git") == ["nul"]
    assert remove_reserved_files(tmp_path) == []
    remove_tree(tmp_path / ".git")


def test_remove_tree_deletes_a_tree_that_holds_reserved_names(tmp_path):
    root = tmp_path / "wt"
    for relative in (*RESERVED, *KEPT):
        write_literal(root / "node" / relative)
    remove_tree(root)
    assert "wt" not in os.listdir(tmp_path)
    remove_tree(root)  # a missing tree is fine


def test_delete_handler_retries_with_the_literal_path_on_windows(monkeypatch):
    calls = []

    def delete(path):
        calls.append(path)
        if not path.startswith("literal:"):
            raise OSError(1, "Incorrect function")

    monkeypatch.setattr(cleanup.os, "chmod", lambda path, mode: None)
    monkeypatch.setattr(cleanup, "extended_path", lambda path: f"literal:{path}")
    monkeypatch.setattr(cleanup, "WINDOWS", True)
    cleanup._retry_delete(delete, "wt/N/nul", None)
    assert calls == ["wt/N/nul", "literal:wt/N/nul"]
    monkeypatch.setattr(cleanup, "WINDOWS", False)
    with pytest.raises(OSError):
        cleanup._retry_delete(delete, "wt/N/nul", None)


@pytest.mark.skipif(os.name != "nt", reason="the literal path form exists only on Windows")
def test_extended_path_is_absolute_and_keeps_the_device_name(tmp_path, monkeypatch):
    assert extended_path(tmp_path / "nul") == "\\\\?\\" + str(tmp_path) + "\\nul"
    monkeypatch.chdir(tmp_path)
    assert extended_path("sub/nul") == "\\\\?\\" + str(tmp_path) + "\\sub\\nul"
    assert extended_path("\\\\?\\C:\\x\\nul") == "\\\\?\\C:\\x\\nul"
    assert extended_path("\\\\server\\share\\nul") == "\\\\?\\UNC\\server\\share\\nul"


def test_reserved_files_are_removed_before_the_commit(toy_repo, tmp_path):
    graph, worker = litter_run(toy_repo, tmp_path)
    result = run_graph(graph, toy_repo.path, worker, RunOptions(out_dir=tmp_path / "runs"))
    summary = result.summary
    assert summary["status"] == "success"
    record = summary["nodes"]["N"]
    assert (record["status"], record["reason"], record["attempts"]) == ("merged", "", 1)
    assert record["removed_reserved_files"] == ["Aux.tar.gz", "con", "nul", "sub/NUL.txt"]
    assert record["changed_files"] == ["nul_like.py", "sub/console.txt"]
    assert record["out_of_scope_files"] == []
    # the files a task may need are merged; the reserved names are nowhere
    merged = names(result.run_dir / "repo")
    assert set(KEPT) <= set(merged)
    assert not [path for path in merged if is_reserved_name(Path(path).name)]
    log = events(result)
    types = [event["type"] for event in log]
    removed = log[types.index("reserved_files_removed")]
    assert (removed["node"], removed["attempt"]) == ("N", 1)
    assert removed["files"] == record["removed_reserved_files"]
    assert (types.index("worker_end") < types.index("reserved_files_removed")
            < types.index("check_start"))
    # nothing was left for the clean-up to trip over
    assert not (result.run_dir / "wt").exists()
    assert "cleanup_warning" not in types and result.cleanup_warnings == []
    assert types[-1] == "run_end"


def test_a_node_without_reserved_files_records_none(toy_repo, tmp_path):
    graph = make_graph([make_node("N", create=("a.txt",), commands=(OK,))], final_checks=(OK,))
    graph = graph.model_copy(update={"base_commit": toy_repo.commit})
    write = f'"{sys.executable}" -c "open(\'a.txt\', \'w\').write(\'A\')"'
    result = run_graph(graph, toy_repo.path, CommandWorker({"N": write}),
                       RunOptions(out_dir=tmp_path / "runs"))
    assert result.summary["nodes"]["N"]["removed_reserved_files"] == []
    assert "reserved_files_removed" not in [event["type"] for event in events(result)]


def failing_remove_tree(seen: list):
    def fake(path: Path) -> None:
        seen.append((path.name, (path.parents[0] / "summary.json").is_file()
                     or (path.parents[1] / "summary.json").is_file()))
        raise OSError(1, "Incorrect function", str(path))
    return fake


def test_run_finishes_when_the_cleanup_raises(toy_repo, tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(coordinator, "remove_tree", failing_remove_tree(seen))
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_hidden.py").write_text("def test_ok():\n    assert True\n",
                                           encoding="utf-8")
    graph, worker = litter_run(toy_repo, tmp_path)
    result = run_graph(graph, toy_repo.path, worker,
                       RunOptions(out_dir=tmp_path / "runs", hidden_tests=hidden))
    # summary.json was complete before either delete was tried
    assert seen == [("_hidden_tests", True), ("wt", True)]
    on_disk = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert on_disk == result.summary
    assert on_disk["status"] == "success" and on_disk["nodes"]["N"]["status"] == "merged"
    assert on_disk["final_checks"][0]["ok"] is True
    assert on_disk["hidden_tests"]["passed"] == 1
    log = events(result)
    warnings = [event for event in log if event["type"] == "cleanup_warning"]
    assert [Path(event["path"]).name for event in warnings] == ["_hidden_tests", "wt"]
    assert all("Incorrect function" in event["error"] for event in warnings)
    assert log[-1]["type"] == "run_end" and log[-1]["status"] == "success"
    assert len(result.cleanup_warnings) == 2
    assert str(result.run_dir / "wt") in result.cleanup_warnings[1]


def test_run_command_prints_one_line_per_cleanup_warning(toy_repo, tmp_path, monkeypatch,
                                                         capsys):
    monkeypatch.setattr(coordinator, "remove_tree", failing_remove_tree([]))
    graph, worker = litter_run(toy_repo, tmp_path)
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph.model_dump(mode="json", by_alias=True)),
                          encoding="utf-8")
    command_map = tmp_path / "commands.json"
    command_map.write_text(json.dumps(worker.commands), encoding="utf-8")
    code = main(["run", str(graph_path), "--repo", str(toy_repo.path), "--worker", "command",
                 "--command-map", str(command_map), "--out", str(tmp_path / "runs")])
    captured = capsys.readouterr()
    assert code == 0
    assert "N: merged attempts=1" in captured.out and "status: success" in captured.out
    lines = [line for line in captured.err.splitlines()
             if line.startswith("warning: cleanup: could not remove ")]
    assert len(lines) == 2 and "Incorrect function" in lines[0]


def test_docker_prompt_says_the_shell_is_linux(toy_index):
    graph = make_graph([make_node("A", modify=("models.py",))])
    docker = build_node_prompt(graph, graph.nodes[0], toy_index, 1, None, sandbox="docker")
    assert LINUX_SENTENCE in " ".join(docker[docker.index("# Rules"):].split())
    plain = build_node_prompt(graph, graph.nodes[0], toy_index, 1, None)
    assert "/dev/null" not in plain and "findstr" not in plain
