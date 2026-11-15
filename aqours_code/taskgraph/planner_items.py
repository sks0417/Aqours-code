"""Planner v3, Step 2 per draft item (``plan --facts per-item``).

In the default mode (``--facts single``) one agent investigates the whole
request. With a vague request or a large repository it cannot hold what it
reads: planning dask 2023.8.0 took 251 model calls and 11M input tokens in
one round without an answer, and read ``dask/dataframe/core.py`` 110 times,
because every compaction drops what it read. Step 1 already splits the
request into draft items, so in per-item mode Step 2 is done per item, by
agents with small contexts, in parallel:

1. **Investigators.** One per draft item, at most ``max_investigators`` at
   once (:class:`PerItemStep2`). Each is a host-side agent like the single
   planner (read_file and glob only, :mod:`repo_guard` on). Its prompt
   (:func:`build_item_prompt`) has the facts instructions adapted to one
   item (:func:`item_instructions`), the whole request, all draft items with
   its own marked, and the two-level outline built for its item
   (:func:`outline_levels.build_item_outline`). The investigators share one
   read-only clone: an agent's trace and runtime state (transcripts, tool
   results) are kept under its own log directory, and read_file and glob
   write nothing into the workspace.
2. **Merge.** The program merges the answers into one :class:`Facts`
   (:func:`merge_answers`) and runs the normalisation, the checks F1-F9 and
   the synthesis on the whole, as in single mode.
3. **Revisions per item.** Every problem goes to the items of the units it
   names, and only their investigators are asked again, each with its own
   previous answer and its own problems (a ``uses`` that no unit provides
   comes with the symbols the other items provide). A problem that names no
   unit stops the plan.
4. **Failures and limits.** The plan's limits cover all investigators
   together, and every investigator has limits of its own
   (:class:`plan_budget.SharedBudget`). An investigator that fails or
   reaches its limits is run again once, alone; a second failure stops the
   plan with an error that names the item. The answers of the others are
   kept (the report and the investigators' logs).
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .affected import TestImports, scan_test_imports
from .facts import (FactEdit, Facts, Normalisation, Unit, check_facts,
                    drop_existing_unprovided_uses, read_facts)
from .outline import _File
from .outline_levels import (ItemOutline, build_item_outline, item_texts,
                             item_two_level_instructions, other_files_list, read_source_files)
from .plan_budget import (INVESTIGATOR_LIMITS, LIMITS, PlanBudget, Reservation,
                          SharedBudget)
from .planner import (DRAFT_ECHO_CHARS, DraftError, DraftItem, GraphInfo, PlanOptions,
                      _issue_dict, extract_json_block)
from .planner_v3 import FACTS_PROMPT_PATH, REVISION_INSTRUCTION, _passed_note
from .repo_index import RepoIndex
from .schema import parse_symbol
from .synthesize import FactsConflict, Synthesis, SynthesisError, synthesize
from .validate import Issue
from .workers import Worker, WorkerRequest, WorkerResult

# ── the instructions: the facts prompt, adapted to one item ──
#
# The prompt file stays the single mode's. Three of its paragraphs are
# replaced and one section is added (an edit of the file must update these).

OPENING = (
    "You are planning, not implementing. You get a request and a draft: the\n"
    "pieces of work the request asks for, listed from the request text alone,\n"
    "before anyone looked at the code. Look at the code and describe, for every\n"
    "draft item, the work it needs: which existing files it changes and how much,\n"
    "which new files it creates, what it provides to other work and what it uses\n"
    "from it. Several coding agents will later do the work, each in its own copy\n"
    "of the repository. Output the description in the JSON format at the end of\n"
    "this prompt.")
ITEM_OPENING = (
    "You are planning, not implementing. You get a request and a draft: the\n"
    "pieces of work the request asks for, listed from the request text alone,\n"
    "before anyone looked at the code. One draft item is yours. Look at the code\n"
    "and describe the work your item needs: which existing files it changes and\n"
    "how much, which new files it creates, what it provides to other work and\n"
    "what it uses from it. Several coding agents will later do the work, each in\n"
    "its own copy of the repository. Output the description in the JSON format\n"
    "at the end of this prompt.")
LOOK = (
    "Look before you describe. Go through the request and the draft items, then\n"
    "the repository: its outline, its README, and any specification the README\n"
    "or the request points to. Find out which files each item must change, which\n"
    "new files are needed, what the items share, and which conventions the code\n"
    "follows.")
ITEM_LOOK = (
    "Look before you describe. Go through the request and your item, then the\n"
    "repository: its outline, its README, and any specification the README or\n"
    "the request points to. Find out which files your item must change, which\n"
    "new files it needs, what it shares with the other items, and which\n"
    "conventions the code follows.")
UNITS = (
    "**Units.** A unit is one piece of work. By default one draft item is one\n"
    "unit. When an item contains several parts that are large, unrelated to each\n"
    "other, and in different files, give it several units with the same `item`.\n"
    "Every draft item has at least one unit. Do not split an item just because it\n"
    "is big, and do not combine different items into one unit.")
ITEM_UNITS = (
    "**Units.** A unit is one piece of work. By default your item is one unit.\n"
    "When it contains several parts that are large, unrelated to each other, and\n"
    "in different files, give it several units, all with your item's id as\n"
    "`item`. Do not split your item just because it is big.")
OUTPUT_HEADING = "## Output format"
YOUR_ITEM_SECTION = """## Your item

