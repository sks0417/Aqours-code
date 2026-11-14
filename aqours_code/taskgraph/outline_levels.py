"""A two-level outline of a repository for the planner (``plan --outline two-level``).

The full outline (:mod:`outline`) gives every definition of every source
file. On a large repository it is most of the planner's first message (52.7K
of 79.9K characters for dvc 0.89), and that message is kept whole by every
compaction, so the planner has little room left for reading code. The
two-level outline keeps the overview and details only what the request is
about. No model is called.

- **Level 1**, every source file, always: its path, its number of lines and
  the names of its top-level classes and functions (a function's name ends
  in ``()``), on one line. No signatures, methods or fields.
- **Level 2**, the detail of the full outline without docstrings (signatures,
  methods, fields, line numbers), for files chosen from the request text by
  a fixed rule:

  (a) files whose path or dotted module name appears in the request (a module
      name with at least two parts, so that ``dvc`` alone does not pick
      ``dvc/__init__.py``);
  (b) files that define a class, function or method whose name appears in
      the request as a whole word. A name is left out when it is shorter than
      ``MIN_NAME_CHARS``, a dunder name, or common: defined in more than
      ``COMMON_DEFINED_IN`` source files (``run``, ``save``), or a plain
      lowercase word (letters only) that the request does not put in an
      inline code span (``error``, ``remote``: English words of the request;
      a fenced code block does not count, its comments and parameter names
      are words too);
  (c) the direct import neighbours of (a) and (b) (files they import and files
      that import them), from :mod:`affected`'s import graph.

  Level 2 is filled in the order (a), (b), (c), within (a) by first mention,
  within (b) by the number of names matched, within (c) by the number of
  chosen files a file neighbours, until ``LEVEL2_BUDGET`` characters; a file
  that does not fit is cut (and recorded), and smaller ones after it may
  still fit.

The other files (tests, documentation, data) are listed apart; a directory
with more than ``COLLAPSE_FILES`` of them is one line with its count.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .affected import TestImports, scan_test_imports
from .outline import _File, _read, _section, is_source_file
from .repo_index import RepoIndex

LEVEL2_BUDGET = 15_000  # characters
MIN_NAME_CHARS = 4
COMMON_DEFINED_IN = 3  # a name defined in more source files than this is too common
COLLAPSE_FILES = 12  # a directory with more non-source files than this is one line
OTHER_FILES_LIMIT = 300
OUTLINE_MODES = ("two-level", "full")
RULES = {"a": "named in the request", "b": "defines a name the request uses",
         "c": "imports or is imported by a file above"}
SOURCE_ROOTS = ("src/",)

# The planner's instructions describe the outline. In two-level mode their
# paragraph about it is replaced by this one (the full mode keeps the original).
FULL_OUTLINE_PARAGRAPH = (
    "The outline below lists every definition in the source files with its\n"
    "signature, line numbers and the first line of its docstring. Work from the\n"
    "outline. Read a file only for a detail the outline does not show, such as\n"
    "how a function works inside, and read only the lines you need. Do not read\n"
    "whole source files to find out what they contain, and do not read test\n"
    "files: their names in the file list are enough to choose where new tests go.")
TWO_LEVEL_PARAGRAPH = (
    "The outline below has two levels. Level 1 lists every source file with its\n"
    "number of lines and the names of its top-level classes and functions. Level 2\n"
    "adds signatures, methods, fields and line numbers, but only for some files:\n"
    "the files the request names, the files that define a name the request uses,\n"
    "and the files they import or that import them. Work from the outline. When you\n"
    "need the definitions of another file, or how a function works inside, read it\n"
    "with read_file, and read only the lines you need. Do not read test files: their\n"
    "names in the file list are enough to choose where new tests go (a collapsed\n"
    "directory shows how many files it has; list them with glob).")
OUTLINE_INTRO = (
    "Level 1 lists every source file; level 2 gives the signatures, methods, fields "
    "and line numbers of some files only (those the request names, those that define "
    "a name the request uses, and the files they import or that import them). Read any "
    "other file's definitions with read_file when you need them.")


@dataclass
class Level2File:
    """A file chosen for level 2, by which rule and why."""

    path: str
    rule: str  # "a", "b", "c"
    why: str
    chars: int = 0

    def to_dict(self) -> dict:
        return {"path": self.path, "rule": self.rule, "why": self.why, "chars": self.chars}


@dataclass
class TwoLevelOutline:
    """The outline text, its parts' sizes, and what level 2 holds and left out."""

    text: str
    level1_chars: int = 0
    level2_chars: int = 0
    files: int = 0
    chosen: list[Level2File] = field(default_factory=list)
    cut: list[Level2File] = field(default_factory=list)

    @property
    def chars(self) -> int:
        """Length of the text in characters."""
        return len(self.text)

    def report(self) -> dict:
        """What the plan report records about the outline."""
        return {"outline_level1_chars": self.level1_chars,
                "outline_level2_chars": self.level2_chars,
                "outline_level2_files": [item.to_dict() for item in self.chosen],
                "outline_level2_cut": [item.to_dict() for item in self.cut]}


