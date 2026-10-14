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
    """A change to one existing file."""

    file: RepoPath
    kind: Literal["logic", "registration"]
    symbols: UniqueSymbols = Field(default_factory=list)
    lines: StrictInt = Field(ge=0)
    standalone: StrictBool = False
    note: str = ""


class FactUse(_Facts):
    """A symbol another unit (or this unit's registration) provides."""

    symbol: Symbol
    need: str


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
    data = _load_json(text)
    unit_ids = [str(unit.get("id", "?")) if isinstance(unit, dict) else "?"
                for unit in (data.get("units") if isinstance(data, dict) else None) or []]
    try:
        return Facts.model_validate(data)
    except ValidationError as exc:
        raise DraftError(_pydantic_issues(exc, unit_ids, key="units")) from None


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
            if not index.has_file(edit.file):
                issues.append(Issue(
                    "F3", [unit.id],
                    f"edits {edit.file}, which does not exist. `edits` lists changes to "
                    "existing files; put a new file in `create`"))
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
                                    f"lists {path} both in `edits` and in `create`"))
    for path, unit_ids in creators.items():
        if len(unit_ids) > 1:
            issues.append(Issue("F4", unit_ids,
                                f"{path} is created by {len(unit_ids)} units; one unit "
                                "creates it, the others use what it provides"))
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