You describe only the units of your item, the one marked in the draft items
below. The other items are investigated by others at the same time; do not
describe their work, even where you see it in the code. The program puts the
answers of all items together and checks them as one description.

- `item`: every unit has your item's id.
- `id`: start the ids of your units with your item's id: `3` when item 3 has
  one unit, `3a` and `3b` when it has two.
- `uses` may name a symbol that the work of another item provides: a name
  that the request, or its interface section, says is added or changed, and
  that your item's code or tests need. You cannot see the other items'
  answers, so name such a symbol exactly as the request names it, as
  `path/to/file.py::Name` or `path/to/file.py::Class.method`.
- `create`: list every new file your item needs, also one that another item
  may need too; the program lets one unit create it and the others add code
  to it.
- `together`: only units of your item.
- `conventions`: the rules of the repository you find. The program joins the
  lists of all items, so list them even when other items may list them too.

The example below describes all four items of its request at once, with ids
of its own. You describe only yours: the investigator of item 2 would answer
with unit B and the conventions."""


def item_instructions() -> str:
    """The instructions of an investigator: the facts prompt, adapted to one item.

    Raises ``ValueError`` when the facts prompt no longer has a paragraph
    this replaces (an edit of the prompt must update both).
    """
    prompt = item_two_level_instructions(FACTS_PROMPT_PATH.read_text(encoding="utf-8"))
    for old, new in ((OPENING, ITEM_OPENING), (LOOK, ITEM_LOOK), (UNITS, ITEM_UNITS)):
        if old not in prompt:
            raise ValueError(f"the facts prompt has no paragraph {old[:40]!r}... to replace")
        prompt = prompt.replace(old, new, 1)
    heading = f"\n{OUTPUT_HEADING}\n"
    if heading not in prompt:
        raise ValueError(f"the facts prompt has no {OUTPUT_HEADING!r} section")
    return prompt.replace(heading, f"\n{YOUR_ITEM_SECTION}\n\n{OUTPUT_HEADING}\n", 1)


# ── the prompt of one investigator ──

def _item_line(item: DraftItem) -> str:
    return f"{item.id}: **{item.title}**" + (f". {item.description.strip()}"
                                            if item.description.strip() else "")


def items_section(items: list[DraftItem], own: DraftItem) -> str:
    """All draft items, the investigator's own one marked."""
    lines = [f"- {_item_line(item)}" + ("  **(your item)**" if item.id == own.id else "")
             for item in items]
    return ("# Draft items\n\nThe pieces of work in the request, listed from the request "
            f"text alone. Yours is item {own.id}, marked below; the other items are "
            "investigated by others at the same time. Every unit you describe has "
            f"`item` {own.id}.\n\n" + "\n".join(lines))


@dataclass
class ItemRevision:
    """What an investigator's next round must fix."""

    number: int  # the item's revision number
    max_revisions: int
    facts: str  # its previous answer, as the program read it when the merge changed it
    errors: list[Issue]
    # (old id, new id, the other item that has the old one)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    # for a use that no unit provides: what the units of every other item provide
    provided: dict[str, list[str]] | None = None


