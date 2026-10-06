"""Bounded, workspace-local context for one node attempt; no model calls."""
from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Node, parse_symbol, split_context_ref

MAX_PACK_BYTES = 80 * 1024


@dataclass(frozen=True)
class Heading:
    title: str
    level: int
    start: int
    end: int


def markdown_headings(source: str) -> list[Heading]:
    """ATX and setext headings outside fenced code blocks, with section offsets."""
    found: list[tuple[str, int, int]] = []
    offset = 0
    fence = ""
    previous = ""
    previous_offset = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) +
                            "{" + str(len(fence)) + r",}\s*", line):
                fence = ""
            previous = ""
        elif marker:
            fence = marker[1]
            previous = ""
        else:
            atx = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", line.rstrip("\r\n"))
            if atx:
                title = re.sub(r"[ \t]+#+[ \t]*$", "", atx[2] or "").strip()
                found.append((title, len(atx[1]), offset))
                previous = ""
            elif previous and re.fullmatch(r" {0,3}(=+|-+)[ \t]*", line.rstrip("\r\n")):
                found.append((previous.strip(), 1 if stripped[0] == "=" else 2,
                              previous_offset))
                previous = ""
            else:
                previous, previous_offset = stripped, offset
        offset += len(line)
    return [Heading(title, level, start,
                    next((other_start for _, other_level, other_start in found[i + 1:]
                          if other_level <= level), len(source)))
            for i, (title, level, start) in enumerate(found)]


def markdown_section(source: str, title: str) -> str:
    """Return all matching sections, including their heading and descendants."""
    matches = [h for h in markdown_headings(source) if h.title == title]
    if not matches:
        raise ValueError(f"Markdown heading does not exist: {title!r}")
    return "\n".join(source[h.start:h.end] for h in matches)


def interface_summary(source: str, path: str = "<context>") -> tuple[str, bool]:
    """Signatures/docstrings/fields and one-line assignments, or full parse fallback."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return source, True

    def doc(body):
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            return [copy.deepcopy(body[0])]
        return []

    def outline(body, in_class=False):
        result = doc(body)
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                item = copy.deepcopy(item)
                item.body = doc(item.body) + [ast.Expr(value=ast.Constant(value=Ellipsis))]
                result.append(item)
            elif isinstance(item, ast.ClassDef):
                item = copy.deepcopy(item)
                item.body = outline(item.body, True) or [ast.Expr(value=ast.Constant(value=Ellipsis))]
                result.append(item)
            elif isinstance(item, ast.AnnAssign) and in_class:
                result.append(copy.deepcopy(item))
            elif (not in_class and isinstance(item, (ast.Assign, ast.AnnAssign))
                  and item.lineno == item.end_lineno
                  and not any(isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp,
                                            ast.DictComp, ast.GeneratorExp))
                              for n in ast.walk(item))):
                result.append(copy.deepcopy(item))
        return result

    tree.body = outline(tree.body)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n", False


@dataclass
class ContextPack:
    text: str = ""
    limit_bytes: int = MAX_PACK_BYTES
    full_files: list[str] = field(default_factory=list)
    own_files: list[str] = field(default_factory=list)
    parse_fallbacks: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def report(self) -> dict:
        """Metadata saved beside the raw pack and in the run summary."""
        return {"chars": len(self.text), "bytes": len(self.text.encode("utf-8")),
                "full_files": self.full_files, "own_files": self.own_files,
                "parse_fallbacks": self.parse_fallbacks, "truncated": self.truncated,
                "missing": self.missing,
                "own_files_exceed_limit": len(self.text.encode("utf-8")) > self.limit_bytes}


def build_context_pack(node: Node, workspace: Path, *,
                       max_bytes: int = MAX_PACK_BYTES) -> ContextPack:
    """Read the current merged worktree; keep own files whole, then fill in order."""
    root = workspace.resolve()
    pack = ContextPack(limit_bytes=max_bytes, own_files=list(dict.fromkeys(
        [*node.edit_set.modify, *node.edit_set.create])))
    seen: set[str] = set()

    def read(path: str, *, optional=False) -> str | None:
        file = root / path
        if not file.resolve().is_relative_to(root):
            raise ValueError(f"context file escapes workspace: {path}")
        if not file.is_file():
            if not optional:
                pack.missing.append(path)
            return None
        return file.read_text(encoding="utf-8", errors="replace")

    def add(label: str, source: str, kind: str, full_path: str | None = None,
            own=False):
        # A longer fence keeps source containing backticks inside its block.
        fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", source)), default=0))
        header = f"## {label} ({kind})\n\n{fence}\n"
        footer = f"\n{fence}\n\n"
        block = header + source + footer
        budget = max_bytes - len(pack.text.encode("utf-8"))
        if not own and len(block.encode("utf-8")) > budget:
            pack.truncated.append(label)
            notice = "\n[truncated by context pack byte limit]"
            available = budget - len((header + notice + footer).encode("utf-8"))
            if available >= 0:
                prefix = source.encode("utf-8")[:available].decode("utf-8", errors="ignore")
                pack.text += header + prefix + notice + footer
            return False
        pack.text += block
        if full_path:
            pack.full_files.append(full_path)
        return True

    for path in pack.own_files:
        source = read(path, optional=path in node.edit_set.create)
        if source is not None:
            add(path, source, "own file, full", path, own=True)
        seen.add(path)

    refs = [split_context_ref(ref) for ref in node.context_files]
    python_paths = [path for path, _ in refs if path.endswith(".py")]
    python_paths += [parse_symbol(symbol).path for symbol in [*node.requires, *node.requires_impl]]
    remaining: list[tuple[str, str, str, str | None]] = []
    # conftest is always provided in full, at the final priority, even if requested.
    for path in dict.fromkeys(python_paths):
        if path in seen or path == "tests/conftest.py":
            continue
        seen.add(path)
        source = read(path)
        if source is not None:
            text, fallback = interface_summary(source, path)
            if fallback:
                pack.parse_fallbacks.append(path)
            remaining.append((path, text, "parse fallback, full" if fallback else "interface",
                              path if fallback else None))
    for path, title in refs:
        if path in seen or path.endswith(".py"):
            continue
        source = read(path)
        if source is not None:
            label = f"{path}#{title}" if title is not None else path
            text = markdown_section(source, title) if title is not None else source
            remaining.append((label, text, "section" if title is not None else "full",
                              None if title is not None else path))
        # Do not deduplicate sections by file: one file may request several titles.
        if title is None:
            seen.add(path)
    fixture = "tests/conftest.py"
    if fixture not in pack.own_files:
        source = read(fixture, optional=True)
        if source is not None:
            remaining.append((fixture, source, "fixture, full", fixture))
    exhausted = False
    for label, text, kind, full_path in remaining:
        if exhausted:
            pack.truncated.append(label)
        else:
            exhausted = not add(label, text, kind, full_path)
    return pack
