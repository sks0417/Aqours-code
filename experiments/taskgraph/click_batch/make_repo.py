"""Create the deterministic git repository of the click batch task.

    python experiments/taskgraph/click_batch/make_repo.py <dest> [--with-reference]

Copies ``vendor/click/`` to ``<dest>`` with LF line endings and adds what an
installation would provide (the package is not installed): a root
``conftest.py`` that puts ``src`` on ``sys.path`` and ``PYTHONPATH``, and
``src/click.egg-info/PKG-INFO`` for ``importlib.metadata.version("click")``.
It commits once with a fixed author, date, and message, and prints the commit
hash, which is the same on every platform. ``--with-reference`` then applies
the patches in ``reference/`` to the working tree without committing them.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMIT_MESSAGE = "click batch base"
ROOT_CONFTEST = '''\
"""Make ``import click`` use ``src/click`` without installing the package."""
import os
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent / "src")
sys.path.insert(0, SRC)
# Tests that start a Python subprocess import click there too.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (SRC, os.environ.get("PYTHONPATH")) if part)
'''
# click's own tests compare ``click.__version__`` with the installed metadata.
PKG_INFO = "Metadata-Version: 2.1\nName: click\nVersion: 8.6.0.dev\n"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Click Batch Task",
    "GIT_AUTHOR_EMAIL": "task@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Click Batch Task",
    "GIT_COMMITTER_EMAIL": "task@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    """Run git in ``repo`` with a configuration independent of the user's."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", *args],
        cwd=repo, env={**os.environ, **GIT_ENV}, capture_output=True, input=stdin,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.decode("utf-8").strip()


def copy_tree(source: Path, dest: Path) -> None:
    """Copy every file under ``source`` to ``dest`` with LF line endings."""
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def reference_patches() -> list[Path]:
    """The reference patches, in the order they are applied."""
    return sorted((HERE / "reference").glob("*.patch"))


def make_repo(dest: Path, with_reference: bool = False) -> str:
    """Create the repository and return its commit hash."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    copy_tree(HERE / "vendor" / "click", dest)
    (dest / "conftest.py").write_bytes(ROOT_CONFTEST.encode("utf-8"))
    (dest / "src" / "click.egg-info").mkdir()
    (dest / "src" / "click.egg-info" / "PKG-INFO").write_bytes(PKG_INFO.encode("utf-8"))
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", COMMIT_MESSAGE)
    commit = git(dest, "rev-parse", "HEAD")
    if with_reference:
        for patch in reference_patches():
            git(dest, "apply", "--whitespace=nowarn", "-",
                stdin=patch.read_bytes().replace(b"\r\n", b"\n"))
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest")
    parser.add_argument("--with-reference", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(make_repo(Path(args.dest), args.with_reference))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