def _revision_section(revision: ItemRevision) -> str:
    problems = "\n".join(f"- {issue.format()}" for issue in revision.errors)
    facts = revision.facts[-DRAFT_ECHO_CHARS:].strip() or "(empty answer)"
    renamed = "".join(f" The program renamed your unit {old} to {new}, because item {other} "
                      f"has a unit {old}; it is {new} below and in the problems."
                      for old, new, other in revision.renamed)
    text = (f"# Fix your previous answer (revision {revision.number} of "
            f"{revision.max_revisions})\n\n"
            "You already described your item once; your answer is below, followed by the "
            "problems the program found in it after putting the answers of all items "
            f"together. {_passed_note(revision.errors)}{renamed}\n\n"
            f"{REVISION_INSTRUCTION}\n\n"
            f"## Previous answer\n\n```json\n{facts}\n```\n\n"
            f"## Problems\n\n{problems}")
    if revision.provided is not None:
        lines = [f"- item {item_id}: " + (", ".join(f"`{symbol}`" for symbol in symbols)
                                          or "(nothing)")
                 for item_id, symbols in revision.provided.items()]
        text += ("\n\n## Symbols the other items provide\n\n"
                 "You cannot see the other items' answers; their units provide the symbols "
                 "below. A `uses` entry names one of them, or a symbol one of your own units "
                 "provides; when your unit needs none of them, leave the entry out. You need "
                 "not read code to fix such a problem.\n\n" + "\n".join(lines))
    return text


def build_item_prompt(request: str, items: list[DraftItem], item: DraftItem, outline: str,
                      file_list: str, revision: ItemRevision | None = None) -> str:
    """The full prompt of one investigator round."""
    files = ("# Repository\n\nThe repository is checked out in your working directory. "
             f"Its source files are in the outline below; its other files:\n\n{file_list}")
    parts = [
        item_instructions().rstrip(),
        "# The request\n\n" + request.strip(),
        items_section(items, item),
        files,
        "# Repository outline\n\n" + outline.strip(),
        f"# Your item\n\nDescribe item {item.id} only: {_item_line(item)}",
    ]
    if revision is not None:
        parts.append(_revision_section(revision))
    return "\n\n".join(parts) + "\n"


@dataclass
class ItemPrompt:
    """The first-round prompt of one investigator and the outline in it."""

    item: DraftItem
    prompt: str
    outline: ItemOutline


def item_outlines(request: str, files: list[_File], tests: TestImports,
                  items: list[DraftItem]) -> dict[str, ItemOutline]:
    """The two-level outline of every draft item (no model is called)."""
    texts = item_texts(request, [(item.id, f"{item.title}. {item.description}")
                                 for item in items])
    return {item.id: build_item_outline(files, tests, texts[item.id]) for item in items}


def item_prompts(request: str, root: Path, index: RepoIndex, items: list[DraftItem],
                 tests: TestImports | None = None) -> list[ItemPrompt]:
    """The first-round prompt of every investigator, without calling a model.

    ``root`` is a checkout of ``index``'s commit (``item-prompts``, the dry run).
    """
    tests = tests if tests is not None else scan_test_imports(Path(root), index)
    outlines = item_outlines(request, read_source_files(root, index), tests, items)
    file_list = other_files_list(index)
    return [ItemPrompt(item, build_item_prompt(request, items, item, outlines[item.id].text,
                                               file_list), outlines[item.id])
            for item in items]


# ── reading and merging the answers ──

@dataclass
class ItemAnswer:
    """One investigator's answer, read: its facts, or the format problems."""

    item: DraftItem
    facts_text: str  # the last ```json block, or the whole answer when there is none
    facts: Facts | None = None
    errors: list[Issue] = field(default_factory=list)  # FORMAT, or F8 of its own units
    normalisations: list[Normalisation] = field(default_factory=list)


def read_item_answer(answer: str, item: DraftItem) -> ItemAnswer:
    """Parse one answer; what the program fixes itself is recorded.

    Every unit gets the investigator's item (an investigator describes only
    its own), and ``together`` may name only the answer's own units (F8).
    """
    try:
        facts_text = extract_json_block(answer)
        facts, fixes = read_facts(answer)
    except DraftError as exc:
        return ItemAnswer(item, answer, errors=exc.issues)
    units = []
    for unit in facts.units:
        if unit.item != item.id:
            fixes.append(Normalisation(unit.id, "item", unit.item, "rewritten",
                                       "an investigator describes only its own item", item.id))
            unit = unit.model_copy(update={"item": item.id})
        units.append(unit)
    facts = facts.model_copy(update={"units": units})
    own = {unit.id for unit in units}
    errors = [Issue("F8", [], f"together lists {unit_id}, which is not one of your units; "
                    "`together` names only units of your own item")
              for group in facts.together for unit_id in group.units if unit_id not in own]
    return ItemAnswer(item, facts_text, facts, errors, fixes)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text) or "item"


