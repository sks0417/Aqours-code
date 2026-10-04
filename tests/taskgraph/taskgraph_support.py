"""Helpers for task graph tests: the toy git repository and graph builders."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aqours_code.taskgraph import Graph
from aqours_code.taskgraph.schema import EXAMPLE_GRAPH_PATH as EXAMPLE_GRAPH

TOY_FILES: dict[str, str] = {
    "models.py": '''\
from dataclasses import dataclass
from enum import Enum


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0


class Config:
    class Limits:
        max_jobs: int = 10
''',
    "store.py": '''\
from models import Job, JobStatus


class JobStore:
    """In-memory job store."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.version = 0

    def add(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.version += 1

    def list_unfinished(self) -> list[Job]:
        return [job for job in self.jobs.values() if job.status is not JobStatus.DONE]
''',
    "runner.py": '''\
from store import JobStore

MAX_ATTEMPTS = 3


def run_loop(store: JobStore) -> int:
    count = 0
    for job in store.list_unfinished():
        job.attempts += 1
        count += 1
    return count
''',
    "tests/test_basic.py": '''\
from models import Job
from runner import run_loop
from store import JobStore


def test_run_loop_counts_unfinished_jobs():
    store = JobStore()
    store.add(Job(id="a"))
    assert run_loop(store) == 1
''',
    "README.md": "# Toy job queue\n",
    "broken.py": "def broken(:\n    pass\n",
}

# Fixed identity and timestamps make the toy commit hash reproducible.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Toy",
    "GIT_AUTHOR_EMAIL": "toy@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Toy",
    "GIT_COMMITTER_EMAIL": "toy@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@dataclass
class ToyRepo:
    """A toy repository and the commit created by the fixture."""

    path: Path
    commit: str


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with an isolated configuration."""
    env = {**os.environ, **_GIT_ENV, "GIT_CONFIG_GLOBAL": str(repo / ".." / "gitconfig")}
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", *args],
        cwd=repo, env=env, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    """Write ``files`` into ``repo``, commit them, and return the new commit."""
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_node(node_id: str, *, kind: str = "implement", modify: tuple = (),
              create: tuple = (), requires: tuple = (), requires_impl: tuple = (),
              provides: tuple = (),
              commands: tuple = ("python -m pytest -q",), size: str | None = None,
              context_files: tuple = (), symbols: tuple = ()) -> dict:
    """Return a node dictionary with sensible defaults for tests."""
    node = {
        "id": node_id,
        "title": node_id,
        "kind": kind,
        "goal": f"do {node_id}",
        "edit_set": {"modify": list(modify), "create": list(create),
                     "symbols": list(symbols)},
        "requires": list(requires),
        "requires_impl": list(requires_impl),
        "provides": list(provides),
        "check": {"commands": list(commands)},
        "context_files": list(context_files),
    }
    if size is not None:
        node["size"] = size
    return node


def make_edge(source: str, target: str, edge_type: str = "full",
              origin: str = "manual") -> dict:
    """Return an edge dictionary."""
    return {"from": source, "to": target, "type": edge_type, "source": origin,
            "reason": "test"}


def make_graph(nodes: list[dict], edges: list[dict] | None = None,
               final_checks: tuple = ("python -m pytest -q",)):
    """Build a validated :class:`aqours_code.taskgraph.Graph` from node and edge dicts."""
    return Graph.model_validate({
        "request_id": "test",
        "request": "test request",
        "repo": "toy",
        "base_commit": "HEAD",
        "final_checks": list(final_checks),
        "generator": {"kind": "manual"},
        "nodes": nodes,
        "edges": edges or [],
    })
