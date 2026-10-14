"""Which existing tests a change affects: the imports of the repository, read with ``ast``.

Planner v3 checks a node with its own tests plus the existing tests that
import, directly or indirectly, a file the node changes, instead of the
whole suite every time.

A file "imports" a repository file when one of its ``import`` statements (at
any depth of the file, also inside functions) names that file's module or a
package above it. Module names are mapped to files from the repository root
(a top-level package) and ``src/`` (the ``src`` layout); a file under
``tests/`` also resolves names from its own directory, which pytest puts on
``sys.path``. Relative imports are resolved from the file's package.

Every Python file of the repository is read this way. A test file is affected
by a change when the files it reaches, following the imports from the test
file through the source modules (``__init__.py`` included, so ``import
click`` reaches every module ``src/click/__init__.py`` imports), contain a
changed file. A ``conftest.py`` counts as imported by every test file in its
directory and below, and a changed test file affects itself. A file whose
imports cannot be read (it does not parse) is listed in ``unresolved``;
which tests a change affects is then unknown.
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
    """The direct imports of every Python file, and what each test file reaches."""

    __test__ = False  # not a pytest class

    # test file -> the repository files it imports directly
    imports: dict[str, set[str]] = field(default_factory=dict)
    # any other Python file (source modules, helpers, conftest.py) -> the same
    sources: dict[str, set[str]] = field(default_factory=dict)
    conftests: list[str] = field(default_factory=list)
    # Files whose imports could not be read (they do not parse, or cannot be read).
    unresolved: list[str] = field(default_factory=list)
    _reached: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)

    def reached(self, test: str) -> set[str]:
        """Every file ``test`` imports, directly or through the files it imports."""
        if test not in self._reached:
            start = set(self.imports.get(test, ())) | {
                conftest for conftest in self.conftests if _under(test, conftest)}
            seen: set[str] = set()
            stack = list(start)
            while stack:  # the seen set stops import cycles
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(self.sources.get(current, ()))
            seen.discard(test)
            self._reached[test] = seen
        return self._reached[test]

    def affected(self, changed: Iterable[str]) -> list[str]:
        """Existing test files that are changed or reach a changed file."""
        changed = set(changed)
        return sorted(test for test in self.imports
                      if test in changed or self.reached(test) & changed)


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

    A file under ``tests/`` also resolves names from its own directory;
    source modules do not (Python 3 has no implicit relative imports, so
    ``import types`` in ``src/click/core.py`` is the standard library).
    Raises ``SyntaxError`` or ``ValueError`` when the source does not parse.
    """
    tree = ast.parse(source, filename=path)
    roots = list(SOURCE_ROOTS)
    own_dir = PurePosixPath(path).parent.as_posix()
    if is_test_path(path):
        roots = list(dict.fromkeys([*roots, f"{own_dir}/" if own_dir != "." else ""]))
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
    """:class:`TestImports` of the Python files in ``sources`` (path -> text).

    A file of ``files`` that is not in ``sources`` counts as importing nothing.
    """
    result = TestImports()
    result.conftests = sorted(path for path in files
                              if PurePosixPath(path).name == "conftest.py")
    for path in sorted(sources):
        if not path.endswith(".py"):
            continue
        try:
            imported = imported_files(path, sources[path], files)
        except (SyntaxError, ValueError):
            result.unresolved.append(path)
            continue
        (result.imports if is_test_file(path) else result.sources)[path] = imported
    return result


def scan_test_imports(root: Path, index: RepoIndex) -> TestImports:
    """Read every Python file of the checkout at ``root`` (indexed by ``index``)."""
    sources = {}
    unreadable = []
    for path in sorted(index.files):
        if not path.endswith(".py"):
            continue
        try:
            sources[path] = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(path)
    result = scan_sources(sources, index.files)
    result.unresolved = sorted([*result.unresolved, *unreadable])
    return result
