"""Hidden tests of the click batch task.

``test_t<N>_*.py`` holds the tests of ticket N of ``request.md``; they check
only behaviour the request states, through click's public interface.
``regression/`` is an unchanged copy of click's own tests at the snapshot
commit, so a public test that was edited to pass is still run as it was.

The package is not installed: ``src`` of the repository these tests are copied
into (as ``_hidden_tests/``) is put on ``sys.path`` and ``PYTHONPATH``.
"""
import os
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
# Tests that start a Python subprocess import click there too.
if SRC not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (SRC, os.environ.get("PYTHONPATH")) if part)


@pytest.fixture
def runner():
    from click.testing import CliRunner

    return CliRunner()
