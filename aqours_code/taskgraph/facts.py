"""Planner v3, Step 2: the task facts the model reports, and the checks on them.

In v3 the model no longer designs the graph. It describes the work as
*units*: where each piece of work edits the code, what it provides and uses
(and whether it needs an interface or the real behaviour), how large it is,
and which units belong together. :mod:`synthesize` builds the graph from
these facts. The facts are local judgements and may be wrong; the checks
here catch the ones that do not fit the repository or each other, and send
them back to the model (``F1``-``F9``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator)

from .planner import DraftError, DraftItem, _as_id, _load_json, _pydantic_issues
from .repo_index import RepoIndex
from .schema import (NODE_ID_PATTERN, RepoPath, Symbol, UniquePaths, UniqueSymbols,
                     _reject_duplicate_paths, parse_symbol, split_context_ref,
                     validate_context_ref)
from .validate import Issue

NEEDS = ("interface", "behavior")

ContextRefs = Annotated[
    list[Annotated[str, AfterValidator(validate_context_ref)]],
    AfterValidator(_reject_duplicate_paths),
]


def _as_ids(value):
    """Let a model write unit ids as numbers."""
    return [_as_id(item) for item in value] if isinstance(value, list) else value


class _Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactEdit(_Facts):
    """A change to one file: an existing one, or a new file another unit creates."""

    file: RepoPath
    kind: Literal["logic", "registration"]
    symbols: UniqueSymbols = Field(default_factory=list)
    lines: StrictInt = Field(ge=0)
    standalone: StrictBool = False
    note: str = ""


class FactUse(_Facts):
    """A symbol another unit (or this unit's registration) provides.

    ``why`` names, for a ``behavior`` need, the result only the provider's
    real code produces. Facts written before it existed have none.
    """

    symbol: Symbol
    need: str
    why: str = ""


class Unit(_Facts):
    """One piece of work."""

    id: str = Field(pattern=NODE_ID_PATTERN)
    item: str
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    size: StrictInt = Field(ge=0)
    edits: list[FactEdit] = Field(default_factory=list)
    create: UniquePaths = Field(default_factory=list)
    provides: UniqueSymbols = Field(default_factory=list)
    uses: list[FactUse] = Field(default_factory=list)
    tests: UniquePaths = Field(default_factory=list)
    context_files: ContextRefs = Field(default_factory=list)

    _item = field_validator("item", mode="before")(_as_id)

    def edit_files(self) -> list[str]:
        """Files of ``edits``, in order."""
        return [edit.file for edit in self.edits]

    def registration_names(self) -> list[str]:
        """Names this unit registers."""
        return [symbol for edit in self.edits if edit.kind == "registration"
                for symbol in edit.symbols]


class Together(_Facts):
    """Units that only make sense as one piece of work."""

    units: list[str] = Field(min_length=1)
    reason: str = ""

    _units = field_validator("units", mode="before")(_as_ids)


class Facts(_Facts):
    """The answer of Step 2 in planner v3."""

    conventions: list[str] = Field(default_factory=list)
    units: list[Unit] = Field(min_length=1)
    together: list[Together] = Field(default_factory=list)


def parse_facts(text: str) -> Facts:
    """Parse the model's final answer; format problems raise :class:`DraftError`."""
    return _validate_facts(_load_json(text))


def _validate_facts(data) -> Facts:
    unit_ids = [str(unit.get("id", "?")) if isinstance(unit, dict) else "?"
                for unit in (data.get("units") if isinstance(data, dict) else None) or []]
    try:
        return Facts.model_validate(data)
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, unit_ids, key="units")) from None


# ── normalisation: what the program fixes itself instead of asking again ──
#
# Fixed here, because the facts mean the same after the fix:
# - a symbol whose file is not a .py file (in edits[].symbols, provides or
#   uses): only Python files define symbols, so it names nothing; dropped, and a
#   uses entry left without its symbol goes with it. A registration edit whose
#   names were all of that kind registers nothing a unit could use, and becomes
#   a logic edit of its unit (a registration of no name would move to REG);
# - a symbol written with "()" after its name ("x.py::run()"): the same symbol;
# - a path written with "\" or starting with "./" (in a file, a symbol, create,
#   tests or context_files): the same path;
# - an entry repeated in a list that must not repeat (edits[].symbols,
#   provides, create, tests, context_files): kept once;
# - a need written "Behaviour", " interface " and the like: the same need;
# - a uses entry of a symbol that exists in the repository and no unit
#   provides (after the parse, with the index): the instructions say such a
#   symbol is not listed, and no dependency can come from it.
# Left as errors, because a fix would have to guess what was meant: a missing
# or wrong file, a symbol in another file than its edit, an unknown draft item,
# repeated unit ids, an edit listed twice for one file, a file both created and
# edited, a creator of an existing file, provides outside the unit's files, a
# use that no unit provides and that does not exist, an unknown need value, a
# test file not created or edited, an unknown unit in `together`, a missing
# context file or heading, and any other malformed symbol, path or field.

