"""The interface of a SWE-EVO release, from the base commit and the gold patch.

A release's tests use names that the release adds (a new class, a new
method): an agent that implements the release notes but names things
differently fails them although its code works. So each task can be given the
interface it must implement, as FeatureBench does: names and signatures, no
bodies, by a fixed rule.

**The rule.** For every non-test ``.py`` file the gold patch touches, the
public definitions at module level and at class level of the base and of the
base plus the gold patch are compared with ``ast`` (definitions nested in
functions are never looked at; a class nested in a class is class level):

- *added*: classes (with their bases and their public methods), functions,
  methods, and names assigned at module level (the name only, no value);
- *changed*: functions and methods whose signature (arguments and return
  annotation) differs; the new signature is shown;
- *removed*: classes, functions and methods that no longer exist.

A name is public when neither it nor a class around it starts with an
underscore; the dunder methods of a new class (``__init__``) count as public.
Each added or changed item is shown as written in the gold patch: its
decorators and signature (defaults and annotations included) and its
docstring, at most ``DOCSTRING_LINES`` lines. A body is never shown, nor any
test file or text of the test patch: only non-test files are read. A file
that does not parse is reported.

This module calls git only through ``gitops``.
"""
from __future__ import annotations

import ast
import io
import re
import tempfile
import textwrap
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .sweevo import patch_files

DOCSTRING_LINES = 15
SECTION_TITLE = "## Interface of this release"
SECTION_INTRO = ("The release adds, changes or removes the public definitions below. Use exactly "
                 "these names and signatures, because other code relies on them. How they work "
                 "inside is up to you.")
TEST_DIRS = ("tests", "test", "testing")
CHANGES = ("added", "changed", "removed")
_TRY_NODES = tuple(node for node in (getattr(ast, "Try", None), getattr(ast, "TryStar", None))
                   if node is not None)


def is_test_path(path: str) -> bool:
    """A test file: under a ``tests``/``test``/``testing`` directory, ``test_*.py``,
    ``*_test.py`` or ``conftest.py``."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return (any(part in TEST_DIRS for part in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


# ── definitions of one module ──

@dataclass
class Definition:
    """A public-or-not definition at module or class level; several nodes for
    overloads, property setters and conditional definitions."""

    qualname: str
    kind: str  # "class", "function", "method", "name"
    nodes: list = field(default_factory=list)


def _assigned_names(targets) -> list[str]:
    names = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names += _assigned_names(target.elts)
        elif isinstance(target, ast.Starred):
            names += _assigned_names([target.value])
    return names


def definitions(tree: ast.Module) -> dict[str, Definition]:
    """Definitions at module level and class level, by qualified name, in source order.

    Statements under ``if``, ``try`` and ``with`` at those levels count;
    function bodies are not entered. Assigned names count at module level only.
    """
    found: dict[str, Definition] = {}

    def add(qualname: str, kind: str, node) -> None:
        found.setdefault(qualname, Definition(qualname, kind)).nodes.append(node)

    def walk(statements, prefix: str, in_class: bool) -> None:
        for stmt in statements:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(prefix + stmt.name, "method" if in_class else "function", stmt)
            elif isinstance(stmt, ast.ClassDef):
                add(prefix + stmt.name, "class", stmt)
                walk(stmt.body, f"{prefix}{stmt.name}.", True)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and not in_class:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for name in _assigned_names(targets):
                    add(prefix + name, "name", stmt)
            elif isinstance(stmt, ast.If):
                walk(stmt.body, prefix, in_class)
                walk(stmt.orelse, prefix, in_class)
            elif isinstance(stmt, _TRY_NODES):
                walk(stmt.body, prefix, in_class)
                for handler in stmt.handlers:
                    walk(handler.body, prefix, in_class)
                walk(stmt.orelse, prefix, in_class)
                walk(stmt.finalbody, prefix, in_class)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body, prefix, in_class)

    walk(tree.body, "", False)
    return found


def is_public(qualname: str, kind: str, new_classes: set[str]) -> bool:
    """Neither the name nor a class around it starts with ``_``; a dunder method
    of a class in ``new_classes`` is public too."""
    parts = qualname.split(".")
    if any(part.startswith("_") for part in parts[:-1]):
        return False
    name = parts[-1]
    if not name.startswith("_"):
        return True
    dunder = name.startswith("__") and name.endswith("__") and len(name) > 4
    return dunder and kind == "method" and ".".join(parts[:-1]) in new_classes


def signature_key(node) -> tuple:
    """What makes two signatures the same: arguments, return annotation, ``async``."""
    if isinstance(node, ast.ClassDef):
        return ("class", tuple(ast.dump(base) for base in node.bases),
                tuple(ast.dump(keyword) for keyword in node.keywords))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (isinstance(node, ast.AsyncFunctionDef), ast.dump(node.args),
                ast.dump(node.returns) if node.returns is not None else None)
    return ("name",)


def header(node, lines: list[str]) -> str:
    """Decorators and the ``def``/``class`` line(s) up to the colon, as written, dedented."""
    first = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
    text = "\n".join(lines[node.lineno - 1:]) + "\n"
    depth, seen_keyword, end = 0, False, None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.NAME and token.string in ("def", "class"):
            seen_keyword = True
        elif token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0 and seen_keyword:
                end = (node.lineno - 1 + token.start[0], token.start[1])
                break
    if end is None:
        raise ValueError(f"no end of the header of {getattr(node, 'name', '?')}")
    block = lines[first - 1:end[0] - 1] + [lines[end[0] - 1][:end[1] + 1]]
    return textwrap.dedent("\n".join(block))


def capped_docstring(node) -> str | None:
    """The node's docstring, at most ``DOCSTRING_LINES`` lines (then ``...``)."""
    doc = ast.get_docstring(node)
    if not doc:
        return None
    doc_lines = doc.splitlines()
    if len(doc_lines) > DOCSTRING_LINES:
        doc_lines = doc_lines[:DOCSTRING_LINES] + ["..."]
    return "\n".join(doc_lines)


