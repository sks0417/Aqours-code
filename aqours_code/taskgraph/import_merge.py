"""Resolve merge conflicts in Python import statements without a model.

Nodes that edit one file in parallel (``edit_set.shared``) often each add
their module to the same import lines at the top of it. Their other edits do
not overlap, but those lines conflict on every merge after the first. A
program can resolve such a conflict exactly, so the Coordinator tries this
before it hands a conflict to the worker.

A conflict hunk is *import-only* when each of its three sides (ours, the
common base, theirs) holds nothing but Python import statements and blank
lines, and some side holds an import: no comment, no other statement,
nothing that does not parse on its own, no ``*`` import. Only ``.py`` files
are considered. Such a hunk is resolved by a three-way merge of the imported
names, per module (``from M import ...``, ``import M``): a name either side
added is kept, a name either side removed is dropped, and an alias
(``stats as stats_module``) is part of the name.

The merged statements keep the order and blank lines of *theirs* (the
integration branch, so the resolved file differs from it as little as
possible). A statement only ours has goes after the statement it follows in
ours, else before the one it precedes there, else last. The statements of
one module become one; its aliased names stay on lines of their own, one
each (as isort and ruff do by default), unless a side has them in one
statement with unaliased names. Names are sorted when every statement of
every side lists them sorted (by type as isort and ruff do,
case-insensitively, or plainly); otherwise they keep theirs' order, and
ours' new names are placed by the same rule as statements. Each statement is
written anew: a ``from`` import longer than ``LINE_LIMIT`` is wrapped in
parentheses, aligned under the first name, and ``import M`` takes one line
per module. The result depends on the three sides alone, so it is
deterministic.

The base side comes from the same conflict written with diff3-style markers
(``gitops.conflict_diff3``). Hunks are analysed there, and the result is
applied to the file as git wrote it, whatever its marker style. The two can
cut the conflict differently (the default style moves lines both sides
share out of a hunk, and joins hunks a few lines apart), so they are
compared in groups: stretches of the file where both have a hunk boundary at
each end. A group whose diff3 hunks are all import-only is replaced by its
diff3 text with each hunk merged; every other hunk keeps its markers byte for
byte. Nothing is changed when the two files do not line up (each side, read
through all hunks, must be the same text in both), or when the file, with
``ours`` for the hunks left, does not compile or has a merged line that is
not part of a top-level import statement (for example a line in a string).
"""
from __future__ import annotations

import ast
import bisect
import io
import tokenize
import warnings
from collections.abc import Callable
from dataclasses import dataclass

LINE_LIMIT = 79
MARKER_SIZE = 7

# A key is ("from", module) or ("import", module); a name is (name, alias).
Key = tuple[str, str]
Name = tuple[str, str | None]
# One import statement of a side (an ``import a, b`` gives one per module),
# or None for a blank line.
Item = tuple[Key, list[Name]] | None
Span = tuple[int, int, int, int]  # [start, end) in the ours view, then in theirs


@dataclass
class Hunk:
    """One conflict hunk: the lines of each side, and the hunk as written."""

    ours: list[str]
    base: list[str] | None  # None: no base section (the default marker style)
    theirs: list[str]
    raw: list[str]  # every line of the hunk, markers included


@dataclass
class FileResolution:
    """What :func:`resolve_import_conflicts` made of one file."""

    text: str
    resolved: int = 0  # hunks (as git wrote them) replaced by merged imports
    left: int = 0  # hunks that keep their markers

    @property
    def complete(self) -> bool:
        """True when every hunk of the file was resolved."""
        return self.resolved > 0 and self.left == 0


