"""The split gate: decide "split or single" with few tokens, before the full plan.

``python -m aqours_code.taskgraph gate`` asks whether a request is worth the
full plan (step 2 of the planner) or one agent should do it. It calls the
model once (plus step 1 when no draft can be reused) and plans nothing.
``plan --gate`` runs the same gate (:class:`GateRun`) on its own Step 1 draft
(:mod:`planner_v3`); ``plan`` without ``--gate`` does not.

Levels, from cheap to less cheap:

0. **Sizes** (no model): the characters of every source file of the
   repository at the base commit; tokens are estimated as characters / 4.
1. **Features**: the draft items of the planner's step 1, reused from an
   earlier plan (``<graph.json>.logs/draft/worker_<n>.json``, else the
   report's ``draft_items``); without one, step 1 runs as in ``plan``.
2. **Location** (one model call, no tools): a program search first takes the
   identifiers of each feature's title and description (names in backticks,
   words that look like paths or modules, CamelCase and snake_case words) and
   finds the source files that define those names or that those paths and
   modules name (the rules of :mod:`outline_levels`). The model gets the
   request, the features, the level-1 outline (every source file's top-level
   names) and the search results, and answers with the files each feature
   modifies and at most :data:`MAX_REFERENCE` it must read. Paths that are not
   in the repository are dropped and recorded. A prompt over
   :data:`LOCATE_INPUT_LIMIT` tokens has its outline cut, and the cut is
   recorded.

The decision (:func:`decide`) is a conservative rule of three zones, with
fixed thresholds:

    M = B0 + tokens of every file the features involve (modify and reference,
        each file once)

W is the context one agent works in (:func:`agent_window`). Two features are
connected when both modify one source file other than a package's
``__init__.py`` (it usually only gains an export); test files do not connect
either. Connected features form groups (union-find), and the concentration is
the largest group's number of features over the number of features.

- M ≤ W/2, or concentration ≥ 3/4: ``single`` (zone ``clear_single``);
- M > W, and no two features share a modified file or concentration ≤ 1/2:
  ``split`` (zone ``clear_split``);
- otherwise ``single`` (zone ``unclear``): the default, since a wrong split
  costs more than a wrong single.

A file of more than W tokens is listed as oversized; that does not change the
decision.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PurePosixPath

from . import gitops
from .cleanup import remove_tree
from .outline import _File, _read, is_source_file
from .outline_levels import (COMMON_DEFINED_IN, MIN_NAME_CHARS, _path_forms, defined_names,
                             level1_line, module_name)
from .plan_budget import PlanBudget
from .planner import (DRAFT_ATTEMPTS, DraftError, DraftItem, DraftItems, GraphInfo,
                      _agent_dict, _generator, _issue_dict, extract_json_block, file_sizes,
                      parse_items, report_path_for, run_draft_stage)
from .repo_index import RepoIndex, build_index
from .schema import Graph
from .single import SINGLE_INSTRUCTION, single_graph
from .validate import Issue
from .workers import Worker, WorkerRequest, WorkerResult, write_json_atomic

# v2: the decision of three zones, W scaled to the compaction trigger
GATE_VERSION = "gate-v2"

# The thresholds of the decision are fixed: once chosen they are not adjusted
# to any task's result.
B0 = 8_000  # tokens: the fixed part of every model call (instructions, tools, request)
# W at the default compaction trigger: the mean context of one agent observed
# in runs with AQOURS_CODE_COMPACT_TRIGGER_TOKENS = 80000. agent_window()
# scales it to the trigger the agents run with.
W_AT_DEFAULT_TRIGGER = 37_000  # tokens
DEFAULT_COMPACT_TRIGGER = 80_000  # tokens, as in aqours_code.config
COMPACT_TRIGGER_ENV = "AQOURS_CODE_COMPACT_TRIGGER_TOKENS"
# concentration at or above which the request stays with one agent
CLEAR_SINGLE_CONCENTRATION = Fraction(3, 4)
# concentration at or below which a request over W is split
CLEAR_SPLIT_CONCENTRATION = Fraction(1, 2)

CHARS_PER_TOKEN = 4
MAX_REFERENCE = 3
LOCATE_INPUT_LIMIT = 80_000  # estimated tokens of the one location prompt
SEARCH_FILES_PER_FEATURE = 10
DEFAULT_GATE_TIMEOUT_S = 600.0
LOCATE_PROMPT_PATH = Path(__file__).resolve().parent / "gate_locate_prompt.md"
# A relative request path in a plan report is relative to this repository.
AQOURS_ROOT = Path(__file__).resolve().parents[2]
# The location is one model call: its budget allows no second one.
LOCATE_BUDGET = {"max_calls": 1, "max_input_tokens": None}

SINGLE = "single"
SPLIT = "split"
CLEAR_SINGLE = "clear_single"
CLEAR_SPLIT = "clear_split"
UNCLEAR = "unclear"


class GateError(Exception):
    """The inputs of the gate cannot be read."""


# ── W: the context of one agent ──

@dataclass(frozen=True)
class Window:
    """W and the compaction trigger it was scaled from."""

    tokens: int
    trigger: int
    source: str  # "environment", ".env" or "default"
    note: str = ""  # a value that could not be used

    def to_dict(self) -> dict:
        return {"W": self.tokens, "half_W": half(self.tokens),
                "compact_trigger_tokens": self.trigger, "trigger_source": self.source,
                "note": self.note or None}


def window_for_trigger(trigger: int) -> int:
    """W = round(37000 × trigger / 80000)."""
    return round(W_AT_DEFAULT_TRIGGER * trigger / DEFAULT_COMPACT_TRIGGER)


def half(w: int) -> int | float:
    """W/2, a whole number when W is even."""
    return w // 2 if w % 2 == 0 else w / 2


def _dotenv_value(path: Path, name: str) -> str | None:
    """The value of ``name`` in a ``.env`` file (the last assignment), or None."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    value = None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != name:
            continue
        rest = rest.strip()
        if rest[:1] in ("'", '"') and rest.endswith(rest[:1]) and len(rest) > 1:
            rest = rest[1:-1]
        else:
            rest = rest.split(" #", 1)[0].strip()
        value = rest
    return value


