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
from .outline import _File, _line, _read, _section, is_source_file
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
    order.extend(_neighbour_files([item.path for item in order], imports, sources, taken))
    return order


def _neighbour_files(seeds: list[str], imports: TestImports, sources: set[str],
                     taken: set[str], near_first: bool = False) -> list[Level2File]:
    """Rule (c): the import neighbours of ``seeds``, most shared first.

    With ``near_first`` (per-item outlines), a neighbour in a seed's own
    directory comes before one further away when they neighbour as many seeds.
    """
    counts: Counter = Counter()
    reasons: dict[str, list[str]] = {}
    for seed in seeds:
        for other in _neighbours(seed, imports, sources):
            if other not in taken:
                counts[other] += 1
                reasons.setdefault(other, []).append(seed)

    def nearness(path: str) -> int:
        parts = PurePosixPath(path).parts[:-1]
        return max((len(_common(parts, PurePosixPath(seed).parts[:-1])) for seed in seeds),
                   default=0)

    key = ((lambda p: (-counts[p], -nearness(p), p)) if near_first
           else (lambda p: (-counts[p], p)))
    neighbours = []
    for path in sorted(counts, key=key):
        shown = reasons[path][:3] + (["..."] if len(reasons[path]) > 3 else [])
        neighbours.append(Level2File(path, "c", f"neighbour of {', '.join(shown)}"))
    return neighbours


def _common(first: tuple, second: tuple) -> tuple:
    common = []
    for left, right in zip(first, second):
        if left != right:
            break
        common.append(left)
    return tuple(common)


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


# ── one outline per draft item (``plan --facts per-item``) ──
#
# In per-item mode every draft item has its own investigator, and its own
# outline. Level 2 is chosen by the rule above, applied to the item's text
# instead of the whole request: its title and description, and the parts of
# the request about it (:func:`item_text`). On a large repository level 1 is
# shortened: a directory that holds no level-2 file is one line.

ITEM_LEVEL2_BUDGET = 12_000  # characters, for each item
LEVEL1_COLLAPSE_CHARS = 15_000  # a longer level 1 collapses directories without level 2
ITEM_RULES = {"a": "named in the item's text", "b": "defines a name the item's text uses",
              "c": "imports or is imported by a file above",
              "d": "its name is a word of the item"}
ITEM_TWO_LEVEL_PARAGRAPH = (
    "The outline below has two levels. Level 1 lists every source file with its\n"
    "number of lines and the names of its top-level classes and functions; on a\n"
    "large repository, a directory that has no file in level 2 is one line with\n"
    "its number of files (list them with glob). Level 2 adds signatures, methods,\n"
    "fields and line numbers, but only for some files: the files your item's text\n"
    "names (its title, its description and the parts of the request about it),\n"
    "the files named like one of its words, the files that define a name that\n"
    "text uses, and the files they import or that import them. A large file may\n"
    "show only the definitions whose names share a word with your item. Work\n"
    "from the outline. When you need the definitions of another file, or how a\n"
    "function works inside, read it with read_file, and read only the lines you\n"
    "need. Do not read test files: their names in the file list are enough to\n"
    "choose where new tests go (a collapsed directory shows how many files it\n"
    "has; list them with glob).")
ITEM_OUTLINE_INTRO = (
    "Level 1 lists every source file; level 2 gives the signatures, methods, fields "
    "and line numbers of some files only (those your item's text names, those named "
    "like one of its words, those that define a name it uses, and the files they "
    "import or that import them; a large file may be shortened to the definitions "
    "whose names share a word with your item). Read any other file's definitions with "
    "read_file when you need them.")
ITEM_OUTLINE_INTRO_COLLAPSED = ITEM_OUTLINE_INTRO + (
    " In level 1, a directory none of whose files is in level 2 is one line with its "
    "number of source files and lines; list its files with glob.")

# Words that say nothing about which item a part of the request is about.
_STOP_WORDS = frozenset("""
a an and are as at be been being but by can could did do does done each either for from
had has have how if in into is it its itself may might must no nor not of on once only
or other our out over own same shall should so some such than that the their them then
there these they this those through to too under until up upon via was we were what when
where which while who whom why will with would yet you your also any all both every more
most just like etc e.g i.e eg ie instead rather whether new old add adds added adding
make makes made use uses used using now so get gets set sets let lets work works one two
need needs needed allow allows allowed ensure ensures without within thing things way
existing changed change changes signature signatures def self cls class classmethod
staticmethod return returns method methods function functions call calls called python
update updated fix fixed enable implement handle provide accept require give show support
""".split())
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
STEM_CHARS = 6


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def _stem(word: str) -> str:
    """A crude stem: the singular, cut to ``STEM_CHARS`` letters (``protected``, ``protection``)."""
    return _singular(word)[:STEM_CHARS]