SYMBOL_FIELDS = ("provides",)
PATH_LIST_FIELDS = ("create", "tests", "context_files")
_NEEDS_SPELLINGS = {"behaviour": "behavior"}


@dataclass
class Normalisation:
    """One thing the program fixed in the model's facts, and why."""

    unit: str
    field: str
    value: str
    action: str  # "dropped" or "rewritten"
    reason: str
    new_value: str | None = None

    def message(self) -> str:
        what = f"{self.field} {self.value!r}"
        if self.action == "rewritten":
            return f"unit {self.unit}: {what} rewritten as {self.new_value!r} ({self.reason})"
        return f"unit {self.unit}: {what} dropped ({self.reason})"

    def to_dict(self) -> dict:
        return {"unit": self.unit, "field": self.field, "value": self.value,
                "action": self.action, "new_value": self.new_value, "reason": self.reason,
                "message": self.message()}


def _clean_path(value):
    if not isinstance(value, str):
        return value
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _clean_symbol(value):
    """The same symbol, written the way the schema reads it."""
    if not isinstance(value, str) or "::" not in value:
        return value
    path, _, name = value.partition("::")
    name = name.strip()
    if name.endswith("()"):
        name = name[:-2]
    return f"{_clean_path(path.strip())}::{name}"


def _not_python(symbol) -> bool:
    if not isinstance(symbol, str) or symbol.count("::") != 1:
        return False
    path = symbol.split("::")[0]
    return bool(path) and not path.endswith(".py")


def normalize_facts_data(data) -> tuple[object, list[Normalisation]]:
    """A copy of the parsed JSON with what the program can fix fixed, and the list of fixes.

    Shapes it does not expect are left alone, for the schema to report.
    """
    fixes: list[Normalisation] = []
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        return data, fixes
    data = json.loads(json.dumps(data))  # never change the caller's object

    def record(unit, field, value, action, reason, new=None):
        fixes.append(Normalisation(unit, field, str(value), action, reason,
                                   None if new is None else str(new)))

    def symbols(unit_id, field, values):
        if not isinstance(values, list):
            return values
        kept = []
        for value in values:
            clean = _clean_symbol(value)
            if clean != value:
                record(unit_id, field, value, "rewritten", "the same symbol", clean)
            if _not_python(clean):
                record(unit_id, field, clean, "dropped",
                       "only Python files define symbols; the file stays in the unit")
                continue
            if clean in kept:
                record(unit_id, field, clean, "dropped", "listed twice")
                continue
            kept.append(clean)
        return kept

    def paths(unit_id, field, values):
        if not isinstance(values, list):
            return values
        kept = []
        for value in values:
            clean = _clean_path(value)
            if clean != value:
                record(unit_id, field, value, "rewritten", "the same path", clean)
            if clean in kept:
                record(unit_id, field, clean, "dropped", "listed twice")
                continue
            kept.append(clean)
        return kept

    for unit in data["units"]:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id", "?"))
        for number, edit in enumerate(unit.get("edits") or []):
            if not isinstance(edit, dict):
                continue
            if isinstance(edit.get("file"), str):
                clean = _clean_path(edit["file"])
                if clean != edit["file"]:
                    record(unit_id, f"edits[{number}].file", edit["file"], "rewritten",
                           "the same path", clean)
                    edit["file"] = clean
            if "symbols" in edit:
                before = edit["symbols"]
                edit["symbols"] = symbols(unit_id, f"edits[{number}].symbols", before)
                if (edit.get("kind") == "registration" and isinstance(before, list) and before
                        and not edit["symbols"]
                        and all(_not_python(_clean_symbol(value)) for value in before)):
                    edit["kind"] = "logic"
                    record(unit_id, f"edits[{number}].kind", "registration", "rewritten",
                           "it registers no Python name another unit could use; the edit "
                           "stays with its unit", "logic")
        for field_name in SYMBOL_FIELDS:
            if field_name in unit:
                unit[field_name] = symbols(unit_id, field_name, unit[field_name])
        for field_name in PATH_LIST_FIELDS:
            if field_name in unit:
                unit[field_name] = paths(unit_id, field_name, unit[field_name])
        uses = unit.get("uses")
        if isinstance(uses, list):
            kept_uses = []
            for use in uses:
                if not isinstance(use, dict):
                    kept_uses.append(use)
                    continue
                symbol = use.get("symbol")
                clean = _clean_symbol(symbol)
                if clean != symbol:
                    record(unit_id, "uses", symbol, "rewritten", "the same symbol", clean)
                    use["symbol"] = clean
                if _not_python(clean):
                    record(unit_id, "uses", clean, "dropped",
                           "only Python files define symbols; the uses entry goes with it")
                    continue
                need = use.get("need")
                if isinstance(need, str):
                    canonical = need.strip().lower()
                    canonical = _NEEDS_SPELLINGS.get(canonical, canonical)
                    if canonical != need and canonical in NEEDS:
                        record(unit_id, "uses.need", need, "rewritten", "the same need",
                               canonical)
                        use["need"] = canonical
                kept_uses.append(use)
            unit["uses"] = kept_uses
    return data, fixes