def split_lines(text: str) -> list[str]:
    """``text`` as lines that keep their ``\\n`` (or ``\\r\\n``)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _marker(line: str) -> str | None:
    """Which conflict marker ``line`` is (``<``, ``|``, ``=``, ``>``), or None."""
    body = line.rstrip("\r\n")
    if not body or body[0] not in "<|=>" or body[:MARKER_SIZE] != body[0] * MARKER_SIZE:
        return None
    rest = body[MARKER_SIZE:]
    return body[0] if not rest or rest.startswith(" ") else None


def parse_conflicts(text: str) -> list[str | Hunk] | None:
    """Split ``text`` into lines outside conflicts (``str``) and hunks, in order.

    Returns None when the markers do not form complete hunks.
    """
    segments: list[str | Hunk] = []
    hunk: Hunk | None = None
    section: list[str] = []
    for line in split_lines(text):
        kind = _marker(line)
        if hunk is None:
            if kind == "<":
                hunk = Hunk([], None, [], [line])
                section = hunk.ours
            else:
                segments.append(line)
            continue
        hunk.raw.append(line)
        if kind is None:
            section.append(line)
        elif kind == "|" and section is hunk.ours:
            hunk.base = []
            section = hunk.base
        elif kind == "=" and section is not hunk.theirs:
            section = hunk.theirs
        elif kind == ">" and section is hunk.theirs:
            segments.append(hunk)
            hunk = None
        else:
            return None
    return segments if hunk is None else None


# ── one hunk ──

def _statements(node: ast.Import | ast.ImportFrom) -> list[Item]:
    if isinstance(node, ast.Import):
        return [(("import", alias.name), [(alias.name, alias.asname)])
                for alias in node.names]
    module = "." * node.level + (node.module or "")
    return [(("from", module), [(alias.name, alias.asname) for alias in node.names])]


def _layout(text: str) -> list[Item] | None:
    """The statements and blank lines of one side; None if it is not import-only."""
    lines = split_lines(text)
    if not text.strip():
        return [None] * len(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None
    if any(token.type == tokenize.COMMENT for token in tokens):
        return None
    starts: dict[int, list[ast.Import | ast.ImportFrom]] = {}
    covered: set[int] = set()
    for node in tree.body:
        if (not isinstance(node, (ast.Import, ast.ImportFrom))
                or any(alias.name == "*" for alias in node.names)):
            return None
        covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        starts.setdefault(node.lineno, []).append(node)
    layout: list[Item] = []
    for number, line in enumerate(lines, 1):
        for node in starts.get(number, ()):
            layout.extend(_statements(node))
        if number not in covered:
            if line.strip():
                return None
            layout.append(None)
    return layout


def _names(layout: list[Item]) -> dict[Key, list[Name]]:
    """The names of each module, both in order of first appearance."""
    names: dict[Key, list[Name]] = {}
    for item in layout:
        if item is not None:
            bucket = names.setdefault(item[0], [])
            bucket.extend(name for name in item[1] if name not in bucket)
    return names


def _kind(name: str) -> int:
    """isort's and ruff's order by type: CONSTANTS, Classes, then the rest."""
    if name.isupper() and len(name) > 1:
        return 0
    return 1 if name[:1].isupper() else 2


SORT_KEYS: tuple[Callable[[Name], tuple], ...] = (
    lambda name: (_kind(name[0]), name[0].lower(), name[0], name[1] or ""),
    lambda name: (name[0].lower(), name[0], name[1] or ""),
    lambda name: (name[0], name[1] or ""),
)


def _neighbours(order: list, position: int, placed: Callable[[object], bool]):
    """The nearest item before ``position`` in ``order`` that is placed, and after it."""
    before = next((item for item in reversed(order[:position]) if placed(item)), None)
    after = next((item for item in order[position + 1:] if placed(item)), None)
    return before, after


def _order(kept: list[Name], ours: list[Name], theirs: list[Name],
           statements: list[list[Name]]) -> list[Name]:
    """The order of a module's merged names (see the module docstring)."""
    for sort_key in SORT_KEYS:
        if all(names == sorted(names, key=sort_key) for names in statements):
            return sorted(kept, key=sort_key)
    result = [name for name in theirs if name in kept]
    for position, name in enumerate(ours):
        if name in result or name not in kept:
            continue
        before, after = _neighbours(ours, position, lambda item: item in result)
        if before is not None:
            result.insert(result.index(before) + 1, name)
        elif after is not None:
            result.insert(result.index(after), name)
        else:
            result.append(name)
    return result