def agent_window(environ: Mapping[str, str] | None = None,
                 dotenv: Path | None = None) -> Window:
    """W for the compaction trigger the agents run with.

    The trigger is ``AQOURS_CODE_COMPACT_TRIGGER_TOKENS`` as the agents read
    it (``aqours_code.config``): from the environment, else from the ``.env``
    of the Aqours-code root, else 80000. A value that is not a positive whole
    number counts as 80000 and is noted.
    """
    environ = os.environ if environ is None else environ
    dotenv = AQOURS_ROOT / ".env" if dotenv is None else dotenv
    raw, source = environ.get(COMPACT_TRIGGER_ENV), "environment"
    if raw is None:
        raw, source = _dotenv_value(dotenv, COMPACT_TRIGGER_ENV), ".env"
    if raw is None:
        return Window(W_AT_DEFAULT_TRIGGER, DEFAULT_COMPACT_TRIGGER, "default")
    try:
        trigger = int(raw)
    except ValueError:
        trigger = 0
    if trigger <= 0:
        return Window(W_AT_DEFAULT_TRIGGER, DEFAULT_COMPACT_TRIGGER, "default",
                      f"{COMPACT_TRIGGER_ENV}={raw!r} in the {source} is not a positive whole "
                      f"number; {DEFAULT_COMPACT_TRIGGER} was used")
    return Window(window_for_trigger(trigger), trigger, source)


def estimate_tokens(chars: int) -> int:
    """Tokens of ``chars`` characters, estimated as characters / 4 (rounded up)."""
    return math.ceil(chars / CHARS_PER_TOKEN)


def file_chars(root: Path, path: str) -> int:
    """Characters of a file of the checkout (UTF-8, undecodable bytes replaced)."""
    try:
        return len((Path(root) / path).read_bytes().decode("utf-8", errors="replace"))
    except OSError:
        return 0


# ── level 2: identifiers and the program search ──

@dataclass(frozen=True)
class Identifier:
    """A name taken from a feature's text, and what it looks like."""

    text: str
    kind: str  # "quoted" (in backticks), "path", "module", "camel", "snake"

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind}


_QUOTED = re.compile(r"`+([^`\n]+?)`+")
# Outside backticks a path has a slash and a file extension (``pkg/mod.py``),
# or is a bare ``name.py``: ``d/h/m/s`` and ``and/or`` are words.
_PATH = re.compile(r"(?<![\w./\\-])(?:(?:[\w.-]+[/\\])+[\w-]*\.[A-Za-z]\w*|[\w-]+\.py)"
                   r"(?![\w/\\])")
