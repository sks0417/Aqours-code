"""An outline of a repository's source files, for the planner's Step 2.

The planner used to read source files one by one, in full, to find out what
they contain; every call then carried all of them again. The outline gives it
the same overview at once: every class, function, method and module-level
name of every source file, with signature, line numbers and the first line of
the docstring. It is built with ``ast`` only; no model is called.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .repo_index import RepoIndex

OUTLINE_LIMIT = 60_000  # characters
DOC_CHARS = 100
# What is left out to stay under the limit, in this order; each level includes
# the ones before it.
REDUCTIONS = ("none", "no_docstrings", "no_private", "no_methods", "truncated")
_NOTES = {
    "no_docstrings": "(Shortened to fit: docstrings are left out.)",
    "no_private": "(Shortened to fit: docstrings and names starting with `_` are left out.)",
    "no_methods": "(Shortened to fit: only classes and top-level functions are listed.)",
}
_NOTES["truncated"] = _NOTES["no_methods"]
_FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Compound statements whose bodies still define names in the enclosing module
# or class (``if TYPE_CHECKING:``, ``try: ... except``), as in repo_index.
_SCOPE_PRESERVING = (ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith,
                     ast.For, ast.AsyncFor, ast.While)


@dataclass
class Outline:
    """The outline text and how far it had to be shortened."""

    text: str
    reduction: str = "none"
    files: int = 0
    omitted_files: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        """Length of the text in characters."""
        return len(self.text)


@dataclass
class _Entry:
    """One line of a file's outline; a class has ``children``."""

    kind: str  # class, def, assign, fields
    name: str
    text: str
    lines: str
    doc: str = ""
    children: list["_Entry"] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # of a fields entry


@dataclass
class _File:
    path: str
    line_count: int
    entries: list[_Entry] | None  # None: the file could not be parsed


def is_source_file(path: str) -> bool:
    """True for a Python file that is not a test: the files the outline lists."""
    *directories, name = path.split("/")
    return (name.endswith(".py") and "tests" not in directories
            and not name.startswith("test_") and name != "conftest.py")


def is_private(name: str) -> bool:
    """True for ``_name``; ``__init__`` and other dunder names are not private."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node, clean=True) or ""
    line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
    return line if len(line) <= DOC_CHARS else line[:DOC_CHARS - 3].rstrip() + "..."


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True for a ``typing.overload`` stub; the real definition follows it."""
    return any((isinstance(decorator, ast.Name) and decorator.id == "overload")
               or (isinstance(decorator, ast.Attribute) and decorator.attr == "overload")
               for decorator in node.decorator_list)


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> _Entry:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return _Entry("def", node.name, f"{prefix} {node.name}({ast.unparse(node.args)}){returns}",
                  f"L{node.lineno}", _first_line(node))


def _class(node: ast.ClassDef) -> _Entry:
    bases = [ast.unparse(base) for base in node.bases]
    bases += [f"{keyword.arg}={ast.unparse(keyword.value)}" if keyword.arg
              else f"**{ast.unparse(keyword.value)}" for keyword in node.keywords]
    text = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    entry = _Entry("class", node.name, text, f"L{node.lineno}-L{node.end_lineno}",
                   _first_line(node))
    fields: list[tuple[str, int]] = []
    _collect(node.body, entry.children, fields)
    if fields:
        first, last = min(line for _, line in fields), max(line for _, line in fields)
        lines = f"L{first}" if first == last else f"L{first}-L{last}"
        names = list(dict.fromkeys(name for name, _ in fields))
        entry.children.insert(0, _Entry("fields", "", "fields", lines, names=names))
    return entry