def drop_existing_unprovided_uses(facts: Facts, index: RepoIndex
                                  ) -> tuple[Facts, list[Normalisation]]:
    """Drop uses of symbols that exist in the repository and that no unit changes.

    The instructions say such a symbol is not listed; no dependency can come
    from it, so the facts mean the same without it. A use stays (and F6
    reports it) when some unit edits the symbol's file: that unit may change
    the symbol and have forgotten to provide it.
    """
    provided = {symbol for unit in facts.units for symbol in unit.provides}
    edited = {path for unit in facts.units for path in unit.edit_files()}
    edited |= {path for unit in facts.units for path in unit.create}
    fixes: list[Normalisation] = []
    units = []
    for unit in facts.units:
        kept = []
        for use in unit.uses:
            path = use.symbol.split("::")[0]
            if (use.symbol not in provided and index.has_symbol(use.symbol)
                    and path not in edited):
                fixes.append(Normalisation(
                    unit.id, "uses", use.symbol, "dropped",
                    "it exists in the repository and no unit changes its file: it is not "
                    "a dependency between units"))
                continue
            kept.append(use)
        units.append(unit.model_copy(update={"uses": kept}) if len(kept) != len(unit.uses)
                     else unit)
    if not fixes:
        return facts, fixes
    return facts.model_copy(update={"units": units}), fixes


def read_facts(text: str, index: RepoIndex | None = None
               ) -> tuple[Facts, list[Normalisation]]:
    """Parse the model's answer after fixing what the program can fix itself.

    Returns the facts and the fixes; what is left wrong raises
    :class:`DraftError` (format) here or is reported by :func:`check_facts`.
    """
    data, fixes = normalize_facts_data(_load_json(text))
    facts = _validate_facts(data)
    if index is not None:
        facts, more = drop_existing_unprovided_uses(facts, index)
        fixes += more
    return facts, fixes


# ── checks ──

def _check_items(facts: Facts, items: list[DraftItem]) -> list[Issue]:
    known = [item.id for item in items]
    issues = [Issue("F1", [unit.id],
                    f"{unit.id} has item {unit.item!r}, which is not a draft item. Use one "
                    f"of {', '.join(known)}")
              for unit in facts.units if unit.item not in known]
    covered = {unit.item for unit in facts.units}
    issues += [Issue("F1", [], f"draft item {item.id} ({item.title}) has no unit. Every "
                     "draft item needs at least one unit with that `item`")
               for item in items if item.id not in covered]
    return issues


def _check_ids(facts: Facts) -> list[Issue]:
    ids = [unit.id for unit in facts.units]
    repeated = list(dict.fromkeys(unit_id for unit_id in ids if ids.count(unit_id) > 1))
    return [Issue("F2", [unit_id], f"unit id {unit_id} is used by {ids.count(unit_id)} units; "
                  "unit ids must be unique") for unit_id in repeated]


def _check_edits(facts: Facts, index: RepoIndex) -> list[Issue]:
    """F3. An edit of a file that does not exist is allowed when another unit
    creates that file: several units add code to one new file. Several
    creators are F4's, and a unit that also creates the file it edits is
    reported by :func:`_check_create`."""
    issues = []
    for unit in facts.units:
        if not unit.edits and not unit.create:
            issues.append(Issue("F3", [unit.id], "edits no file and creates none: every "
                                "unit changes at least one file"))
        files = unit.edit_files()
        for path in dict.fromkeys(files):
            if files.count(path) > 1:
                issues.append(Issue("F3", [unit.id], f"lists {path} in {files.count(path)} "
                                    "edits; give one edit per file"))
        for edit in unit.edits:
            created_elsewhere = any(edit.file in other.create for other in facts.units
                                    if other is not unit)
            if (not index.has_file(edit.file) and not created_elsewhere
                    and edit.file not in unit.create):
                issues.append(Issue(
                    "F3", [unit.id],
                    f"edits {edit.file}, which does not exist and no other unit creates it. "
                    "A new file is listed in the `create` of exactly one unit; other units "
                    "that add code to it list it in `edits`."))
            for symbol in edit.symbols:
                if parse_symbol(symbol).path != edit.file:
                    issues.append(Issue(
                        "F3", [unit.id],
                        f"the {edit.kind} edit of {edit.file} lists {symbol}, which is in "
                        "another file; an edit's symbols are in its own file"))
    return issues