# ── the interface of one file ──

@dataclass
class Item:
    """One added, changed or removed public definition."""

    change: str  # "added", "changed", "removed"
    kind: str    # "class", "function", "method", "name"
    qualname: str
    line: int    # in the new file; in the old one for a removed item
    signatures: list[str] = field(default_factory=list)  # headers as written (not for removed)
    docstring: str | None = None


@dataclass
class FileInterface:
    """The interface changes of one file, or why they could not be read."""

    path: str
    items: list[Item] = field(default_factory=list)
    error: str = ""
    # The headers (as in the gold patch) of existing classes that gain or change methods.
    class_headers: dict[str, str] = field(default_factory=dict)


def _parse(source: str | None, path: str, which: str):
    if source is None:
        return None, []
    try:
        return ast.parse(source, filename=path), source.splitlines()
    except SyntaxError as exc:
        raise SyntaxError(f"{which} version does not parse: line {exc.lineno}: {exc.msg}") from None


def file_interface(path: str, old: str | None, new: str | None) -> FileInterface:
    """Compare one file's public definitions: ``old`` (base) and ``new`` (gold); None: absent."""
    result = FileInterface(path)
    try:
        old_tree, _ = _parse(old, path, "base")
        new_tree, new_lines = _parse(new, path, "gold")
    except SyntaxError as exc:
        result.error = f"SyntaxError: {exc}"
        return result
    before = definitions(old_tree) if old_tree is not None else {}
    after = definitions(new_tree) if new_tree is not None else {}
    new_classes = {name for name, item in after.items()
                   if item.kind == "class" and name not in before}
    old_classes = {name for name, item in before.items() if item.kind == "class"}
    for name, item in after.items():
        if not is_public(name, item.kind, new_classes):
            continue
        if name not in before:
            change = "added"
        elif (item.kind in ("function", "method")
              and [signature_key(node) for node in before[name].nodes]
              != [signature_key(node) for node in item.nodes]):
            change = "changed"
        else:
            continue
        signatures, docstring = [], None
        if item.kind != "name":
            signatures = [header(node, new_lines) for node in item.nodes]
            docstring = capped_docstring(item.nodes[-1])
        result.items.append(Item(change, item.kind, name, item.nodes[0].lineno,
                                 signatures, docstring))
    for name, item in before.items():
        if (name not in after and item.kind != "name"
                and is_public(name, item.kind, set())
                and not ("." in name and name.rsplit(".", 1)[0] not in after
                         and name.rsplit(".", 1)[0] in old_classes)):
            # a method of a removed class is listed with its class, not again
            result.items.append(Item("removed", item.kind, name, item.nodes[0].lineno))
    result.items.sort(key=lambda entry: (entry.change == "removed", entry.line))
    for entry in result.items:
        owner = entry.qualname.rsplit(".", 1)[0] if "." in entry.qualname else ""
        if (entry.kind == "method" and entry.change != "removed" and owner not in new_classes
                and owner in after and after[owner].kind == "class"):
            result.class_headers.setdefault(owner, header(after[owner].nodes[-1], new_lines))
    return result


