"""Create a deterministic git repository for one job runner variant.

    python experiments/taskgraph/job_runner/make_repo.py <coupled|modular> <dest> [--with-reference]

Copies ``<variant>/base/`` to ``<dest>`` with LF line endings, commits it once
with a fixed author, date, and message, and prints the commit hash, which is
the same on every platform. ``--with-reference`` then copies the reference
solution over the working tree without committing it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARIANTS = ("coupled", "modular")
COMMIT_MESSAGE = "job runner base"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Job Runner Task",
    "GIT_AUTHOR_EMAIL": "task@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Job Runner Task",
    "GIT_COMMITTER_EMAIL": "task@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a configuration independent of the user's."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", *args],
        cwd=repo, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def copy_tree(source: Path, dest: Path) -> None:
    """Copy every file under ``source`` to ``dest`` with LF line endings."""
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def make_repo(variant: str, dest: Path, with_reference: bool = False) -> str:
    """Create the repository and return its commit hash."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(VARIANTS)}")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    copy_tree(HERE / variant / "base", dest)
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", COMMIT_MESSAGE)
    commit = git(dest, "rev-parse", "HEAD")
    if with_reference:
        copy_tree(HERE / variant / "reference", dest)
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("variant", choices=VARIANTS)
    parser.add_argument("dest")
    parser.add_argument("--with-reference", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(make_repo(args.variant, Path(args.dest), args.with_reference))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