_DOTTED = re.compile(r"(?<![\w./])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_WORD = re.compile(r"(?<![\w])[A-Za-z_]\w*")


def normalise_path(text: str) -> str:
    """``.\\pkg\\mod.py`` -> ``pkg/mod.py``."""
    path = str(text).strip().strip("`'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_pathlike(text: str) -> bool:
    return bool(re.fullmatch(r"[\w./\\-]+", text)) and ("/" in text or "\\" in text
                                                         or text.endswith(".py"))


def _is_camel(word: str) -> bool:
    """``JobStore``, ``camelCase``, ``HTTPRemote``; not ``Duration`` or ``URL``."""
    return ("_" not in word and any(ch.islower() for ch in word)
            and any(ch.isupper() for ch in word[1:]))


def _is_snake(word: str) -> bool:
    return "_" in word and any(ch.isalnum() for ch in word)


def _dotted_ok(text: str) -> bool:
    """Leave out ``e.g`` and ``i.e``: every part of one letter."""
    return any(len(part) > 1 for part in text.split("."))


def extract_identifiers(text: str) -> list[Identifier]:
    """The identifiers of a feature's text, each once, in the order they appear.

    In backticks every name counts (a dotted name also as a module, a path as
    a path); outside them only words that look like paths (with a file
    extension), dotted modules, CamelCase or snake_case names. Plain words
    outside backticks are English.
    """
    found: dict[str, Identifier] = {}

    def add(value: str, kind: str) -> None:
        value = value.strip().rstrip(".,:;")
        if value and value not in found:
            found[value] = Identifier(value, kind)

    for span in _QUOTED.findall(text):
        span = span.strip()
        if _is_pathlike(span):
            add(normalise_path(span), "path")
            continue
        for token in re.findall(r"[A-Za-z_][\w.]*", span):
            token = token.strip(".")
            if "." in token and _dotted_ok(token):
                add(token, "module")
            for part in token.split("."):
                if part:
                    add(part, "quoted")
    rest = _QUOTED.sub(" ", text)
    for match in _PATH.finditer(rest):
        add(normalise_path(match.group().rstrip(".")), "path")
    rest = _PATH.sub(" ", rest)
    for match in _DOTTED.finditer(rest):
        if _dotted_ok(match.group()):
            add(match.group(), "module")
    for match in _WORD.finditer(rest):
        word = match.group()
        if _is_snake(word):
            add(word, "snake")
        elif _is_camel(word):
            add(word, "camel")
    return list(found.values())


def definitions(files: list[_File]) -> dict[str, set[str]]:
    """Name -> the source files defining it: classes, functions, methods, module names."""
    definers: dict[str, set[str]] = {}
    for file in files:
        names = defined_names(file) | {entry.name for entry in file.entries or []
                                       if entry.kind == "assign"}
        for name in names:
            definers.setdefault(name, set()).add(file.path)
    return definers


def module_paths(files: list[_File]) -> dict[str, str]:
    """Dotted module name (at least two parts) -> its source file."""
    modules: dict[str, str] = {}
    for file in files:
        name = module_name(file.path)
        if name.count(".") >= 1:
            modules.setdefault(name, file.path)
    return modules


def _files_named(text: str, paths: list[str]) -> list[str]:
    """Source files whose path is ``text`` or ends with ``/text``."""
    if "/" not in text and text == "__init__.py":
        return []
    return [path for path in paths
            if any(form == text or form.endswith("/" + text) for form in _path_forms(path))]


def _module_named(text: str, modules: dict[str, str]) -> tuple[str, str] | None:
    """The file of the longest module prefix of ``text`` (``pkg.mod.Class`` -> pkg/mod.py)."""
    parts = text.split(".")
    for end in range(len(parts), 1, -1):
        prefix = ".".join(parts[:end])
        if prefix in modules:
            return modules[prefix], prefix
    return None


@dataclass
class SearchHit:
    """A source file the search found for a feature, and why."""

    path: str
    why: list[str]

    def to_dict(self) -> dict:
        return {"path": self.path, "why": self.why}


@dataclass
class FeatureSearch:
    """What the program search found for one feature."""

    identifiers: list[Identifier]
    hits: list[SearchHit]
    common: list[str] = field(default_factory=list)  # names defined in too many files
    cut: int = 0  # files found beyond SEARCH_FILES_PER_FEATURE, left out

    def to_dict(self) -> dict:
        return {"identifiers": [ident.to_dict() for ident in self.identifiers],
                "files": [hit.to_dict() for hit in self.hits],
                "common": self.common, "cut": self.cut}


def search_feature(text: str, files: list[_File], definers: dict[str, set[str]] | None = None,
                   modules: dict[str, str] | None = None,
                   limit: int = SEARCH_FILES_PER_FEATURE) -> FeatureSearch:
    """Find the source files a feature's text points at.

    Files named by a path or module come first, then files by the number of
    the feature's names they define. A name is left out when it is shorter
    than ``MIN_NAME_CHARS`` or a dunder name, and recorded as common when more
    than ``COMMON_DEFINED_IN`` files define it (as in :mod:`outline_levels`).
    """
    definers = definers if definers is not None else definitions(files)
    modules = modules if modules is not None else module_paths(files)
    paths = [file.path for file in files]
    identifiers = extract_identifiers(text)
    reasons: dict[str, list[str]] = {}
    named: set[str] = set()
    common: list[str] = []
    for ident in identifiers:
        if ident.kind == "path":
            for path in _files_named(ident.text, paths):
                reasons.setdefault(path, []).append(f"path {ident.text}")
                named.add(path)
            continue
        if ident.kind == "module":
            found = _module_named(ident.text, modules)
            if found is not None:
                reasons.setdefault(found[0], []).append(f"module {found[1]}")
                named.add(found[0])
            continue
        name = ident.text
        if len(name) < MIN_NAME_CHARS or (name.startswith("__") and name.endswith("__")):
            continue
        defining = definers.get(name)
        if not defining:
            continue
        if len(defining) > COMMON_DEFINED_IN:
            common.append(name)
            continue
        for path in sorted(defining):
            reasons.setdefault(path, []).append(f"defines {name}")
    order = sorted(reasons, key=lambda path: (path not in named, -len(reasons[path]), path))
    hits = [SearchHit(path, list(dict.fromkeys(reasons[path]))) for path in order[:limit]]
    return FeatureSearch(identifiers, hits, sorted(set(common)), max(0, len(order) - limit))


# ── level 2: the location prompt ──

def load_locate_prompt() -> str:
    """The instructions of the location call."""
    return LOCATE_PROMPT_PATH.read_text(encoding="utf-8")


def _features_section(items: list[DraftItem]) -> str:
    lines = [f"- {item.id}: **{item.title}**" + (f". {item.description.strip()}"
                                                 if item.description.strip() else "")
             for item in items]
    return ("# Features\n\nThe pieces of work in the request, listed from the request text "
            "alone.\n\n" + "\n".join(lines))


def _outline_section(lines: list[str], omitted: int) -> str:
    text = ("# Repository outline\n\nEvery source file with its number of lines and the "
            "names of its top-level classes and functions.\n\n"
            + ("\n".join(lines) if lines else "(no Python source files)"))
    if omitted:
        text += (f"\n\n(Outline cut to fit: {omitted} more source files are not listed; "
                 "the files of the search results are all listed.)")
    return text


def _search_section(items: list[DraftItem], searches: Mapping[str, FeatureSearch]) -> str:
    parts = ["# Search results\n\nFor each feature, the source files that define a name its "
             "text uses, or whose path or module it names (a program search: a hint, not "
             "the answer)."]
    for item in items:
        search = searches.get(item.id)
        lines = [f"## Feature {item.id}: {item.title}"]
        if search is None or not search.hits:
            lines.append("- (no file found)")
        else:
            lines += [f"- {hit.path}: {'; '.join(hit.why)}" for hit in search.hits]
            if search.cut:
                lines.append(f"- ... and {search.cut} more files")
        if search is not None and search.common:
            lines.append(f"- names defined in too many files to search: "
                         f"{', '.join(search.common)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _assemble(request: str, items: list[DraftItem], outline_lines: list[str],
              searches: Mapping[str, FeatureSearch], omitted: int) -> str:
    parts = [load_locate_prompt().rstrip(), "# The request\n\n" + request.strip(),
             _features_section(items), _outline_section(outline_lines, omitted),
             _search_section(items, searches)]
    return "\n\n".join(parts) + "\n"


@dataclass
class LocatePrompt:
    """The location prompt and what had to be cut to fit the input limit."""

    text: str
    outline_files: int
    omitted: list[str] = field(default_factory=list)
    limit: int = LOCATE_INPUT_LIMIT

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(len(self.text))

    @property
    def over_limit(self) -> bool:
        """True when even the cut prompt is over the limit."""
        return self.estimated_tokens > self.limit

    def report(self) -> dict:
        return {"prompt_chars": len(self.text), "prompt_tokens_estimate": self.estimated_tokens,
                "input_limit": self.limit, "outline_files": self.outline_files,
                "outline_cut": bool(self.omitted), "outline_omitted": self.omitted,
                "over_limit": self.over_limit}


def build_locate_prompt(request: str, items: list[DraftItem], files: list[_File],
                        searches: Mapping[str, FeatureSearch],
                        limit: int = LOCATE_INPUT_LIMIT) -> LocatePrompt:
    """The prompt of the location call, its outline cut when over ``limit`` tokens.

    Cutting keeps the outline lines of the files in the search results, then
    other files in path order while they fit; the lines stay in path order.
    """
    lines = {file.path: level1_line(file) for file in files}
    text = _assemble(request, items, list(lines.values()), searches, 0)
    if estimate_tokens(len(text)) <= limit:
        return LocatePrompt(text, len(lines), [], limit)
    searched = {hit.path for search in searches.values() for hit in search.hits}
    kept = {path for path in lines if path in searched}
    others = [path for path in lines if path not in kept]
    base = _assemble(request, items, [lines[path] for path in lines if path in kept],
                     searches, len(others))
    room = limit * CHARS_PER_TOKEN - len(base)
    for path in others:
        cost = len(lines[path]) + 1
        if cost > room:
            break
        room -= cost
        kept.add(path)
    omitted = [path for path in lines if path not in kept]
    text = _assemble(request, items, [lines[path] for path in lines if path in kept],
                     searches, len(omitted))
    return LocatePrompt(text, len(kept), omitted, limit)


# ── level 2: reading the answer ──

@dataclass
class FeatureFiles:
    """The files of one feature: those it modifies and those it must read."""

    id: str
    title: str
    modify: list[str] = field(default_factory=list)
    reference: list[str] = field(default_factory=list)


@dataclass
class LocateAnswer:
    """The checked answer of the location call."""

    features: list[FeatureFiles]
    dropped: list[dict] = field(default_factory=list)  # {feature, role, path, why}
    notes: list[str] = field(default_factory=list)


def _as_id(value) -> str:
    return str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""


def _path_list(value, feature: str, role: str, notes: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        notes.append(f"feature {feature}: {role} is not a list; left out")
        return []
    paths = []
    for entry in value:
        if not isinstance(entry, str):
            notes.append(f"feature {feature}: a {role} entry is not a path; left out")
            continue
        path = normalise_path(entry)
        if path and path not in paths:
            paths.append(path)
    return paths


def parse_locations(answer: str, items: list[DraftItem], index: RepoIndex) -> LocateAnswer:
    """Read the location answer; drop (and record) paths that are not in the repository.

    A feature without an entry modifies nothing (noted). References beyond
    ``MAX_REFERENCE`` are dropped, and so is a reference the feature also
    modifies. Format problems raise :class:`DraftError`.
    """
    block = extract_json_block(answer)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise DraftError([Issue("FORMAT", [], f"the last ```json block is not valid JSON: "
                                             f"{exc}")]) from None
    entries = data.get("features") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise DraftError([Issue("FORMAT", [], "the answer has no `features` list")])
    known = {item.id for item in items}
    by_id: dict[str, dict] = {}
    notes: list[str] = []
    for entry in entries:
        feature = _as_id(entry.get("id")) if isinstance(entry, dict) else ""
        if not feature:
            notes.append("an entry without a feature id was left out")
        elif feature not in known:
            notes.append(f"entry for unknown feature {feature!r} was left out")
        elif feature in by_id:
            notes.append(f"feature {feature} has several entries; the first one counts")
        else:
            by_id[feature] = entry
    answer_out = LocateAnswer([], [], notes)
    for item in items:
        entry = by_id.get(item.id)
        if entry is None:
            notes.append(f"feature {item.id} has no entry: it modifies no file")
            answer_out.features.append(FeatureFiles(item.id, item.title))
            continue
        kept: dict[str, list[str]] = {}
        for role in ("modify", "reference"):
            kept[role] = []
            for path in _path_list(entry.get(role), item.id, role, notes):
                if not index.has_file(path):
                    answer_out.dropped.append({"feature": item.id, "role": role, "path": path,
                                               "why": "not in the repository"})
                elif role == "reference" and path in kept["modify"]:
                    continue
                elif role == "reference" and len(kept[role]) >= MAX_REFERENCE:
                    answer_out.dropped.append({"feature": item.id, "role": role, "path": path,
                                               "why": f"more than {MAX_REFERENCE} references"})
                else:
                    kept[role].append(path)
        answer_out.features.append(FeatureFiles(item.id, item.title, kept["modify"],
                                                kept["reference"]))
    return answer_out


# ── the decision ──

def connects(path: str) -> bool:
    """Whether two features modifying ``path`` are connected.

    A source file (a ``.py`` file that is not a test) connects them; a
    package's ``__init__.py`` does not (it usually only gains an export).
    """
    return is_source_file(path) and PurePosixPath(path).name != "__init__.py"


def feature_groups(features: list[FeatureFiles]) -> list[list[int]]:
    """Indexes of connected features (union-find), each group in feature order."""
    parent = list(range(len(features)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first: dict[str, int] = {}
    for index, feature in enumerate(features):
        for path in feature.modify:
            if not connects(path):
                continue
            if path in first:
                low, high = sorted((find(first[path]), find(index)))
                parent[high] = low
            else:
                first[path] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(features)):
        groups.setdefault(find(index), []).append(index)
    return sorted(groups.values(), key=lambda members: members[0])


def _files_of(feature: FeatureFiles, with_reference: bool) -> list[str]:
    return list(dict.fromkeys(feature.modify + (feature.reference if with_reference else [])))


def decide(features: list[FeatureFiles], tokens: Mapping[str, int], *,
           w: int = W_AT_DEFAULT_TRIGGER, b0: int = B0, with_reference: bool = True) -> dict:
    """The decision, its zone, its reason and every number it rests on.

    ``tokens`` gives each file's tokens (a missing file counts 0). With
    ``with_reference`` False only the modified files count in M; the groups
    always come from the modified files.
    """
    def entries(paths: list[str]) -> list[dict]:
        return [{"path": path, "tokens": tokens.get(path, 0)} for path in paths]

    involved = list(dict.fromkeys(path for feature in features
                                  for path in _files_of(feature, with_reference)))
    code = sum(tokens.get(path, 0) for path in involved)
    m = b0 + code
    groups = []
    for members in feature_groups(features):
        counts: dict[str, int] = {}
        for index in members:
            for path in dict.fromkeys(features[index].modify):
                if connects(path):
                    counts[path] = counts.get(path, 0) + 1
        groups.append({"features": [features[index].id for index in members],
                       "shared_files": sorted(path for path, count in counts.items()
                                              if count > 1)})
    groups.sort(key=lambda group: -len(group["features"]))  # ties keep the feature order
    largest = groups[0]["features"] if groups else []
    count = len(features)
    concentration = Fraction(len(largest), count) if count else Fraction(0)
    shared = any(len(group["features"]) > 1 for group in groups)
    held = (f"the largest group holds {len(largest)} of {count} features "
            f"({float(concentration):.2f})")
    if 2 * m <= w:
        decision, zone = SINGLE, CLEAR_SINGLE
        reason = f"M = {m:,} <= W/2 = {half(w):,}: one agent holds it"
    elif concentration >= CLEAR_SINGLE_CONCENTRATION:
        decision, zone = SINGLE, CLEAR_SINGLE
        reason = f"{held}, at least 3/4: the features concentrate on shared files"
    elif m > w and not shared:
        decision, zone = SPLIT, CLEAR_SPLIT
        reason = f"M = {m:,} > W = {w:,} and no two features share a modified file"
    elif m > w and concentration <= CLEAR_SPLIT_CONCENTRATION:
        decision, zone = SPLIT, CLEAR_SPLIT
        reason = f"M = {m:,} > W = {w:,} and {held}, at most 1/2"
    else:
        decision, zone = SINGLE, UNCLEAR
        where = (f"W/2 = {half(w):,} < M = {m:,} <= W = {w:,}" if m <= w
                 else f"M = {m:,} > W = {w:,}")
        reason = (f"between the clear zones ({where}; {held}): single by default, since a "
                  "wrong split costs more than a wrong single")
    return {
        "decision": decision,
        "zone": zone,
        "reason": reason,
        "settings": {"W": w, "B0": b0, "with_reference": with_reference},
        "material": {"M": m, "B0": b0, "code": code, "files": len(involved), "W": w,
                     "half_W": half(w)},
        "features": [{"id": feature.id, "title": feature.title,
                      "modify": entries(feature.modify), "reference": entries(feature.reference),
                      "tokens": sum(tokens.get(path, 0)
                                    for path in _files_of(feature, with_reference))}
                     for feature in features],
        "groups": groups,
        "shared": shared,
        "concentration": {"value": float(concentration),
                          "largest_group": largest, "of": f"{len(largest)}/{count}"},
        "oversized": [entry for entry in entries(involved) if entry["tokens"] > w],
    }


def features_from_gate(data: Mapping) -> tuple[list[FeatureFiles], dict[str, int]]:
    """The features and file tokens recorded in a gate output, for :func:`decide` again."""
    features, tokens = [], {}
    for feature in data.get("features") or []:
        modify = [entry["path"] for entry in feature.get("modify") or []]
        reference = [entry["path"] for entry in feature.get("reference") or []]
        for entry in [*(feature.get("modify") or []), *(feature.get("reference") or [])]:
            tokens[entry["path"]] = int(entry.get("tokens") or 0)
        features.append(FeatureFiles(str(feature["id"]), str(feature.get("title", "")),
                                     modify, reference))
    return features, tokens


# ── level 1: the draft ──

@dataclass
class GateDraft:
    """The features and where they came from."""

    items: DraftItems | None
    source: str  # "reused" (a plan's draft log), "report" (its draft_items), "step1"
    path: str | None = None
    usage: dict = field(default_factory=lambda: {"model_calls": 0, "input_tokens": 0,
                                                 "output_tokens": 0})
    rounds: list[dict] = field(default_factory=list)  # step 1 run by the gate
    errors: list[Issue] = field(default_factory=list)

    def report(self) -> dict:
        items = self.items
        return {"source": self.source, "path": self.path,
                "estimated_changed_lines": items.estimated_changed_lines if items else None,
                "items": [item.model_dump() for item in items.items] if items else [],
                "usage": self.usage, "rounds": self.rounds}


def _usage(result: WorkerResult) -> dict:
    return {"model_calls": result.model_calls, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens}


def reusable_draft(graph_path: Path) -> GateDraft | None:
    """The step-1 draft of the plan of ``graph_path``, if it kept one.

    First the plan's draft log (``<graph>.logs/draft/worker_<n>.json``, the
    first attempt that answered with a readable draft), then the
    ``draft_items`` of its report.
    """
    graph_path = Path(graph_path)
    log_dir = graph_path.with_name(f"{graph_path.name}.logs") / "draft"
    for attempt in range(1, DRAFT_ATTEMPTS + 1):
        path = log_dir / f"worker_{attempt}.json"
        try:
            result = WorkerResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
            items = parse_items(result.final_answer) if result.ok else None
        except (OSError, ValueError, TypeError, AttributeError, DraftError):
            continue
        if items is not None:
            return GateDraft(items, "reused", str(path), _usage(result))
    report_path = report_path_for(graph_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        items = DraftItems.model_validate({
            "estimated_changed_lines": report["estimated_changed_lines"],
            "items": report["draft_items"]})
    except (OSError, ValueError, TypeError, KeyError):
        return None
    stage1 = (report.get("stage_totals") or {}).get("stage1") or {}
    usage = {key: int(stage1.get(key) or 0)
             for key in ("model_calls", "input_tokens", "output_tokens")}
    return GateDraft(items, "report", str(report_path), usage)


# ── running ──

@dataclass
class GateOptions:
    """Parameters of one ``gate`` run.

    ``base_commit`` None reads the repository at its HEAD; ``draft_from`` is a
    plan's graph whose draft is reused when it has one; ``window`` None takes
    W from the compaction trigger (:func:`agent_window`).
    """

    request_path: Path
    repo: Path
    out: Path | None = None
    base_commit: str | None = None
    draft_from: Path | None = None
    from_report: Path | None = None
    timeout_s: float = DEFAULT_GATE_TIMEOUT_S
    input_limit: int = LOCATE_INPUT_LIMIT
    window: Window | None = None


def resolve_request(value: str) -> Path:
    """A request path from a plan report; a relative one is under the Aqours-code root."""
    path = Path(str(value).replace("\\", "/"))
    return path if path.is_absolute() else AQOURS_ROOT / path


def options_from_report(report_path: Path, out: Path | None = None,
                        timeout_s: float = DEFAULT_GATE_TIMEOUT_S) -> GateOptions:
    """Gate options from a plan report: its request, repository, base commit and draft."""
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GateError(f"plan report not found: {report_path}") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GateError(f"cannot read the plan report {report_path}: {exc}") from None
    if not isinstance(report, dict):
        raise GateError(f"{report_path} is not a JSON object")
    missing = [key for key in ("request", "repo", "base_commit") if not report.get(key)]
    if missing:
        raise GateError(f"{report_path} does not name the {', '.join(missing)} of the plan")
    name = report_path.name
    suffix = ".report.json"
    graph = (report_path.with_name(name[:-len(suffix)])
             if name.endswith(suffix) and len(name) > len(suffix) else report_path)
    return GateOptions(request_path=resolve_request(report["request"]),
                       repo=Path(report["repo"]), out=out, base_commit=report["base_commit"],
                       draft_from=graph, from_report=report_path, timeout_s=timeout_s)


@dataclass
class GateResult:
    """Outcome of :func:`run_gate`: ``ok`` when a decision was made."""

    ok: bool
    data: dict


def _draft(options: GateOptions, request: str, clone: Path, index: RepoIndex,
           worker: Worker, log_dir: Path) -> GateDraft:
    if options.draft_from is not None:
        reused = reusable_draft(options.draft_from)
        if reused is not None:
            return reused
    items, rounds, errors = run_draft_stage(request, file_sizes(clone, index), worker, clone,
                                            log_dir, options.timeout_s, PlanBudget())
    usage = {key: sum(entry["agent"][key] for entry in rounds)
             for key in ("model_calls", "input_tokens", "output_tokens")}
    return GateDraft(items, "step1", None, usage, rounds, list(errors))


@dataclass
class GateRun:
    """The gate's work on one checkout: sizes, the location call and the decision.

    ``gate`` and ``plan --gate`` share it: :meth:`measure` reads level 0,
    :meth:`build_prompt` searches and writes the location prompt, the caller
    makes the call, and :meth:`read_answer` checks the answer and decides.
    """

    window: Window
    input_limit: int = LOCATE_INPUT_LIMIT
    files: list[_File] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)
    level0: dict = field(default_factory=dict)
    searches: dict[str, FeatureSearch] = field(default_factory=dict)
    prompt: LocatePrompt | None = None
    result: WorkerResult | None = None
    located: LocateAnswer | None = None
    decision: dict = field(default_factory=dict)
    errors: list[Issue] = field(default_factory=list)

    def measure(self, clone: Path, index: RepoIndex) -> None:
        """Level 0: the characters of every source file of the checkout."""
        self.files = [_read(clone, path) for path in sorted(index.files)
                      if is_source_file(path)]
        self.sizes = {file.path: file_chars(clone, file.path) for file in self.files}
        self.level0 = {"source_files": len(self.files), "chars": sum(self.sizes.values()),
                       "tokens": sum(estimate_tokens(chars) for chars in self.sizes.values()),
                       "oversized": [{"path": path, "tokens": estimate_tokens(chars)}
                                     for path, chars in self.sizes.items()
                                     if estimate_tokens(chars) > self.window.tokens],
                       "sizes": self.sizes}

    def build_prompt(self, request: str, items: list[DraftItem]) -> LocatePrompt:
        """The program search and the location prompt."""
        definers, modules = definitions(self.files), module_paths(self.files)
        self.searches = {item.id: search_feature(f"{item.title}\n{item.description}",
                                                 self.files, definers, modules)
                         for item in items}
        self.prompt = build_locate_prompt(request, items, self.files, self.searches,
                                          self.input_limit)
        return self.prompt

    def read_answer(self, result: WorkerResult, items: list[DraftItem], index: RepoIndex,
                    clone: Path) -> None:
        """Check the answer of a call that answered (``result.ok``) and decide.

        A format problem goes to :attr:`errors` and leaves no decision.
        """
        self.result = result
        if not result.ok:
            return
        try:
            self.located = parse_locations(result.final_answer, items, index)
        except DraftError as exc:
            self.errors += exc.issues
            return
        involved = {path for feature in self.located.features
                    for path in feature.modify + feature.reference}
        tokens = {path: estimate_tokens(self.sizes[path] if path in self.sizes
                                        else file_chars(clone, path))
                  for path in involved}
        self.decision = decide(self.located.features, tokens, w=self.window.tokens)

    def usage(self) -> dict:
        """Model calls and tokens of the location call."""
        return _usage(self.result if self.result is not None else WorkerResult(ok=False))

    def output_fields(self) -> dict:
        """The fields of the gate output about the location and the decision."""
        decision, located, result, prompt = self.decision, self.located, self.result, self.prompt
        usage = self.usage()
        return {
            "ok": bool(decision),
            "errors": [_issue_dict(issue) for issue in self.errors],
            "decision": decision.get("decision"),
            "zone": decision.get("zone"),
            "reason": decision.get("reason"),
            "window": self.window.to_dict(),
            "constants": {"B0": B0, "W_at_default_trigger": W_AT_DEFAULT_TRIGGER,
                          "default_compact_trigger": DEFAULT_COMPACT_TRIGGER,
                          "clear_single_concentration": str(CLEAR_SINGLE_CONCENTRATION),
                          "clear_split_concentration": str(CLEAR_SPLIT_CONCENTRATION),
                          "chars_per_token": CHARS_PER_TOKEN, "max_reference": MAX_REFERENCE,
                          "locate_input_limit": self.input_limit, "fixed": True},
            "material": decision.get("material"),
            "features": decision.get("features", []),
            "groups": decision.get("groups", []),
            "shared": decision.get("shared"),
            "concentration": decision.get("concentration"),
            "oversized": decision.get("oversized", []),
            "dropped": located.dropped if located is not None else [],
            "notes": located.notes if located is not None else [],
            "search": {key: value.to_dict() for key, value in self.searches.items()},
            "locate": {**(prompt.report() if prompt is not None else {}),
                       "agent": _agent_dict(result) if result is not None else None,
                       "input_tokens": usage["input_tokens"],
                       "output_tokens": usage["output_tokens"],
                       "answer": result.final_answer if result is not None else None},
            "level0": self.level0,
        }


def run_gate(options: GateOptions, locate_worker: Worker,
             draft_stage_worker: Worker | None = None) -> GateResult:
    """Run the gate; write ``options.out`` when given.

    ``locate_worker`` makes the location call; ``draft_stage_worker`` runs
    step 1 when no draft can be reused (default: ``locate_worker``). Raises
    ``gitops.GitError`` / ``RuntimeError`` on git failures and ``OSError`` /
    ``UnicodeDecodeError`` for a request it cannot read.
    """
    started = time.monotonic()
    request = options.request_path.read_text(encoding="utf-8")
    repo = options.repo.resolve()
    base = gitops.head(repo, options.base_commit or "HEAD")
    model = locate_worker.describe().get("model")
    window = options.window if options.window is not None else agent_window()
    gate_run = GateRun(window, options.input_limit)
    draft: GateDraft | None = None
    tmp = Path(tempfile.mkdtemp(prefix="tg-gate-"))
    if options.out is not None:
        options.out.parent.mkdir(parents=True, exist_ok=True)
        log_root = options.out.with_name(f"{options.out.name}.logs")
    else:
        log_root = tmp / "logs"
    try:
        clone = tmp / "repo"
        gitops.clone_for_run(repo, clone, base)
        index = build_index(clone, base)
        gate_run.measure(clone, index)
        draft = _draft(options, request, clone, index, draft_stage_worker or locate_worker,
                       log_root / "draft")
        gate_run.errors += draft.errors
        if draft.items is not None:
            items = draft.items.items
            prompt = gate_run.build_prompt(request, items)
            result = locate_worker.run(WorkerRequest(
                node_id="gate-locate", attempt=1, prompt=prompt.text, workspace=clone,
                log_dir=log_root / "locate", timeout_s=options.timeout_s, repo_guard=True,
                model_budget=dict(LOCATE_BUDGET)))
            if not result.ok:
                gate_run.errors.append(Issue("AGENT", [], f"the location call failed "
                                                          f"({result.reason or 'worker_error'}): "
                                                          f"{result.error[-2000:]}"))
            gate_run.read_answer(result, items, index, clone)
    finally:
        remove_tree(tmp)
    locate_usage = gate_run.usage()
    draft_usage = draft.usage if draft is not None and draft.source == "step1" else \
        {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
    fields = gate_run.output_fields()
    data = {
        "gate_version": GATE_VERSION,
        "model": str(model) if model is not None else None,
        "request": str(options.request_path),
        "repo": str(repo),
        "base_commit": base,
        "from_report": str(options.from_report) if options.from_report else None,
        "draft_from": str(options.draft_from) if options.draft_from else None,
        "out": str(options.out) if options.out else None,
        "logs": str(log_root) if options.out else None,
        **{key: value for key, value in fields.items() if key not in ("search", "locate",
                                                                      "level0")},
        "draft": draft.report() if draft is not None else None,
        "search": fields["search"],
        "locate": fields["locate"],
        "level0": fields["level0"],
        "totals": {key: draft_usage[key] + locate_usage[key]
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "duration_s": round(time.monotonic() - started, 3),
    }
    if options.out is not None:
        write_json_atomic(options.out, data)
    return GateResult(ok=bool(gate_run.decision), data=data)


def locate_model_budget(plan_budget: Mapping | None) -> dict:
    """The location call's model budget inside a plan: one call, and the plan's limits.

    ``plan_budget`` is what is left of the plan (``PlanBudget.request_fields``),
    or None when the plan has no limits.
    """
    budget = dict(LOCATE_BUDGET)
    if plan_budget:
        if plan_budget.get("max_calls") is not None:
            budget["max_calls"] = min(budget["max_calls"], plan_budget["max_calls"])
        budget["max_input_tokens"] = plan_budget.get("max_input_tokens")
    return budget


# ── the single-agent graph of plan --gate ──

WHERE_TO_LOOK_TITLE = "Where to look (from a quick look at the repository; not verified)"
WHERE_TO_LOOK_LIMIT = 1500  # characters of the whole section
WHERE_TO_LOOK_NOTE = (
    "Before any planning, a quick automatic look at the repository suggested these files "
    "for each feature. It is rough: it may miss files or name wrong ones, so check them "
    "yourself before you rely on them.")


def _feature_line(feature: FeatureFiles) -> str:
    title = " ".join(feature.title.split()) or feature.id
    parts = []
    if feature.modify:
        parts.append(f"modify {', '.join(feature.modify)}")
    if feature.reference:
        parts.append(f"read {', '.join(feature.reference)}")
    return f"- {title}: {'; '.join(parts) if parts else 'no file found'}"


def where_to_look(features: list[FeatureFiles], limit: int = WHERE_TO_LOOK_LIMIT) -> str:
    """The hint section of the single-agent goal: one line per feature, files only.

    At most ``limit`` characters: lines that do not fit are left out and a
    last line says how many.
    """
    head = f"## {WHERE_TO_LOOK_TITLE}\n\n{WHERE_TO_LOOK_NOTE}\n"
    lines = [_feature_line(feature) for feature in features]
    text = head + "\n" + "\n".join(lines) if lines else head + "\n- (no feature)"
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for number, line in enumerate(lines):
        rest = len(lines) - number - 1
        cut = f"- ... {rest} more feature(s) left out to keep this section short"
        candidate = head + "\n" + "\n".join([*kept, line, cut] if rest else [*kept, line])
        if len(candidate) > limit:
            break
        kept.append(line)
    rest = len(lines) - len(kept)
    cut = f"- ... {rest} more feature(s) left out to keep this section short"
    text = head + "\n" + "\n".join([*kept, cut])
    return text[:limit]


def gate_goal(request: str, features: list[FeatureFiles]) -> str:
    """The single-agent goal with the hint section before the closing instruction."""
    return f"{request}\n\n{where_to_look(features)}\n\n{SINGLE_INSTRUCTION}"


def gate_single_graph(info: GraphInfo, repo: Path, features: list[FeatureFiles],
                      decision: Mapping, version: str) -> Graph:
    """The graph of ``plan --gate`` when the gate says single.

    The graph of the ``single`` command (one ``solve`` node, any file, the
    whole request, the same final checks), with the hint section in the
    goal, ``planner_version`` ``version`` and the gate's decision in the
    revision log.
    """
    graph = single_graph(info.request, repo, info.final_checks)
    data = graph.model_dump(mode="json", by_alias=True)
    data["base_commit"] = info.base_commit
    data["nodes"][0]["goal"] = gate_goal(info.request, features)
    data["generator"] = _generator(info, "none", version)
    data["revision_log"] = [{
        "action": "other", "nodes": [node["id"] for node in data["nodes"]],
        "reason": f"gate: {decision['decision']} ({decision['zone']}): {decision['reason']}"}]
    return Graph.model_validate(data)


def plan_report(data: Mapping, output: Path) -> dict:
    """The ``gate`` field of a plan report: the decision and what it rests on."""
    material = data.get("material") or {}
    return {
        "ok": data.get("ok", False),
        "errors": data.get("errors", []),
        "decision": data.get("decision"),
        "zone": data.get("zone"),
        "reason": data.get("reason"),
        "M": material.get("M"),
        "W": (data.get("window") or {}).get("W"),
        "window": data.get("window"),
        "concentration": data.get("concentration"),
        "features": [{"id": feature["id"], "title": feature["title"],
                      "modify": [entry["path"] for entry in feature["modify"]],
                      "reference": [entry["path"] for entry in feature["reference"]]}
                     for feature in data.get("features") or []],
        "locate_tokens": {key: (data.get("locate") or {}).get(key) or 0
                          for key in ("input_tokens", "output_tokens")},
        "output": str(output),
    }


def locate_worker() -> Worker:
    """The worker of the location call: the planner's child process, no file tools.

    It is the worker of step 1 (``planner_entry --stage draft``), with the same
    model configuration as ``plan``.
    """
    from .planner import draft_worker
    return draft_worker()


# ── output ──

def _file_text(entries: list[dict]) -> str:
    return ", ".join(f"{entry['path']} ({entry['tokens']:,})" for entry in entries) or "-"


def format_gate(data: Mapping) -> str:
    """The gate's result as readable text."""
    lines = []
    draft = data.get("draft") or {}
    if draft:
        where = f" from {draft['path']}" if draft.get("path") else ""
        lines.append(f"features: {len(draft.get('items') or [])} (draft: {draft['source']}"
                     f"{where})")
    level0 = data.get("level0") or {}
    if level0:
        lines.append(f"repository: {level0['source_files']} source files, "
                     f"{level0['tokens']:,} tokens (characters / {CHARS_PER_TOKEN})")
    locate = data.get("locate") or {}
    if locate.get("prompt_chars") is not None:
        cut = (f"outline cut: {len(locate['outline_omitted'])} files left out"
               if locate["outline_cut"] else "outline whole")
        over = "; still over the limit" if locate["over_limit"] else ""
        lines.append(f"location call: prompt about {locate['prompt_tokens_estimate']:,} tokens "
                     f"(limit {locate['input_limit']:,}; {cut}{over}); "
                     f"{locate['input_tokens']:,} in / {locate['output_tokens']:,} out")
    for issue in data.get("errors") or []:
        lines.append(f"error: [{issue['code']}] {issue['message']}")
    window = data.get("window") or {}
    if window:
        note = f"; {window['note']}" if window.get("note") else ""
        trigger = window["compact_trigger_tokens"]
        lines.append(f"W = {window['W']:,} (compaction trigger {trigger:,} from the "
                     f"{window['trigger_source']}{note}); W/2 = {window['half_W']:,}")
    material = data.get("material")
    if material:
        lines.append(f"M = B0 {material['B0']:,} + code {material['code']:,} "
                     f"({material['files']} files) = {material['M']:,} tokens")
        for feature in data["features"]:
            lines.append(f"  {feature['id']} {feature['title']}: {feature['tokens']:,} tokens; "
                         f"modify {_file_text(feature['modify'])}; "
                         f"reference {_file_text(feature['reference'])}")
        shared = [group for group in data["groups"] if group["shared_files"]]
        if shared:
            lines.append(f"groups: {len(data['groups'])}; sharing modified files:")
            lines += [f"  {', '.join(group['features'])}: {', '.join(group['shared_files'])}"
                      for group in shared]
        else:
            lines.append(f"groups: {len(data['groups'])} (no two features share a modified "
                         "file)")
        concentration = data["concentration"]
        lines.append(f"concentration: {concentration['of']} = {concentration['value']:.2f}")
    if data.get("oversized"):
        lines.append(f"oversized (over W): {_file_text(data['oversized'])}")
    dropped = data.get("dropped") or []
    if dropped:
        lines.append(f"dropped paths: {len(dropped)}")
        lines += [f"  {entry['feature']} {entry['role']} {entry['path']}: {entry['why']}"
                  for entry in dropped]
    lines += [f"note: {note}" for note in data.get("notes") or []]
    totals = data.get("totals") or {}
    if totals:
        lines.append(f"model calls of this gate: {totals['model_calls']}, "
                     f"{totals['input_tokens']:,} in / {totals['output_tokens']:,} out")
    if data.get("decision"):
        lines.append(f"decision: {data['decision']} ({data['zone']}): {data['reason']}")
    else:
        lines.append("decision: none (see the errors)")
    return "\n".join(lines) + "\n"