# ── level 1 ──

def level1_line(file: _File) -> str:
    """``- path (N lines): Class, function(), ...``"""
    head = f"- {file.path} ({file.line_count} lines)"
    if file.entries is None:
        return head + ": (could not be parsed)"
    names = [entry.name + ("()" if entry.kind == "def" else "")
             for entry in file.entries if entry.kind in ("class", "def")]
    return f"{head}: {', '.join(names)}" if names else head


# ── choosing level 2 ──

def module_name(path: str) -> str:
    """``dvc/remote/local.py`` -> ``dvc.remote.local``; ``src/`` is left out."""
    for root in SOURCE_ROOTS:
        if path.startswith(root):
            path = path[len(root):]
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _path_forms(path: str) -> list[str]:
    forms = [path]
    for root in SOURCE_ROOTS:
        if path.startswith(root):
            forms.append(path[len(root):])
    return forms


def _first_mention(request: str, path: str) -> int | None:
    """Where the request first names ``path`` or its module (None if it does not)."""
    positions = []
    for form in _path_forms(path):
        match = re.search(rf"(?<![\w.\-]){re.escape(form)}(?![\w/])", request)
        if match:
            positions.append(match.start())
    module = module_name(path)
    if module.count(".") >= 1:
        match = re.search(rf"(?<![\w.]){re.escape(module)}(?!\w)", request)
        if match:
            positions.append(match.start())
    return min(positions) if positions else None


def defined_names(file: _File) -> set[str]:
    """Names of the file's top-level classes and functions and of its methods."""
    names = set()
    for entry in file.entries or []:
        if entry.kind in ("class", "def"):
            names.add(entry.name)
        for child in entry.children:
            if child.kind == "def":
                names.add(child.name)
    return names


def request_names(request: str, definers: dict[str, set[str]]) -> set[str]:
    """Names the request uses that may choose a file (rule (b)).

    A plain lowercase word counts only inside an inline code span (``like
    this``) of the request; a fenced code block is not one, since its
    comments and parameter names (``scheme=None``) are words too.
    """
    unfenced = re.sub(r"```.*?```", "", request, flags=re.S)
    quoted = set(re.findall(r"`+([^`\n]+)`+", unfenced))
    quoted_words = {word for text in quoted for word in re.findall(r"[A-Za-z_]\w*", text)}
    chosen = set()
    for word in set(re.findall(r"(?<![\w])[A-Za-z_]\w*(?![\w])", request)):
        if word not in definers or len(word) < MIN_NAME_CHARS:
            continue
        if word.startswith("__") and word.endswith("__"):
            continue
        if len(definers[word]) > COMMON_DEFINED_IN:
            continue
        if re.fullmatch(r"[a-z]+", word) and word not in quoted_words:
            continue  # a plain English word of the request
        chosen.add(word)
    return chosen


def _neighbours(path: str, imports: TestImports, sources: set[str]) -> set[str]:
    out = set(imports.sources.get(path, ()))
    out |= {other for other, deps in imports.sources.items() if path in deps}
    return {other for other in out if other in sources and other != path}


