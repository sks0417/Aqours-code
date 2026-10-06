"""AST-based symbol index of a repository at one commit, read without checkout."""
from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .schema import SYMBOL_SEPARATOR
from .context_pack import markdown_headings

_FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Compound statements whose bodies still define names in the enclosing
# module or class scope (e.g. ``if TYPE_CHECKING:`` or ``try: ... except``).
_SCOPE_PRESERVING = (ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith,
                     ast.For, ast.AsyncFor, ast.While)


@dataclass
class RepoIndex:
    """Files and symbols present in a repository at ``commit``."""

    commit: str
    files: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    markdown_titles: dict[str, list[str]] = field(default_factory=dict)

    def has_file(self, path: str) -> bool:
        """Return whether ``path`` exists at the indexed commit."""
        return path in self.files

    def has_symbol(self, symbol: str) -> bool:
        """Return whether ``symbol`` is defined at the indexed commit."""
        return symbol in self.symbols


def _git(repo_path: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed: {detail or f'exit code {proc.returncode}'}")
    return proc.stdout


def _target_names(target: ast.expr) -> Iterator[str]:
    """Yield plain names bound by an assignment target."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _self_attribute_names(target: ast.expr, self_name: str) -> Iterator[str]:
    """Yield ``xxx`` for every ``self.xxx`` bound by an assignment target."""
    if (isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == self_name):
        yield target.attr
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _self_attribute_names(element, self_name)
    elif isinstance(target, ast.Starred):
        yield from _self_attribute_names(target.value, self_name)


def _walk_function_body(statements: Iterable[ast.stmt]) -> Iterator[ast.AST]:
    """Walk a function body without entering nested functions or classes."""
    nested_scopes = (*_FUNCTION_DEFS, ast.ClassDef, ast.Lambda)
    stack = [node for node in statements if not isinstance(node, nested_scopes)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, nested_scopes))


def _init_attributes(init: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    positional = [*init.args.posonlyargs, *init.args.args]
    if not positional:
        return set()
    self_name = positional[0].arg
    names: set[str] = set()
    for node in _walk_function_body(init.body):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_self_attribute_names(target, self_name))
        elif isinstance(node, ast.AnnAssign):
            names.update(_self_attribute_names(node.target, self_name))
    return names


def _collect_scope(statements: Iterable[ast.stmt], prefix: str,
                   in_class: bool, out: set[str]) -> None:
    for statement in statements:
        if isinstance(statement, _FUNCTION_DEFS):
            out.add(prefix + statement.name)
            if in_class and statement.name == "__init__":
                for attribute in _init_attributes(statement):
                    out.add(prefix + attribute)
        elif isinstance(statement, ast.ClassDef):
            out.add(prefix + statement.name)
            _collect_scope(statement.body, f"{prefix}{statement.name}.", True, out)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                out.update(prefix + name for name in _target_names(target))
        elif isinstance(statement, ast.AnnAssign):
            out.update(prefix + name for name in _target_names(statement.target))
        elif isinstance(statement, _SCOPE_PRESERVING):
            for block in ("body", "orelse", "finalbody"):
                _collect_scope(getattr(statement, block, []), prefix, in_class, out)
            for handler in getattr(statement, "handlers", []):
                _collect_scope(handler.body, prefix, in_class, out)


def extract_symbols(source: bytes | str, path: str) -> set[str]:
    """Return all symbols defined in one Python source file.

    Raises ``SyntaxError`` or ``ValueError`` when the source cannot be parsed.
    """
    tree = ast.parse(source, filename=path)
    qualnames: set[str] = set()
    _collect_scope(tree.body, "", False, qualnames)
    return {f"{path}{SYMBOL_SEPARATOR}{name}" for name in qualnames}


def build_index(repo_path: Path, commit: str) -> RepoIndex:
    """Index all files and Python symbols of ``repo_path`` at ``commit``."""
    repo_path = Path(repo_path)
    try:
        sha = _git(repo_path, "rev-parse", "--verify", "--quiet",
                   f"{commit}^{{commit}}").decode().strip()
    except RuntimeError as exc:
        raise RuntimeError(
            f"cannot resolve commit {commit!r} in {repo_path}: {exc}") from exc
    listing = _git(repo_path, "-c", "core.quotepath=off", "ls-tree", "-r", "-z",
                   "--name-only", sha)
    files = {name for name in listing.decode("utf-8").split("\0") if name}
    index = RepoIndex(commit=sha, files=files)
    for path in sorted(files):
        if path.lower().endswith((".md", ".markdown")):
            source = _git(repo_path, "show", f"{sha}:{path}").decode("utf-8", errors="replace")
            index.markdown_titles[path] = [h.title for h in markdown_headings(source)]
        if not path.endswith(".py"):
            continue
        source = _git(repo_path, "show", f"{sha}:{path}")
        try:
            index.symbols.update(extract_symbols(source, path))
        except (SyntaxError, ValueError) as exc:
            index.warnings.append(
                f"skipped {path}: cannot parse ({type(exc).__name__}: {exc})")
    return index