def _from_lines(head: str, parts: list[str], limit: int) -> list[str]:
    """``head`` and the names, wrapped in parentheses when longer than ``limit``."""
    line = head + ", ".join(parts)
    if len(line) <= limit:
        return [line]
    indent = " " * (len(head) + 1)
    if len(indent) + max(len(part) for part in parts) + 1 > limit:
        return [head + "(", *(f"    {part}," for part in parts), ")"]
    lines, current = [], head + "("
    for index, part in enumerate(parts):
        piece = part + ("," if index < len(parts) - 1 else ")")
        if current.endswith("("):
            current += piece
        elif len(current) + 1 + len(piece) <= limit:
            current += " " + piece
        else:
            lines.append(current)
            current = indent + piece
    lines.append(current)
    return lines


def _render(key: Key, names: list[Name], combined: bool,
            limit: int = LINE_LIMIT) -> list[str]:
    """The lines of one module's statements, without line endings."""
    kind, module = key
    if kind == "import":
        return [f"import {name}" if alias is None else f"import {name} as {alias}"
                for name, alias in names]
    head = f"from {module} import "
    plain = [name if alias is None else f"{name} as {alias}"
             for name, alias in names if alias is None or combined]
    lines = _from_lines(head, plain, limit) if plain else []
    for name, alias in names:
        if alias is not None and not combined:
            lines += _from_lines(head, [f"{name} as {alias}"], limit)
    return lines


def _groups(layout: list[Item]) -> tuple[int, list[list[Key]], list[int], int]:
    """Leading blank lines, groups of modules, blank lines between groups, trailing ones."""
    lead = 0
    while lead < len(layout) and layout[lead] is None:
        lead += 1
    trail = 0
    while trail < len(layout) - lead and layout[len(layout) - 1 - trail] is None:
        trail += 1
    groups: list[list[Key]] = [[]]
    gaps: list[int] = []
    run = 0
    for item in layout[lead:len(layout) - trail]:
        if item is None:
            run += 1
            continue
        if run:
            groups.append([])
            gaps.append(run)
            run = 0
        if not any(item[0] in group for group in groups):
            groups[-1].append(item[0])
    return lead, groups, gaps, trail


def merge_imports(ours: str, base: str, theirs: str, newline: str = "\n") -> str | None:
    """Three-way merge of the imports of one conflict hunk (see the module docstring).

    Returns the merged text, every line ending with ``newline``, or None when
    a side is not import-only.
    """
    layouts = [_layout(ours), _layout(base), _layout(theirs)]
    if any(layout is None for layout in layouts):
        return None
    ours_names, base_names, theirs_names = (_names(layout) for layout in layouts)
    if not (ours_names or base_names or theirs_names):
        return None  # blank lines only: not an import conflict
    statements = [item for layout in layouts for item in layout if item is not None]
    combined = {key for key, names in statements
                if any(alias is None for _, alias in names)
                and any(alias is not None for _, alias in names)}
    merged: dict[Key, list[Name]] = {}
    for key in dict.fromkeys([*theirs_names, *ours_names]):
        o, b, t = (names.get(key, []) for names in (ours_names, base_names, theirs_names))
        kept = [name for name in dict.fromkeys([*t, *o])
                if name not in b or (name in o and name in t)]
        merged[key] = _order(kept, o, t, [names for k, names in statements if k == key])
    skeleton, other = ((layouts[2], ours_names) if theirs_names
                       else (layouts[0], theirs_names))
    lead, groups, gaps, trail = _groups(skeleton)

    def find(key: object) -> list[Key] | None:
        return next((group for group in groups if key in group), None)

    order = list(other)
    for position, key in enumerate(order):
        if find(key) is not None:
            continue
        before, after = _neighbours(order, position, lambda item: find(item) is not None)
        if before is not None:
            group = find(before)
            group.insert(group.index(before) + 1, key)
        elif after is not None:
            group = find(after)
            group.insert(group.index(after), key)
        else:
            groups[-1].append(key)
    lines = [""] * lead
    rendered = False
    for index, group in enumerate(groups):
        keys = [key for key in group if merged.get(key)]
        if not keys:
            continue
        if rendered:
            lines += [""] * gaps[index - 1]
        for key in keys:
            lines += _render(key, merged[key], key in combined)
        rendered = True
    lines += [""] * trail
    return "".join(line + newline for line in lines)


# ── one file ──