def _collect(statements: list[ast.stmt], out: list[_Entry],
             fields: list[tuple[str, int]] | None = None) -> None:
    """Outline the definitions of a module body, or of a class body with ``fields``.

    In a class, only methods get entries: its attribute names are collected
    in ``fields`` and nested classes are left out.
    """
    in_class = fields is not None
    for statement in statements:
        if isinstance(statement, _FUNCTION_DEFS):
            if not _is_overload(statement):
                out.append(_function(statement))
        elif isinstance(statement, ast.ClassDef):
            if not in_class:
                out.append(_class(statement))
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (statement.targets if isinstance(statement, ast.Assign)
                       else [statement.target])
            for name in (name for target in targets for name in _target_names(target)):
                if in_class:
                    fields.append((name, statement.lineno))
                elif not any(entry.kind == "assign" and entry.name == name for entry in out):
                    # a name assigned again (if/else branches) is listed once
                    out.append(_Entry("assign", name, f"{name} = ...",
                                      f"L{statement.lineno}"))
        elif isinstance(statement, _SCOPE_PRESERVING):
            for block in ("body", "orelse", "finalbody"):
                _collect(getattr(statement, block, []), out, fields)
            for handler in getattr(statement, "handlers", []):
                _collect(handler.body, out, fields)


def _read(root: Path, path: str) -> _File:
    try:
        source = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _File(path, 0, None)
    line_count = len(source.splitlines())
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return _File(path, line_count, None)
    entries: list[_Entry] = []
    _collect(tree.body, entries)
    return _File(path, line_count, entries)


def _line(entry: _Entry, level: int, indent: str = "") -> str:
    doc = f": {entry.doc}" if entry.doc and level == 0 else ""
    return f"{indent}{entry.text} ({entry.lines}){doc}"


def _section(file: _File, level: int) -> str:
    """One file at reduction ``level`` (an index into ``REDUCTIONS``)."""
    lines = [f"## {file.path} ({file.line_count} lines)"]
    if file.entries is None:
        return lines[0] + " (could not be parsed)"
    for entry in file.entries:
        if (level >= 2 and is_private(entry.name)) or (level >= 3 and entry.kind == "assign"):
            continue
        lines.append(_line(entry, level))
        for child in entry.children if level < 3 else []:
            if child.kind == "fields":
                names = [name for name in child.names if level < 2 or not is_private(name)]
                if names:
                    lines.append(f"  fields: {', '.join(names)} ({child.lines})")
            elif level < 2 or not is_private(child.name):
                lines.append(_line(child, level, "  "))
    return "\n".join(lines)


def _join(note: str, sections: list[str]) -> str:
    return "\n\n".join(([note] if note else []) + sections) + "\n"


def build_outline(root: Path, index: RepoIndex, limit: int = OUTLINE_LIMIT) -> Outline:
    """Outline every source file of ``index`` as checked out in ``root``.

    The text never exceeds ``limit`` characters: if the full outline is
    longer, docstrings go first, then private names, then methods and
    module-level names, and finally whole files, which are then named.
    """
    files = [_read(Path(root), path) for path in sorted(index.files) if is_source_file(path)]
    if not files:
        return Outline("(no Python source files)\n")
    text = ""
    for level, reduction in enumerate(REDUCTIONS[:-1]):
        text = _join(_NOTES.get(reduction, ""), [_section(file, level) for file in files])
        if len(text) <= limit:
            return Outline(text, reduction, len(files))
    # Still too long: list as many whole files as fit and name the rest.
    sections = [_section(file, len(REDUCTIONS) - 2) for file in files]
    kept: list[str] = []
    while len(kept) < len(sections):
        rest = [file.path for file in files[len(kept) + 1:]]
        if len(_truncated(kept + [sections[len(kept)]], rest)) > limit:
            break
        kept.append(sections[len(kept)])
    omitted = [file.path for file in files[len(kept):]]
    return Outline(_truncated(kept, omitted)[:limit], "truncated", len(files), omitted)


def _truncated(sections: list[str], omitted: list[str]) -> str:
    text = _join(_NOTES["truncated"], sections)
    if omitted:
        text += (f"\n(Outline cut off here: {len(omitted)} more source file(s) are not "
                 f"listed: {', '.join(omitted)}.)\n")
    return text


def repo_outline(root: Path, index: RepoIndex) -> str:
    """The outline text of ``build_outline`` with the default limit."""
    return build_outline(root, index).text