def choose_level2(request: str, files: list[_File], imports: TestImports) -> list[Level2File]:
    """Files for level 2, in the order they are to be filled: (a), (b), (c)."""
    by_path = {file.path: file for file in files}
    sources = set(by_path)
    order: list[Level2File] = []
    taken: set[str] = set()
    mentions = {path: _first_mention(request, path) for path in sorted(sources)}
    for path in sorted((p for p, at in mentions.items() if at is not None),
                       key=lambda p: (mentions[p], p)):
        order.append(Level2File(path, "a", RULES["a"]))
        taken.add(path)
    definers: dict[str, set[str]] = {}
    for file in files:
        for name in defined_names(file):
            definers.setdefault(name, set()).add(file.path)
    names = request_names(request, definers)
    matches: dict[str, list[str]] = {}
    for name in sorted(names):
        for path in definers[name]:
            matches.setdefault(path, []).append(name)
    for path in sorted((p for p in matches if p not in taken),
                       key=lambda p: (-len(matches[p]), p)):
        order.append(Level2File(path, "b", f"defines {', '.join(sorted(matches[path]))}"))
        taken.add(path)
    seeds = [item.path for item in order]
    counts: Counter = Counter()
    reasons: dict[str, list[str]] = {}
    for seed in seeds:
        for other in _neighbours(seed, imports, sources):
            if other not in taken:
                counts[other] += 1
                reasons.setdefault(other, []).append(seed)
    for path in sorted(counts, key=lambda p: (-counts[p], p)):
        shown = reasons[path][:3] + (["..."] if len(reasons[path]) > 3 else [])
        order.append(Level2File(path, "c", f"neighbour of {', '.join(shown)}"))
    return order


# ── the outline ──

def build_two_level_outline(root: Path, index: RepoIndex, request: str,
                            imports: TestImports | None = None,
                            budget: int = LEVEL2_BUDGET) -> TwoLevelOutline:
    """The two-level outline of the checkout at ``root`` for ``request``."""
    files = [_read(Path(root), path) for path in sorted(index.files) if is_source_file(path)]
    if not files:
        return TwoLevelOutline("(no Python source files)\n")
    imports = imports if imports is not None else scan_test_imports(Path(root), index)
    level1 = "\n".join(level1_line(file) for file in files)
    by_path = {file.path: file for file in files}
    sections, chosen, cut, used = [], [], [], 0
    for item in choose_level2(request, files, imports):
        section = _section(by_path[item.path], 1).replace("## ", "### ", 1)
        item.chars = len(section) + 2
        if used + item.chars > budget:
            cut.append(item)
            continue
        used += item.chars
        chosen.append(item)
        sections.append(section)
    level2 = "\n\n".join(sections) if sections else "(no file was chosen)"
    parts = [OUTLINE_INTRO, "## Level 1: every source file\n\n" + level1,
             "## Level 2: details of some files\n\n" + level2]
    text = "\n\n".join(parts) + "\n"
    return TwoLevelOutline(text, len(level1), len(level2), len(files), chosen, cut)


def other_files_list(index: RepoIndex) -> str:
    """The files that are not source files; a crowded directory is one line."""
    others = sorted(path for path in index.files if not is_source_file(path))
    by_dir: dict[str, list[str]] = {}
    for path in others:
        by_dir.setdefault(str(PurePosixPath(path).parent), []).append(path)
    lines, done = [], set()
    for path in others:
        folder = str(PurePosixPath(path).parent)
        members = by_dir[folder]
        if len(members) > COLLAPSE_FILES:
            if folder not in done:
                done.add(folder)
                kinds = Counter(PurePosixPath(member).suffix or "(none)" for member in members)
                detail = ", ".join(f"{count} {suffix}" for suffix, count
                                   in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
                shown = "./" if folder == "." else f"{folder}/"
                lines.append(f"- {shown} ({len(members)} files: {detail}; list them with glob)")
        else:
            lines.append(f"- {path}")
    if len(lines) > OTHER_FILES_LIMIT:
        rest = len(lines) - OTHER_FILES_LIMIT
        lines = lines[:OTHER_FILES_LIMIT] + [f"- ... and {rest} more lines (use glob)"]
    return "\n".join(lines) or "- (none)"


def two_level_instructions(prompt: str) -> str:
    """The planner's instructions with the outline paragraph for two levels.

    Raises ``ValueError`` when the instructions no longer contain the
    paragraph about the full outline (an edit must update both).
    """
    if FULL_OUTLINE_PARAGRAPH not in prompt:
        raise ValueError("the planner's instructions have no paragraph about the outline "
                         "to replace")
    return prompt.replace(FULL_OUTLINE_PARAGRAPH, TWO_LEVEL_PARAGRAPH, 1)