def patched_sources(repo: Path, patch: str, commit: str = "HEAD"
                    ) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """The files a patch touches, before (at ``commit`` of ``repo``) and after it.

    None for a file that does not exist on that side. Only the touched files
    are written to a temporary directory and the patch is applied there.
    """
    paths = sorted({path for item in patch_files(patch) for path in (item["old"], item["new"])})
    before: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory(prefix="aqours-tg-iface-") as tmp:
        root = Path(tmp) / "files"
        root.mkdir()
        for path in paths:
            content = gitops.blob_at(repo, commit, path)
            before[path] = None if content is None else content.decode("utf-8", "replace")
            if content is not None:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(content)
        gitops.apply_patch(root, patch)
        after = {path: ((root / path).read_bytes().decode("utf-8", "replace")
                        if (root / path).is_file() else None) for path in paths}
    return before, after


def release_interface(repo: Path, gold_patch: str, commit: str = "HEAD") -> list[FileInterface]:
    """The interface of every non-test ``.py`` file the gold patch touches (with any change)."""
    before, after = patched_sources(repo, gold_patch, commit)
    files = []
    for path in sorted(before):
        if not path.endswith(".py") or is_test_path(path):
            continue
        interface = file_interface(path, before[path], after[path])
        if interface.items or interface.error:
            files.append(interface)
    return files


# ── the section of the request ──

def _docstring_block(doc: str, indent: str) -> list[str]:
    doc = doc.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    doc_lines = doc.splitlines()
    if len(doc_lines) == 1:
        return [f'{indent}"""{doc_lines[0]}"""']
    return ([f'{indent}"""{doc_lines[0]}'] + [f"{indent}{line}".rstrip() for line in doc_lines[1:]]
            + [f'{indent}"""'])


def _stub(item: Item, indent: str) -> list[str]:
    """``item`` as a stub: its decorators and signature(s), docstring, ``...``."""
    if item.kind == "name":
        return [f"{indent}{item.qualname.rsplit('.', 1)[-1]}"]
    out = []
    for number, signature in enumerate(item.signatures):
        out += [f"{indent}{line}".rstrip() for line in signature.splitlines()]
        body = indent + "    "
        if number == len(item.signatures) - 1 and item.docstring:
            out += _docstring_block(item.docstring, body)
        if item.kind != "class" or number < len(item.signatures) - 1:
            out.append(f"{body}...")
    return out