def text_words(text: str) -> set[str]:
    """Stems of the words of ``text``; an identifier also gives its parts.

    ``URLInfo`` gives ``urlinf``, ``url`` and ``info``; ``get_missing_deps``
    gives ``get_mi``, ``missin`` and ``dep``. Stop words, numbers and words of
    one letter are left out.
    """
    words = set()
    for token in _IDENTIFIER.findall(text):
        parts = [part for piece in token.split("_") for part in _CAMEL.findall(piece)]
        for word in {token.lower(), *(part.lower() for part in parts)}:
            if (len(word) >= 2 and not word.isdigit() and word not in _STOP_WORDS
                    and _singular(word) not in _STOP_WORDS):
                words.add(_stem(word))
    return words


@dataclass
class RequestPart:
    """A part of the request: a section under a heading, or a line before the first one."""

    text: str
    line: int  # its first line, counting from 1
    section: bool  # a whole section (heading and body)


def _request_lines(request: str) -> list[tuple[int, str, bool]]:
    """``(number, line, is_heading)``; a heading is never inside a fenced block."""
    lines, fenced = [], False
    for number, line in enumerate(request.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append((number, line, False))
            continue
        lines.append((number, line, not fenced and bool(re.match(r"#{1,6}\s", line))))
    return lines


def _line_parts(lines: list[tuple[int, str, bool]]) -> list[RequestPart]:
    """Each non-empty line is a part; a fenced code block is one part."""
    parts: list[RequestPart] = []
    block: list[tuple[int, str]] = []
    for number, line, _heading in lines:
        if block:
            block.append((number, line))
            if line.lstrip().startswith("```"):
                parts.append(RequestPart("\n".join(text for _, text in block), block[0][0],
                                         False))
                block = []
        elif line.lstrip().startswith("```"):
            block = [(number, line)]
        elif line.strip():
            parts.append(RequestPart(line, number, False))
    if block:
        parts.append(RequestPart("\n".join(text for _, text in block), block[0][0], False))
    return parts


def request_parts(request: str) -> list[RequestPart]:
    """The request cut into parts that can be matched against draft items.

    Every section under a Markdown heading (outside fenced code) is one part,
    with its fenced blocks; the text before the first heading is cut into its
    lines, so that a list of changes (a release's notes) gives one part per
    change.
    """
    lines = _request_lines(request)
    heads = [position for position, (_, _, heading) in enumerate(lines) if heading]
    first = heads[0] if heads else len(lines)
    parts = _line_parts(lines[:first])
    for start, end in zip(heads, [*heads[1:], len(lines)]):
        text = "\n".join(line for _, line, _ in lines[start:end]).strip()
        parts.append(RequestPart(text, lines[start][0], True))
    return parts


@dataclass
class ItemText:
    """The text level 2 is chosen from for one draft item, and where it came from."""

    item: str
    text: str
    parts: list[int] = field(default_factory=list)  # first lines of the request parts in it
    words: list[str] = field(default_factory=list)  # the item's words that chose them
    keys: list[str] = field(default_factory=list)  # all of the item's own words


def item_texts(request: str, items: list[tuple[str, str]]) -> dict[str, ItemText]:
    """For every ``(id, title and description)``: its text and the request parts about it.

    A part is about an item when it has one of the item's words, at least half
    as many of them as the item with the most. An item's words are the stems
    of its title and description (:func:`text_words`) that are neither in more
    than a third of the items nor in more than half of the parts (``dvc``,
    ``DataFrame``). A section that this gives to more than half of the items
    (at least three) is cut into its lines, which are matched again.
    """
    parts = request_parts(request)
    part_words = [text_words(part.text) for part in parts]
    own = {item_id: text_words(text) for item_id, text in items}
    in_items: Counter = Counter(word for words in own.values() for word in words)
    in_parts: Counter = Counter(word for words in part_words for word in words)
    many_items = max(1, len(items) // 3)
    many_parts = max(2, len(parts) // 2)
    keys = {item_id: {word for word in words if in_items[word] <= many_items
                      and in_parts[word] <= many_parts}
            for item_id, words in own.items()}

    def owners(words: set[str]) -> list[str]:
        scores = {item_id: len(key & words) for item_id, key in keys.items()}
        best = max(scores.values(), default=0)
        return [item_id for item_id, score in scores.items()
                if score >= 1 and 2 * score >= best]

    chosen: dict[str, list[RequestPart]] = {item_id: [] for item_id, _ in items}
    for part, words in zip(parts, part_words):
        found = owners(words)
        if part.section and len(items) >= 3 and len(found) > len(items) // 2:
            lines = _line_parts(_request_lines(part.text))
            for line in lines:
                for item_id in owners(text_words(line.text)):
                    chosen[item_id].append(RequestPart(line.text, part.line + line.line - 1,
                                                       False))
            continue
        for item_id in found:
            chosen[item_id].append(part)
    texts = {}
    for item_id, text in items:
        picked = chosen[item_id]
        body = "\n\n".join([text, *(part.text for part in picked)])
        matched = sorted({word for part in picked for word in keys[item_id]
                          & text_words(part.text)})
        texts[item_id] = ItemText(item_id, body, [part.line for part in picked], matched,
                                  sorted(keys[item_id]))
    return texts


def read_source_files(root: Path, index: RepoIndex) -> list[_File]:
    """The parsed source files of the checkout, read once for every item's outline."""
    return [_read(Path(root), path) for path in sorted(index.files) if is_source_file(path)]


def _collapsed_level1(files: list[_File], keep: set[str]) -> tuple[str, list[str]]:
    """Level 1 with every directory that holds no file of ``keep`` as one line.

    The highest such directory is the one line (``dask/array/`` with its
    subdirectories), and only when the line is shorter than the files it
    replaces. Returns the text and the collapsed directories.
    """
    def parents(path: str) -> list[str]:
        parts = PurePosixPath(path).parts[:-1]
        return ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]

    kept_dirs = {folder for path in keep for folder in parents(path)}
    lines: list[str] = []
    collapsed: list[str] = []
    position = 0
    while position < len(files):
        file = files[position]
        folder = next((folder for folder in parents(file.path) if folder not in kept_dirs),
                      None)
        if folder is None:
            lines.append(level1_line(file))
            position += 1
            continue
        members = []
        while (position < len(files)
               and files[position].path.startswith(folder + "/")):
            members.append(files[position])
            position += 1
        line = (f"- {folder}/ ({len(members)} source files, "
                f"{sum(member.line_count for member in members):,} lines; none in level 2)")
        full = [level1_line(member) for member in members]
        if len(line) < len("\n".join(full)):
            lines.append(line)
            collapsed.append(folder + "/")
        else:
            lines.extend(full)
    return "\n".join(lines), collapsed


@dataclass
class ItemOutline(TwoLevelOutline):
    """An item's two-level outline: also its text and the directories collapsed in level 1."""

    item_text: ItemText | None = None
    collapsed: list[str] = field(default_factory=list)
    level1_full_chars: int = 0
    shortened: list[str] = field(default_factory=list)  # level-2 files shown focused

    def report(self) -> dict:
        """What the plan report records about one item's outline."""
        fields = super().report()
        fields.update({
            "outline_chars": self.chars,
            "outline_level1_full_chars": self.level1_full_chars,
            "outline_level1_collapsed": list(self.collapsed),
            "outline_level2_shortened": list(self.shortened),
            "item_text_chars": len(self.item_text.text) if self.item_text else 0,
            "item_request_lines": list(self.item_text.parts) if self.item_text else [],
            "item_words": list(self.item_text.words) if self.item_text else [],
        })
        return fields


def _named_files(files: list[_File], keys: set[str], taken: set[str]) -> list[Level2File]:
    """Rule (d), per item: files whose name is one of ``keys``.

    ``keys`` are the item's words that also chose a part of the request
    (:attr:`ItemText.words`): words both the draft item and the request use.
    ``gc`` gives ``dvc/command/gc.py`` and ``dvc/repo/gc.py``; an
    ``__init__.py`` is named by its directory. A file whose directories also
    have such words comes first.
    """
    found = []
    for file in files:
        if file.path in taken:
            continue
        parts = PurePosixPath(file.path).parts
        name = PurePosixPath(parts[-1]).stem
        if name == "__init__":
            if len(parts) < 2:
                continue
            name = parts[-2]
        if _stem(name.lower()) in keys:
            near = len({_stem(part.lower()) for part in parts[:-1]} & keys)
            found.append((-near, file.path, name))
    return [Level2File(path, "d", f"its name {name!r} is a word of the item")
            for _, path, name in sorted(found)]


def _focused_section(file: _File, keys: set[str]) -> str | None:
    """A file with only its definitions whose names share a word with the item.

    A class is shown when its name or one of its members' names does, with
    those members only. None when no name does.
    """
    if file.entries is None:
        return None

    def matches(name: str) -> bool:
        return bool(text_words(name) & keys)

    lines, total = [], 0
    for entry in file.entries:
        members = [child for child in entry.children if child.kind == "def"]
        total += 1 + len(members)
        kept = [child for child in members if matches(child.name)]
        fields = [name for child in entry.children if child.kind == "fields"
                  for name in child.names if matches(name)]
        if not (matches(entry.name) or kept or fields):
            continue
        lines.append(_line(entry, 1))
        if fields:
            lines.append(f"  fields: {', '.join(fields)}")
        lines.extend(_line(child, 1, "  ") for child in kept)
    if not lines:
        return None
    shown = len(lines) - sum(1 for line in lines if line.startswith("  fields:"))
    head = (f"### {file.path} ({file.line_count} lines; shortened: only the {shown} of its "
            f"{total} definitions whose names share a word with your item; read the file for "
            "the others)")
    return "\n".join([head, *lines])


def build_item_outline(files: list[_File], imports: TestImports, text: ItemText,
                       budget: int = ITEM_LEVEL2_BUDGET,
                       collapse_chars: int = LEVEL1_COLLAPSE_CHARS) -> ItemOutline:
    """The two-level outline of one draft item, from the files of :func:`read_source_files`.

    Level 2 is chosen from ``text`` by the rules (a) and (b) of
    :func:`choose_level2`, then (d) :func:`_named_files`, and filled to
    ``budget``; a file of these rules that does not fit is shown focused
    (:func:`_focused_section`) when that fits. Then (c): the import neighbours
    of the files shown, the nearest first. When level 1 is longer than
    ``collapse_chars``, the directories that hold no level-2 file are
    collapsed into one line each.
    """
    if not files:
        return ItemOutline("(no Python source files)\n", item_text=text)
    by_path = {file.path: file for file in files}
    keys = set(text.keys)
    seeds = [entry for entry in choose_level2(text.text, files, imports)
             if entry.rule in ("a", "b")]
    for entry in seeds:
        if entry.rule == "a":
            entry.why = ITEM_RULES["a"]
    seeds += _named_files(files, set(text.words), {entry.path for entry in seeds})
    sections, chosen, cut, shortened = [], [], [], []
    used = 0

    def place(entry: Level2File, focus: bool) -> None:
        nonlocal used
        section = _section(by_path[entry.path], 1).replace("## ", "### ", 1)
        if focus and used + len(section) + 2 > budget:
            focused = _focused_section(by_path[entry.path], keys)
            if focused is not None and used + len(focused) + 2 <= budget:
                section = focused
                shortened.append(entry.path)
        entry.chars = len(section) + 2
        if used + entry.chars > budget:
            cut.append(entry)
            return
        used += entry.chars
        chosen.append(entry)
        sections.append(section)

    for entry in seeds:
        place(entry, True)
    full = "\n".join(level1_line(file) for file in files)
    collapse = len(full) > collapse_chars and bool(chosen)  # no level 2: level 1 is the map
    # On a long level 1, a neighbour outside the directories of the files above
    # would keep a whole directory from collapsing for a few lines of level 2.
    near = {"/".join(PurePosixPath(entry.path).parts[:depth]) for entry in chosen
            for depth in range(len(PurePosixPath(entry.path).parts))}
    for entry in _neighbour_files([entry.path for entry in chosen], imports, set(by_path),
                                  {entry.path for entry in seeds}, near_first=True):
        if collapse and "/".join(PurePosixPath(entry.path).parts[:-1]) not in near:
            continue
        place(entry, False)
    level1, collapsed = full, []
    if collapse:
        level1, collapsed = _collapsed_level1(files, {item.path for item in chosen})
    level2 = "\n\n".join(sections) if sections else "(no file was chosen)"
    intro = ITEM_OUTLINE_INTRO_COLLAPSED if collapsed else ITEM_OUTLINE_INTRO
    heading = ("## Level 1: every source file" + (" (directories without level-2 files "
                                                   "collapsed)" if collapsed else ""))
    outline = "\n\n".join([intro, f"{heading}\n\n{level1}",
                           "## Level 2: details of some files\n\n" + level2]) + "\n"
    return ItemOutline(outline, len(level1), len(level2), len(files), chosen, cut,
                       item_text=text, collapsed=collapsed, level1_full_chars=len(full),
                       shortened=shortened)


def item_two_level_instructions(prompt: str) -> str:
    """The planner's instructions with the outline paragraph for an item's outline."""
    if FULL_OUTLINE_PARAGRAPH not in prompt:
        raise ValueError("the planner's instructions have no paragraph about the outline "
                         "to replace")
    return prompt.replace(FULL_OUTLINE_PARAGRAPH, ITEM_TWO_LEVEL_PARAGRAPH, 1)
