"""Fixtures for task graph tests: a toy git repository and its index."""
from __future__ import annotations

from pathlib import Path

import pytest

from aqours_code.taskgraph import RepoIndex, build_index
from taskgraph_support import TOY_FILES, ToyRepo, commit_files, git


@pytest.fixture
def toy_index(toy_repo: ToyRepo) -> RepoIndex:
    """Index of the toy repository at its fixture commit."""
    return build_index(toy_repo.path, toy_repo.commit)


@pytest.fixture
def toy_repo(tmp_path: Path) -> ToyRepo:
    """Create and commit the toy repository; return its path and commit."""
    repo = tmp_path / "toy"
    repo.mkdir()
    git(repo, "init", "-q")
    commit = commit_files(repo, TOY_FILES, "toy repository")
    return ToyRepo(path=repo, commit=commit)