def render_file(interface: FileInterface) -> list[str]:
    """One file's part of the section: a heading and a stub block."""
    out = [f"### `{interface.path}`", ""]
    if interface.error:
        return out + [f"The interface of this file could not be read ({interface.error}).", ""]
    present = [item for item in interface.items if item.change != "removed"]
    removed = [item for item in interface.items if item.change == "removed"]
    classes = {item.qualname: item for item in present if item.kind == "class"}
    members: dict[str, list[Item]] = {}
    for item in present:
        owner = item.qualname.rsplit(".", 1)[0] if "." in item.qualname else ""
        if owner and item.kind == "method":
            members.setdefault(owner, []).append(item)
    blocks: list[tuple[int, list[str]]] = []
    for item in present:
        if item.kind == "method":
            continue
        if item.kind == "class":
            lines = [f"# {item.change}: class {item.qualname}"] + _stub(item, "")
            methods = members.pop(item.qualname, [])
            for number, member in enumerate(methods):
                if number or item.docstring:
                    lines.append("")
                lines += _stub(member, "    ")
            if not methods and not item.docstring:
                lines.append("    ...")
        else:
            label = "module-level name" if item.kind == "name" else "function"
            note = " (the new signature)" if item.change == "changed" else ""
            lines = [f"# {item.change}: {label} {item.qualname}{note}"] + _stub(item, "")
        blocks.append((item.line, lines))
    for owner, methods in members.items():  # methods of classes that already exist
        lines = [f"# existing class {owner}: its added or changed methods"]
        class_header = interface.class_headers.get(owner)
        indent = "    " if class_header else ""
        if class_header:
            lines += class_header.splitlines()
        for number, member in enumerate(methods):
            if number:
                lines.append("")
            note = " (the new signature)" if member.change == "changed" else ""
            lines.append(f"{indent}# {member.change}: {member.qualname.rsplit('.', 1)[-1]}{note}")
            lines += _stub(member, indent)
        blocks.append((methods[0].line, lines))
    out.append("```python")
    for number, (_, lines) in enumerate(sorted(blocks, key=lambda block: block[0])):
        if number:
            out.append("")
        out += lines
    if removed:
        if blocks:
            out.append("")
        out += [f"# removed: {item.kind} {item.qualname}" for item in removed]
    out += ["```", ""]
    return out


def interface_section(files: list[FileInterface]) -> str:
    """The request's interface section: title, introduction, then each file."""
    out = [SECTION_TITLE, "", SECTION_INTRO, ""]
    if not files:
        out += ["(The release adds, changes or removes no public definition.)", ""]
    for interface in files:
        out += render_file(interface)
    return "\n".join(out).rstrip("\n") + "\n"


def item_count(files: list[FileInterface]) -> int:
    """Added, changed and removed items: each class, method, function and name counts."""
    return sum(len(interface.items) for interface in files)


# ── checks of a section (used by the validation) ──

def module_bindings(source: str) -> set[str]:
    """Names bound at module level: definitions, assignments, imports (not ``*``)."""
    tree = ast.parse(source)
    names = {name.split(".")[0] for name in definitions(tree)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _at_module_level(tree, node):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def star_imports(source: str) -> list[tuple[str, int]]:
    """``from <module> import *`` at module level: (module, level)."""
    tree = ast.parse(source)
    return [(node.module or "", node.level) for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and _at_module_level(tree, node)
            and any(alias.name == "*" for alias in node.names)]


def _at_module_level(tree: ast.Module, target) -> bool:
    def walk(statements) -> bool:
        for stmt in statements:
            if stmt is target:
                return True
            if isinstance(stmt, ast.If) and (walk(stmt.body) or walk(stmt.orelse)):
                return True
            if isinstance(stmt, _TRY_NODES) and (
                    walk(stmt.body) or any(walk(h.body) for h in stmt.handlers)
                    or walk(stmt.orelse) or walk(stmt.finalbody)):
                return True
        return False

    return walk(tree.body)


def imported_names(source: str, package: str, is_module) -> list[tuple[str, str, str]]:
    """(module, name, how) for each name a test file takes from ``package``.

    ``how`` is ``"from"`` for ``from pkg.mod import Name``, and
    ``"attribute"`` for ``mod.Name`` where ``mod`` is a module the file
    imports (``import pkg.mod``, ``import pkg.mod as m``, ``from pkg import
    mod``): the attribute chain is followed as long as ``is_module(dotted)``
    says it names a module, and its next part is the name. A chain that does
    not start at a module (a class imported by name) is not followed.
    """
    tree = ast.parse(source)
    names: list[tuple[str, str, str]] = []
    aliases: dict[str, str] = {}  # local name -> dotted name it stands for
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                node.module == package or node.module.startswith(package + ".")):
            for alias in node.names:
                if alias.name != "*":
                    names.append((node.module, alias.name, "from"))
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    else:
                        aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
    inner = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)}
    # A local name that shadows an imported module (``da = ddf.a`` after
    # ``import dask.array as da``) is not followed anywhere in the file.
    rebound = {node.id for node in ast.walk(tree)
               if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    rebound |= {arg.arg for node in ast.walk(tree) if isinstance(node, ast.arguments)
                for arg in (*node.posonlyargs, *node.args, *node.kwonlyargs,
                            *(a for a in (node.vararg, node.kwarg) if a is not None))}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) in inner:
            continue
        chain = []
        value = node
        while isinstance(value, ast.Attribute):
            chain.append(value.attr)
            value = value.value
        if not isinstance(value, ast.Name) or value.id not in aliases or value.id in rebound:
            continue
        dotted = aliases[value.id]
        if not is_module(dotted):
            continue
        for attr in reversed(chain):
            if is_module(f"{dotted}.{attr}"):
                dotted = f"{dotted}.{attr}"
                continue
            names.append((dotted, attr, "attribute"))
            break
    return list(dict.fromkeys(names))


