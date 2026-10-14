"""Which existing tests a change affects: the imports of the test files, read with ``ast``.

Planner v3 checks a node with its own tests plus the existing tests that
import a module the node changes, instead of the whole suite every time. A
test file "imports" a repository file when one of its ``import`` statements
(at any depth of the file, also inside functions) names the file's module or
a package above it. Module names are mapped to files from three roots: the
repository root (a top-level package), ``src/`` (the ``src`` layout), and
the test file's own directory (pytest puts it on ``sys.path``). Relative
imports are resolved from the test file's package. Only direct imports
count: ``import click`` reaches ``src/click/__init__.py``, not every module
that ``__init__.py`` imports in turn.

A ``conftest.py`` affects every test file in its directory and below.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .compare import is_test_path
from .repo_index import RepoIndex

SOURCE_ROOTS = ("", "src/")


def is_test_file(path: str) -> bool:
    """A pytest file under ``tests/``: ``test_*.py`` or ``*_test.py``."""
    name = PurePosixPath(path).name
    return (is_test_path(path) and name.endswith(".py")
            and (name.startswith("test_") or name.endswith("_test.py")))


@dataclass
class TestImports:
    """For every existing test file, the repository files it imports."""

    __test__ = False  # not a pytest class

    imports: dict[str, set[str]] = field(default_factory=dict)
    conftests: list[str] = field(default_factory=list)
    # Test files whose imports could not be read (they do not parse).
    unresolved: list[str] = field(default_factory=list)

    def affected(self, changed: Iterable[str]) -> list[str]:
        """Existing test files that import, are, or sit under a changed file's conftest."""
        changed = set(changed)
        found = []
        for test, imported in self.imports.items():
            if test in changed or imported & changed or any(
                    _under(test, conftest) for conftest in self.conftests
                    if conftest in changed):
                found.append(test)
        return sorted(found)


def _under(test: str, conftest: str) -> bool:
    folder = PurePosixPath(conftest).parent
    return folder == PurePosixPath(".") or PurePosixPath(test).is_relative_to(folder)


def _module_files(name: str, roots: list[str], files: set[str]) -> set[str]:
    """Files of ``name`` and of every package above it, under each root."""
    found = set()
    parts = name.split(".")
    for end in range(1, len(parts) + 1):
        stem = "/".join(parts[:end])
        for root in roots:
            for candidate in (f"{root}{stem}.py", f"{root}{stem}/__init__.py"):
                if candidate in files:
                    found.add(candidate)
    return found


def _relative_base(path: str, level: int) -> str | None:
    """The package directory a relative import of ``level`` dots starts from."""
    folder = PurePosixPath(path).parent
    for _ in range(level - 1):
        if folder == PurePosixPath("."):
            return None
        folder = folder.parent
    return "" if folder == PurePosixPath(".") else f"{folder.as_posix()}/"


def imported_files(path: str, source: str, files: set[str]) -> set[str]:
    """Repository files that ``source`` (the file at ``path``) imports.

    Raises ``SyntaxError`` or ``ValueError`` when the source does not parse.
    """
    tree = ast.parse(source, filename=path)
    own_dir = PurePosixPath(path).parent.as_posix()
    roots = [*SOURCE_ROOTS, f"{own_dir}/" if own_dir != "." else ""]
    roots = list(dict.fromkeys(roots))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _module_files(alias.name, roots, files)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(path, node.level)
                if base is None:
                    continue
                if f"{base}__init__.py" in files:  # the package itself is imported
                    found.add(f"{base}__init__.py")
                search = [base]
                prefix = node.module or ""
            else:
                search = roots
                prefix = node.module or ""
            if prefix:
                found |= _module_files(prefix, search, files)
            for alias in node.names:
                if alias.name != "*":
                    name = f"{prefix}.{alias.name}" if prefix else alias.name
                    found |= _module_files(name, search, files)
    found.discard(path)
    return found


def scan_sources(sources: dict[str, str], files: set[str]) -> TestImports:
    """:class:`TestImports` of the test files in ``sources`` (path -> text)."""
    result = TestImports()
    result.conftests = sorted(path for path in files
                              if PurePosixPath(path).name == "conftest.py")
    for path in sorted(sources):
        if not is_test_file(path):
            continue
        try:
            result.imports[path] = imported_files(path, sources[path], files)
        except (SyntaxError, ValueError):
            result.unresolved.append(path)
    return result


def scan_test_imports(root: Path, index: RepoIndex) -> TestImports:
    """Read every test file of the checkout at ``root`` (indexed by ``index``)."""
    sources = {}
    unreadable = []
    for path in sorted(index.files):
        if not is_test_file(path):
            continue
        try:
            sources[path] = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(path)
    result = scan_sources(sources, index.files)
    result.unresolved = sorted([*result.unresolved, *unreadable])
    return result