def _view(segments: list[str | Hunk], side: str) -> list[str]:
    """The file as one side sees it: every hunk replaced by that side."""
    lines: list[str] = []
    for segment in segments:
        lines.extend(getattr(segment, side) if isinstance(segment, Hunk) else [segment])
    return lines


def _spans(segments: list[str | Hunk]) -> list[Span]:
    """Where each segment lies in the ours view and in the theirs view."""
    spans: list[Span] = []
    o = t = 0
    for segment in segments:
        if isinstance(segment, Hunk):
            span = (o, o + len(segment.ours), t, t + len(segment.theirs))
        else:
            span = (o, o + 1, t, t + 1)
        spans.append(span)
        o, t = span[1], span[3]
    return spans


def _boundaries(spans: list[Span]) -> set[tuple[int, int]]:
    points = {(span[0], span[2]) for span in spans}
    if spans:
        points.add((spans[-1][1], spans[-1][3]))
    return points


def _imports_in_context(path: str, text: str, merged_lines: list[int]) -> bool:
    """True if ``text`` compiles and each of ``merged_lines`` is in a top-level import."""
    source = text[1:] if text.startswith("﻿") else text
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename=path)
            compile(tree, path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError):
        return False
    covered: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return all(number in covered for number in merged_lines)


def resolve_import_conflicts(path: str, written: str,
                             diff3: str | None = None) -> FileResolution:
    """Resolve the import-only conflict hunks of one file (see the module docstring).

    ``written`` is the file as git wrote it; ``diff3`` is the same conflict
    with diff3-style markers (None: ``written`` already has them). Returns the
    new text and how many hunks of ``written`` were resolved and are left. A
    file that is not ``.py``, or whose markers cannot be read, is returned
    unchanged.
    """
    shown = parse_conflicts(written)
    if shown is None:
        return FileResolution(written)
    hunks = sum(isinstance(segment, Hunk) for segment in shown)
    unchanged = FileResolution(written, 0, hunks)
    analysed = shown if diff3 is None else parse_conflicts(diff3)
    if (not path.endswith(".py") or not hunks or analysed is None
            or any(_view(analysed, side) != _view(shown, side) for side in ("ours", "theirs"))):
        return unchanged
    shown_spans, analysed_spans = _spans(shown), _spans(analysed)
    cuts = sorted(_boundaries(shown_spans) & _boundaries(analysed_spans))

    def group(span: Span) -> int:
        return bisect.bisect_right(cuts, (span[0], span[2])) - 1

    # Each group's text from the diff3 file, with a flag on the merged lines.
    texts: dict[int, list[tuple[str, bool]]] = {}
    mergeable: dict[int, bool] = {}
    for segment, span in zip(analysed, analysed_spans):
        index = group(span)
        lines = texts.setdefault(index, [])
        if not isinstance(segment, Hunk):
            lines.append((segment, False))
            continue
        merged = None
        if segment.base is not None:
            newline = "\r\n" if segment.raw[0].endswith("\r\n") else "\n"
            merged = merge_imports("".join(segment.ours), "".join(segment.base),
                                   "".join(segment.theirs), newline)
        mergeable[index] = merged is not None and mergeable.get(index, True)
        lines.extend((line, True) for line in split_lines(merged or ""))
    conflicted = {group(span) for segment, span in zip(shown, shown_spans)
                  if isinstance(segment, Hunk)}
    resolved_groups = {index for index, ok in mergeable.items() if ok and index in conflicted}
    if not resolved_groups:
        return unchanged
    out: list[str] = []
    check: list[str] = []  # the file with ``ours`` for the hunks left
    merged_lines: list[int] = []
    done: set[int] = set()
    resolved = 0
    for segment, span in zip(shown, shown_spans):
        index = group(span)
        if index in resolved_groups:
            resolved += isinstance(segment, Hunk)
            if index not in done:
                done.add(index)
                for line, is_merged in texts[index]:
                    out.append(line)
                    check.append(line)
                    if is_merged and line.strip():
                        merged_lines.append(len(check))
        elif isinstance(segment, Hunk):
            out.extend(segment.raw)
            check.extend(segment.ours)
        else:
            out.append(segment)
            check.append(segment)
    if not resolved or not _imports_in_context(path, "".join(check), merged_lines):
        return unchanged
    return FileResolution("".join(out), resolved, hunks - resolved)