def _check_create(facts: Facts, index: RepoIndex) -> list[Issue]:
    issues = []
    creators: dict[str, list[str]] = {}
    for unit in facts.units:
        for path in unit.create:
            creators.setdefault(path, []).append(unit.id)
            if index.has_file(path):
                issues.append(Issue("F4", [unit.id],
                                    f"creates {path}, which already exists; put it in "
                                    "`edits` instead"))
            if path in unit.edit_files():
                issues.append(Issue("F4", [unit.id],
                                    f"lists {path} both in `create` and in `edits`; a file "
                                    "the unit creates is listed only in its `create`"))
    for path, unit_ids in creators.items():
        if len(unit_ids) > 1:
            issues.append(Issue("F4", unit_ids,
                                f"{path} is created by {len(unit_ids)} units "
                                f"({', '.join(unit_ids)}). One unit lists it in `create`; the "
                                "other units that add code to it list it in `edits`, with "
                                "the symbols they add."))
    return issues


def _check_provides(facts: Facts) -> list[Issue]:
    issues = []
    for unit in facts.units:
        kinds = {edit.file: edit for edit in unit.edits}
        for symbol in unit.provides:
            path = parse_symbol(symbol).path
            edit = kinds.get(path)
            if edit is None and path not in unit.create:
                issues.append(Issue(
                    "F5", [unit.id],
                    f"provides {symbol}, but {path} is neither in its `edits` nor in its "
                    "`create`"))
            elif edit is not None and edit.kind == "registration" and symbol not in edit.symbols:
                issues.append(Issue(
                    "F5", [unit.id],
                    f"provides {symbol} in {path}, where it only registers names "
                    f"({', '.join(edit.symbols) or 'none'}); a registered name must be in "
                    "the `symbols` of that registration edit"))
    return issues


def _check_uses(facts: Facts) -> list[Issue]:
    provided = {symbol for unit in facts.units for symbol in unit.provides}
    issues = []
    for unit in facts.units:
        for use in unit.uses:
            if use.need not in NEEDS:
                issues.append(Issue(
                    "F6", [unit.id],
                    f"uses {use.symbol} with need {use.need!r}; need is \"interface\" or "
                    "\"behavior\" (\"behavior\" when unsure)"))
            if use.symbol not in provided:
                issues.append(Issue(
                    "F6", [unit.id],
                    f"uses {use.symbol}, which no unit provides. List in `uses` only what "
                    "units provide (also this unit's registered names); an existing symbol "
                    "that no unit changes is not listed"))
    return issues


def _check_tests(facts: Facts) -> list[Issue]:
    return [Issue("F7", [unit.id],
                  f"lists {path} in `tests`, but it is neither in its `create` nor in its "
                  "`edits`")
            for unit in facts.units for path in unit.tests
            if path not in unit.create and path not in unit.edit_files()]


def _check_together(facts: Facts) -> list[Issue]:
    ids = {unit.id for unit in facts.units}
    return [Issue("F8", [], f"together lists {unit_id}, which is not a unit")
            for group in facts.together for unit_id in group.units if unit_id not in ids]


def _check_context(facts: Facts, index: RepoIndex) -> list[Issue]:
    created = {path for unit in facts.units for path in unit.create}
    issues = []
    for unit in facts.units:
        for ref in unit.context_files:
            path, title = split_context_ref(ref)
            if index.has_file(path):
                if title is not None and title not in index.markdown_titles.get(path, []):
                    issues.append(Issue("F9", [unit.id],
                                        f"context heading {ref} does not exist"))
            elif path not in created:
                issues.append(Issue("F9", [unit.id],
                                    f"context file {path} neither exists nor is created by "
                                    "a unit"))
    return issues


def check_facts(facts: Facts, items: list[DraftItem], index: RepoIndex) -> list[Issue]:
    """Errors F1-F9: facts that do not fit the draft, the repository, or each other."""
    return [*_check_items(facts, items), *_check_ids(facts), *_check_edits(facts, index),
            *_check_create(facts, index), *_check_provides(facts), *_check_uses(facts),
            *_check_tests(facts), *_check_together(facts), *_check_context(facts, index)]
