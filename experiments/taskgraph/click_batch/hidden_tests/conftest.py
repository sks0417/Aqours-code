"""Hidden tests of the click batch task.

``test_t<N>_*.py`` holds the tests of ticket N of ``request.md``; they check
only behaviour the request states, through click's public interface.
``regression/`` is an unchanged copy of click's own tests at the snapshot
commit, so a public test that was edited to pass is still run as it was.

The package is not installed: ``src`` of the repository these tests are copied
into (as ``_hidden_tests/``) is put on ``sys.path`` and ``PYTHONPATH``.

The tests of ``regression/`` that are listed in ``sandbox_skips.txt`` (as
``tests/...`` node ids) are skipped: they need a terminal pager, which the
evaluation sandbox does not have. The list here is this directory's own copy,
so a change to the repository's list does not reach the hidden tests.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SKIP_REASON = "needs a terminal pager; not available in the evaluation sandbox"
SRC = str(HERE.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
# Tests that start a Python subprocess import click there too.
if SRC not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (SRC, os.environ.get("PYTHONPATH")) if part)


def sandbox_skips() -> set:
    """The node ids in ``sandbox_skips.txt`` (one per line; ``#`` starts a comment)."""
    lines = (HERE / "sandbox_skips.txt").read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def pytest_collection_modifyitems(config, items):
    skips = sandbox_skips()
    for item in items:
        try:
            path = item.path.relative_to(HERE / "regression")
        except ValueError:
            continue  # a ticket test
        name = item.nodeid.partition("::")[2]
        if f"tests/{path.as_posix()}::{name}" in skips:
            item.add_marker(pytest.mark.skip(reason=SKIP_REASON))


@pytest.fixture
def runner():
    from click.testing import CliRunner

    return CliRunner()
