"""Create the deterministic git repository of the click config task.

    python experiments/taskgraph/click_config/make_repo.py <dest> [--with-reference]

The base repository is the one of the click batch task: this calls
``make_repo()`` of ``../click_batch/make_repo.py`` (same vendor snapshot,
added files, author, date and message), so the commit hash is the same,
``bd8caad9078c90c90f38d096fb63650adf1213fd``, and it prints it.
``--with-reference`` then applies ``reference/config.patch`` (not the click
batch patches) to the working tree without committing it.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLICK_BATCH = HERE.parent / "click_batch"
BASE_COMMIT = "bd8caad9078c90c90f38d096fb63650adf1213fd"


def _load_click_batch():
    """``click_batch/make_repo.py`` as a module (its directory is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "click_batch_make_repo", CLICK_BATCH / "make_repo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


click_batch = _load_click_batch()


def reference_patches() -> list[Path]:
    """The reference patches, in the order they are applied."""
    return sorted((HERE / "reference").glob("*.patch"))


def make_repo(dest: Path, with_reference: bool = False) -> str:
    """Create the repository and return its commit hash."""
    dest = Path(dest)
    commit = click_batch.make_repo(dest, with_reference=False)
    if with_reference:
        for patch in reference_patches():
            click_batch.git(dest, "apply", "--whitespace=nowarn", "-",
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