_TRIVIAL = re.compile(r"[\W_]*|pass|return|else:|try:|finally:|break|continue|raise|"
                      r"return None|return True|return False|\.\.\.")


def is_trivial(line: str) -> bool:
    """A line too common to tell anything (``)``, ``pass``, ``return None``)."""
    text = line.strip()
    return len(text) < 4 or bool(_TRIVIAL.fullmatch(text))


def added_body_lines(patch: str, after: dict[str, str | None]) -> set[str]:
    """Lines the patch adds to non-test ``.py`` files outside headers, decorators and docstrings.

    Stripped, and without trivial ones. A header is a ``def``/``class`` line
    up to its colon with the decorators above it; a docstring is that of the
    module, a class or a function.
    """
    lines: set[str] = set()
    for path, added in added_line_numbers(patch).items():
        source = after.get(path)
        if source is None or not path.endswith(".py") or is_test_path(path):
            continue
        source_lines = source.splitlines()
        allowed = _header_and_docstring_numbers(source)
        for number in added:
            if number not in allowed and 0 < number <= len(source_lines):
                text = source_lines[number - 1].strip()
                if not is_trivial(text):
                    lines.add(text)
    return lines


def _header_and_docstring_numbers(source: str) -> set[int]:
    """Line numbers of every header (decorators included) and docstring of a module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    source_lines = source.splitlines()
    numbers: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            head = header(node, source_lines)
            first = min([d.lineno for d in node.decorator_list] + [node.lineno])
            numbers.update(range(first, first + head.count("\n") + 1))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Module)) and ast.get_docstring(node) is not None:
            doc = node.body[0]
            numbers.update(range(doc.lineno, doc.end_lineno + 1))
    return numbers


def header_and_docstring_lines(sources: dict[str, str | None]) -> set[str]:
    """Stripped lines of the headers and docstrings of the non-test ``.py`` files."""
    lines: set[str] = set()
    for path, source in sources.items():
        if source is None or not path.endswith(".py") or is_test_path(path):
            continue
        source_lines = source.splitlines()
        for number in _header_and_docstring_numbers(source):
            if 0 < number <= len(source_lines):
                lines.add(source_lines[number - 1].strip())
    return lines


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_line_numbers(patch: str) -> dict[str, list[int]]:
    """For each file of a git diff, the line numbers (in the new file) of its added lines."""
    result: dict[str, list[int]] = {}
    path, number, in_header = None, 0, False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if path is not None:
                result.setdefault(path, [])
        elif (match := _HUNK.match(line)) is not None:
            number, in_header = int(match.group(1)), False
        elif in_header:
            continue
        elif path is not None and line.startswith("+"):
            result[path].append(number)
            number += 1
        elif path is not None and line.startswith(" "):
            number += 1
    return result