def _convention_key(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".")


@dataclass
class Merged:
    """The answers of all items as one description, and what the merge changed."""

    facts: Facts
    normalisations: list[tuple[str, Normalisation]]  # (item, fix)
    unit_items: dict[str, str]  # unit id -> item id
    renamed: dict[str, list[tuple[str, str, str]]]  # item -> (old id, new id, other item)
    changed: set[str]  # items whose units the merge changed


def merge_answers(answers: list[ItemAnswer]) -> Merged:
    """Merge the parsed answers (in draft order) into one :class:`Facts`.

    - **Unit ids** stay unique across items: a unit whose id an earlier item
      (in draft order) already uses is renamed ``<item>-<id>`` (``-2``,
      ``-3`` when that is taken too), in its item's ``together`` as well. Ids
      repeated within one answer are left to F2.
    - **A new file in the ``create`` of units of several items** stays in the
      first item's unit; for the others it becomes a logic edit of that new
      file with the symbols they provide in it and their share of the size.
      Within one answer, two creators are left to F4.
    - **Conventions**: the union, in draft order, without repeats (ignoring
      case, spaces and a final full stop).
    - **together**: each answer's own groups; across items there are none.
    """
    owners: dict[str, str] = {}
    fixes: list[tuple[str, Normalisation]] = []
    renamed: dict[str, list[tuple[str, str, str]]] = {}
    changed: set[str] = set()
    renamed_units: list[tuple[str, list[Unit], list]] = []
    for answer in answers:
        item_id = answer.item.id
        mapping: dict[str, str] = {}
        units = []
        for unit in answer.facts.units:
            new_id = mapping.get(unit.id, unit.id)
            if unit.id not in mapping and owners.get(unit.id, item_id) != item_id:
                other = owners[unit.id]
                base = f"{_safe(item_id)}-{unit.id}"
                new_id, number = base, 2
                while new_id in owners:
                    new_id, number = f"{base}-{number}", number + 1
                mapping[unit.id] = new_id
                renamed.setdefault(item_id, []).append((unit.id, new_id, other))
                fixes.append((item_id, Normalisation(
                    new_id, "id", unit.id, "rewritten",
                    f"item {other} has a unit {unit.id}; unit ids are unique across items",
                    new_id)))
                changed.add(item_id)
            if new_id != unit.id:
                unit = unit.model_copy(update={"id": new_id})
            owners.setdefault(new_id, item_id)
            units.append(unit)
        together = [group.model_copy(update={"units": [mapping.get(unit_id, unit_id)
                                                       for unit_id in group.units]})
                    for group in answer.facts.together]
        renamed_units.append((item_id, units, together))

    creators: dict[str, tuple[str, str]] = {}
    merged_units: list[Unit] = []
    together_all = []
    unit_items: dict[str, str] = {}
    for item_id, units, together in renamed_units:
        for unit in units:
            moved = [path for path in unit.create
                     if path in creators and creators[path][0] != item_id]
            for path in unit.create:
                if path not in moved:
                    creators.setdefault(path, (item_id, unit.id))
            if moved:
                share = (max(0, unit.size - sum(edit.lines for edit in unit.edits))
                         // len(unit.create))
                edits = list(unit.edits)
                for path in moved:
                    first_item, first_unit = creators[path]
                    if path not in unit.edit_files():
                        edits.append(FactEdit(
                            file=path, kind="logic", lines=share, standalone=False,
                            symbols=[symbol for symbol in unit.provides
                                     if parse_symbol(symbol).path == path]))
                    fixes.append((item_id, Normalisation(
                        unit.id, "create", path, "rewritten",
                        f"unit {first_unit} of item {first_item} creates it too; one unit "
                        "creates a new file and the others add code to it",
                        f"a logic edit of {path} ({share} lines)")))
                unit = unit.model_copy(update={
                    "create": [path for path in unit.create if path not in moved],
                    "edits": edits})
                changed.add(item_id)
            merged_units.append(unit)
            unit_items[unit.id] = item_id
        together_all.extend(together)

    conventions, seen = [], set()
    for answer in answers:
        for text in answer.facts.conventions:
            key = _convention_key(text)
            if key and key not in seen:
                seen.add(key)
                conventions.append(text.strip())
    facts = Facts(conventions=conventions, units=merged_units, together=together_all)
    return Merged(facts, fixes, unit_items, renamed, changed)


@dataclass
class ItemsEvaluation:
    """The answers of one round, merged, checked and synthesized."""

    answers: dict[str, ItemAnswer]
    merged: Merged | None = None
    facts: Facts | None = None  # the merged facts after the normalisation
    synthesis: Synthesis | None = None
    errors: list[Issue] = field(default_factory=list)
    problems: dict[str, list[Issue]] = field(default_factory=dict)  # per item, draft order
    unassigned: list[Issue] = field(default_factory=list)  # problems that name no unit
    normalisations: list[tuple[str, Normalisation]] = field(default_factory=list)
    program_error: bool = False


def evaluate_items(answers: dict[str, ItemAnswer], items: list[DraftItem], info: GraphInfo,
                   index: RepoIndex, tests: TestImports, ablate: frozenset[str],
                   revised: bool) -> ItemsEvaluation:
    """Merge, check and synthesize the latest answer of every item.

    While an answer has a format problem, only the format problems are
    reported: the content of a description with a missing item cannot be
    checked (other units may use what the missing one provides).
    """
    evaluation = ItemsEvaluation(answers)
    order = [item.id for item in items]
    for item_id in order:
        evaluation.normalisations += [(item_id, fix)
                                      for fix in answers[item_id].normalisations]
    formats = {item_id: answers[item_id].errors for item_id in order
               if answers[item_id].facts is None}
    if formats:
        evaluation.problems = formats
        evaluation.errors = [issue for issues in formats.values() for issue in issues]
        return evaluation
    merged = merge_answers([answers[item_id] for item_id in order])
    evaluation.merged = merged
    evaluation.normalisations += merged.normalisations
    facts, dropped = drop_existing_unprovided_uses(merged.facts, index)
    evaluation.normalisations += [(merged.unit_items[fix.unit], fix) for fix in dropped]
    evaluation.facts = facts
    own = [(item_id, issue) for item_id in order for issue in answers[item_id].errors]
    issues = [issue for issue in check_facts(facts, items, index) if issue.code != "F8"]
    if not issues and not own:
        try:
            evaluation.synthesis = synthesize(facts, index, info, tests, ablate=ablate,
                                              revised=revised)
        except FactsConflict as exc:
            issues = list(exc.issues)
        except SynthesisError as exc:
            evaluation.errors = [Issue("SYNTH", [], f"the program broke an invariant {exc}")]
            evaluation.program_error = True
            return evaluation
    problems: dict[str, list[Issue]] = {}
    for item_id, issue in own:
        problems.setdefault(item_id, []).append(issue)
    for issue in issues:
        owners = list(dict.fromkeys(merged.unit_items[unit_id] for unit_id in issue.nodes
                                    if unit_id in merged.unit_items))
        if not owners:
            evaluation.unassigned.append(issue)
        for item_id in owners:
            problems.setdefault(item_id, []).append(issue)
    evaluation.problems = {item_id: problems[item_id] for item_id in order if item_id in problems}
    evaluation.errors = [issue for _, issue in own] + issues
    return evaluation


def _item_facts_json(facts: Facts, unit_items: dict[str, str], answer: ItemAnswer) -> str:
    """One item's part of the merged facts, written as its answer would be."""
    data = {"conventions": list(answer.facts.conventions),
            "units": [unit.model_dump(mode="json", exclude_defaults=True)
                      for unit in facts.units if unit_items.get(unit.id) == answer.item.id],
            "together": [group.model_dump(mode="json", exclude_defaults=True)
                         for group in facts.together
                         if all(unit_items.get(unit_id) == answer.item.id
                                for unit_id in group.units)]}
    return json.dumps(data, indent=2, ensure_ascii=False)


def _unprovided_uses(facts: Facts, unit_items: dict[str, str], item_id: str) -> bool:
    provided = {symbol for unit in facts.units for symbol in unit.provides}
    return any(use.symbol not in provided for unit in facts.units
               if unit_items.get(unit.id) == item_id for use in unit.uses)


def other_items_provide(facts: Facts, unit_items: dict[str, str], items: list[DraftItem],
                        item_id: str) -> dict[str, list[str]]:
    """What the units of every other item provide, in draft order."""
    return {item.id: list(dict.fromkeys(symbol for unit in facts.units
                                        if unit_items.get(unit.id) == item.id
                                        for symbol in unit.provides))
            for item in items if item.id != item_id}


# ── running the investigators ──

def trace_counts(path: Path) -> tuple[int | None, int | None]:
    """Compactions and read_file calls in an agent's trace (None, None without a trace)."""
    if not path.is_file():
        return None, None
    compactions = reads = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a timeout can leave a partial last record
        if not isinstance(event, dict):
            continue
        if event.get("type") == "compact":
            compactions += 1
        elif event.get("type") == "tool_use" and event.get("tool") == "read_file":
            reads += 1
    return compactions, reads


@dataclass
class _Outcome:
    """One investigator run: its result, or the plan limit that kept it from starting."""

    result: WorkerResult | None = None
    blocked: str = ""
    reservation: Reservation | None = None


class PerItemStep2:
    """Step 2 of planner v3 with one investigator per draft item."""

    def __init__(self, options: PlanOptions, worker: Worker, clone: Path, index: RepoIndex,
                 tests: TestImports, info: GraphInfo, items: list[DraftItem], request: str,
                 log_dir: Path, budget: PlanBudget, ablate: frozenset[str]):
        self.options, self.worker, self.clone, self.index = options, worker, clone, index
        self.tests, self.info, self.items, self.request = tests, info, items, request
        self.budget, self.ablate = budget, ablate
        self.by_id = {item.id: item for item in items}
        self.limits = options.investigator_limits()
        self.shared = SharedBudget(budget, self.limits)
        self.file_list = other_files_list(index)
        self.outlines = item_outlines(request, read_source_files(clone, index), tests, items)
        self.safe: dict[str, str] = {}
        for item in items:
            name, number = _safe(item.id), 2
            while name in self.safe.values():
                name, number = f"{_safe(item.id)}-{number}", number + 1
            self.safe[item.id] = name
        self.log_dirs = {item.id: log_dir / "items" / f"item-{self.safe[item.id]}"
                         for item in items}
        self.runs: Counter = Counter()
        self.records: list[dict] = []
        self.revisions = {item.id: 0 for item in items}
        self._lock = threading.Lock()
        self.wall_s = 0.0

    # ── one round ──

    def _investigate(self, item_id: str, number: int, prompt: str, rerun: bool) -> _Outcome:
        reservation = self.shared.reserve(self.options.timeout_s)
        if isinstance(reservation, str):
            return _Outcome(blocked=reservation)
        with self._lock:
            self.runs[item_id] += 1
            attempt = self.runs[item_id]
        request = WorkerRequest(
            node_id=f"planner-item-{self.safe[item_id]}", attempt=attempt, prompt=prompt,
            workspace=self.clone, log_dir=self.log_dirs[item_id],
            timeout_s=reservation.timeout_s, repo_guard=True,
            model_budget=reservation.model_budget())
        try:
            result = self.worker.run(request)
        except BaseException:
            self.shared.release(reservation, 2, number, None, item_id)
            raise
        self.shared.release(reservation, 2, number, result, item_id)
        compactions, reads = trace_counts(request.log_dir / f"trace_{attempt}.jsonl")
        outline = self.outlines[item_id]
        record = {
            "item": item_id, "title": self.by_id[item_id].title, "round": number,
            "run": attempt, "rerun": rerun,
            "model_calls": result.model_calls, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "duration_s": round(result.duration_s, 3),
            "compactions": compactions, "read_file": reads, "prompt_chars": len(prompt),
            "outline_chars": outline.chars,
            "level2_files": [entry.path for entry in outline.chosen],
            "level2_cut": [entry.path for entry in outline.cut],
            "level1_collapsed": len(outline.collapsed),
            "limits": reservation.to_dict(),
            "ended": "answer" if result.ok else (result.reason or "worker_error"),
            "error": "" if result.ok else result.error[-2000:],
            "answer": result.final_answer,
            "log_dir": str(request.log_dir),
        }
        with self._lock:
            self.records.append(record)
        return _Outcome(result, reservation=reservation)

    def _finished(self) -> tuple[list[str], list[str]]:
        """Items whose last investigator run gave an answer, and the others."""
        last = {}
        for record in sorted(self.records, key=lambda r: (r["round"], r["run"])):
            last[record["item"]] = record["ended"]
        done = [item.id for item in self.items if last.get(item.id) == "answer"]
        return done, [item.id for item in self.items if item.id not in done]

    def _finished_note(self) -> str:
        done, rest = self._finished()
        return (f"Items with an answer: {', '.join(done) or 'none'}; without: "
                f"{', '.join(rest) or 'none'} (their answers are kept in the report and in "
                "the logs).")

    def _own_limits(self) -> str:
        own = self.limits
        parts = [f"{own.model_calls} model calls" if own.model_calls is not None else None,
                 f"{own.input_tokens:,} input tokens" if own.input_tokens is not None else None,
                 f"{own.seconds:.0f} s" if own.seconds is not None else None]
        return ", ".join(part for part in parts if part) or "none"

    def _limit_stop(self, number: int, item_id: str, limit: str,
                    failed: WorkerResult | None) -> Issue:
        item = self.by_id[item_id]
        if failed is None:
            head = (f"planning stopped before the investigator of item {item_id} "
                    f"({item.title}) in step 2 round {number}: {self.budget.limits.text(limit)} "
                    "was reached.")
        else:
            head = (f"planning stopped: the investigator of item {item_id} ({item.title}) in "
                    f"step 2 round {number} failed ({failed.reason or 'worker_error'}), and "
                    f"{self.budget.limits.text(limit)} leaves no room to run it again.")
        message = (f"{head} So far: {self.budget.so_far()}. {self._finished_note()} "
                   f"Raise it with {LIMITS[limit][1]}.")
        return self.budget.stop("LIMIT", 2, number, limit, message, item=item_id)

    def _failed_twice(self, number: int, item_id: str, first: WorkerResult,
                      second: _Outcome) -> Issue:
        item, result = self.by_id[item_id], second.result
        reasons = (first.reason or "worker_error", result.reason or "worker_error")
        cut = second.reservation.cut_by_plan if second.reservation else ()
        limited = result.reason in ("model_budget", "worker_timeout") and bool(cut)
        message = (f"planning stopped: the investigator of item {item_id} ({item.title}) in "
                   f"step 2 round {number} failed twice ({reasons[0]}, then {reasons[1]} when "
                   f"run again alone): {result.error[-1500:]}. So far: "
                   f"{self.budget.so_far()}. {self._finished_note()}")
        if limited:
            message += (f" Its limits were cut to what the plan had left ({', '.join(cut)}); "
                        "raise the plan's limits with --max-calls, --max-input-tokens or "
                        "--max-time.")
        elif result.reason in ("model_budget", "worker_timeout"):
            message += (f" Its own limits: {self._own_limits()} (raise them with "
                        f"{', '.join(INVESTIGATOR_LIMITS.values())}).")
        return self.budget.stop("LIMIT" if limited else "AGENT", 2, number, reasons[1],
                                message, item=item_id)

    def _round(self, number: int, item_ids: list[str], prompts: dict[str, str]
               ) -> tuple[dict[str, WorkerResult], Issue | None]:
        """Run the investigators of ``item_ids`` in parallel; a failed one once more, alone."""
        workers = max(1, min(self.options.max_investigators, len(item_ids)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="investigator") as pool:
            futures = {item_id: pool.submit(self._investigate, item_id, number,
                                            prompts[item_id], False)
                       for item_id in item_ids}
        outcomes = {item_id: future.result() for item_id, future in futures.items()}
        results = {item_id: outcome.result for item_id, outcome in outcomes.items()
                   if outcome.result is not None and outcome.result.ok}
        for item_id in item_ids:
            if item_id in results:
                continue
            outcome = outcomes[item_id]
            if outcome.blocked:
                return results, self._limit_stop(number, item_id, outcome.blocked, None)
            again = self._investigate(item_id, number, prompts[item_id], True)
            if again.blocked:
                return results, self._limit_stop(number, item_id, again.blocked,
                                                 outcome.result)
            if not again.result.ok:
                return results, self._failed_twice(number, item_id, outcome.result, again)
            results[item_id] = again.result
        return results, None

    # ── all rounds ──

    def _revision(self, item_id: str, evaluation: ItemsEvaluation) -> ItemRevision:
        answer = evaluation.answers[item_id]
        merged = evaluation.merged
        facts_text, renamed, provided = answer.facts_text, [], None
        if merged is not None and evaluation.facts is not None:
            if item_id in merged.changed:
                facts_text = _item_facts_json(evaluation.facts, merged.unit_items, answer)
            renamed = merged.renamed.get(item_id, [])
            if _unprovided_uses(evaluation.facts, merged.unit_items, item_id):
                provided = other_items_provide(evaluation.facts, merged.unit_items,
                                               self.items, item_id)
        return ItemRevision(self.revisions[item_id], self.options.max_revisions, facts_text,
                            evaluation.problems[item_id], renamed, provided)

    def _round_record(self, number: int, item_ids: list[str],
                      answers: dict[str, ItemAnswer],
                      evaluation: ItemsEvaluation | None, errors: list[Issue]) -> dict:
        runs = [record for record in self.records if record["round"] == number]
        totals = {key: sum(record[key] for record in runs)
                  for key in ("model_calls", "input_tokens", "output_tokens")}
        totals["duration_s"] = round(sum(record["duration_s"] for record in runs), 3)
        return {
            "round": number,
            "items": list(item_ids),
            "facts_json": {item_id: answers[item_id].facts_text
                           for item_id in item_ids if item_id in answers},
            "normalisations": ([{"item": item_id, **fix.to_dict()}
                                for item_id, fix in evaluation.normalisations]
                               if evaluation is not None else []),
            "errors": [_issue_dict(issue) for issue in errors],
            "problems": ({item_id: [_issue_dict(issue) for issue in issues]
                          for item_id, issues in evaluation.problems.items()}
                         if evaluation is not None else {}),
            "agent": totals,
        }

    def run(self, run) -> None:
        """Run Step 2 per item and fill ``run`` (a ``planner_v3._V3Run``)."""
        started = time.monotonic()
        latest: dict[str, ItemAnswer] = {}
        revision_of: dict[str, ItemRevision] = {}
        to_run = [item.id for item in self.items]
        number = 0
        try:
            while to_run:
                number += 1
                prompts = {item_id: build_item_prompt(
                    self.request, self.items, self.by_id[item_id],
                    self.outlines[item_id].text, self.file_list, revision_of.get(item_id))
                    for item_id in to_run}
                results, stop = self._round(number, to_run, prompts)
                answered = {item_id: read_item_answer(result.final_answer,
                                                      self.by_id[item_id])
                            for item_id, result in results.items()}
                latest.update(answered)
                if stop is not None:
                    run.errors = [stop]
                    run.rounds.append(self._round_record(number, to_run, answered, None,
                                                         run.errors))
                    return
                evaluation = evaluate_items(latest, self.items, self.info, self.index,
                                            self.tests, self.ablate, revised=number > 1)
                run.errors = list(evaluation.errors)
                if evaluation.facts is not None:
                    run.facts = evaluation.facts
                if evaluation.synthesis is not None:
                    run.synthesis, run.graph_round = evaluation.synthesis, number
                    run.graph = evaluation.synthesis.graph
                if evaluation.unassigned:
                    listed = "; ".join(issue.format() for issue in evaluation.unassigned)
                    run.errors = [self.budget.stop(
                        "UNASSIGNED", 2, number, "unassigned",
                        "planning stopped: these problems name no unit, so no investigator "
                        f"can be asked to fix them: {listed}"), *run.errors]
                run.rounds.append(self._round_record(number, to_run, answered, evaluation,
                                                     run.errors))
                if not evaluation.errors or evaluation.program_error or evaluation.unassigned:
                    return
                if any(self.revisions[item_id] >= self.options.max_revisions
                       for item_id in evaluation.problems):
                    return  # an item has no revision left: the plan fails with these errors
                for item_id in evaluation.problems:
                    self.revisions[item_id] += 1
                    revision_of[item_id] = self._revision(item_id, evaluation)
                to_run = list(evaluation.problems)
        finally:
            self.wall_s = time.monotonic() - started
            run.per_item = self.report()

    def report(self) -> dict:
        """The plan report's fields about the investigators."""
        order = {item.id: position for position, item in enumerate(self.items)}
        records = sorted(self.records, key=lambda r: (r["round"], order[r["item"]], r["run"]))
        done, rest = self._finished()
        items = []
        for item in self.items:
            own = [record for record in records if record["item"] == item.id]
            items.append({
                "item": item.id, "title": item.title, "runs": len(own),
                "revisions": self.revisions[item.id],
                "finished": item.id in done,
                "ended": own[-1]["ended"] if own else "not run",
            })
        return {
            "outline_per_item": {item.id: self.outlines[item.id].report()
                                 for item in self.items},
            "max_investigators": self.options.max_investigators,
            "investigator_limits": asdict(self.limits),
            "investigators": records,
            "item_summary": items,
            "items_finished": done,
            "items_unfinished": rest,
            "revisions_per_item": dict(self.revisions),
            "step2_wall_s": round(self.wall_s, 3),
            "step2_investigators_s": round(sum(record["duration_s"] for record in records), 3),
        }
