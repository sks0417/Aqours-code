"""Planner v3, Step 3: the program decides the execution structure from the task facts.

The model of Step 2 reports *facts* (:mod:`facts`): units of work, where they
edit the code, what they provide and use, how large they are. This module
builds the graph from them by the rules of the four properties of the
project proposal. The same rules give a single node, plain parallel nodes,
interfaces first and then parallel nodes, or nodes that wait for each other,
depending on the facts. Every structural choice is a :class:`Decision` that
names its property, its rule, the units and nodes involved, the numbers it
used and why. Each property can be turned off (``ablate``); it then falls
back to its simplest behaviour.

Core principle: first satisfy the real dependencies, then use rules to cut
the coordination cost. The thresholds are initial assumptions, not proven
optima. Their basis: a worker's fixed cost (starting, reading the code,
running its checks) is about a minute, as much as writing a few dozen lines
directly.

0. **Contradictions.** ``uses`` form a graph (provider -> user). A cycle of
   ``behavior`` uses only (each needs the other's real result) cannot be
   split: its units are merged. A cycle with an ``interface`` use is left to
   step 2, where that interface goes first and opens the cycle.
1. **P1, which nodes.** Units in ``together`` are merged; a unit smaller
   than ``SMALL_UNIT_LINES`` joins the group it is most related to (same
   file, unless both only register names there, or uses between them).
   Units changing the same function are merged too (P3, step 3.1, done here
   because it decides the nodes). One group left: the single-agent graph.
2. **P2, real dependencies first.** A ``behavior`` use waits for the
   provider; an ``interface`` use waits too when either side is smaller
   than ``WAIT_LINES``; otherwise the interface goes into the node ``PRE``
   that runs first. A registered name (an export, a registry entry) goes
   last, into ``REG``, when nobody uses it; stays with its unit when only
   that unit, or users that wait anyway, need it; and goes into ``PRE``,
   together with the interface of what it registers, when another unit
   needs it early. When two or more nodes keep registrations in one file
   (step 2c, P3), those registrations go into ``PRE`` with their interfaces
   too, so that the nodes do not queue as a whole on that file.
3. **P3, shared edits.** On a file several groups edit, a small
   (``SMALL_SHARED_EDIT_LINES``) edit that can be written on its own moves
   into ``PRE``; everything else stays and is ordered (``derive_edges``).
4. **P4, checks.** Each node runs its own tests and the existing tests that
   import, directly or indirectly, a file it changes (:mod:`affected`); more
   than ``AFFECTED_TESTS_MAX`` such files, or Python files whose imports
   cannot be read, run the whole suite. A node that edits a file another
   node edits, ``PRE`` and ``REG`` run the whole suite after merging.
5. **The graph.** Nodes, explicit edges for the decisions, then
   :func:`derive_edges` and :func:`validate`.

Problems are told apart by their source. When a move of the program breaks
one of its own invariants (a file nobody edits any more, a symbol nobody
provides), :class:`SynthesisError` says which and after which decisions; the
model is not asked. When the facts themselves contradict each other (a
dependency cycle no interface can open, an edge ``derive_edges`` had to skip
for a cycle), :class:`FactsConflict` carries issues the model revises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .affected import TestImports
from .derive import derive_edges
from .facts import FactEdit, Facts, Unit
from .planner import PLANNER_VERSION, GraphInfo
from .repo_index import RepoIndex
from .revise import Part, _new_id, goal_with_conventions, merged_goal
from .schema import Graph, RevisionEntry, parse_symbol
from .single import single_node
from .validate import Issue, usable_edges, validate

# Initial assumptions, not proven optima (see the module docstring).
SMALL_UNIT_LINES = 30          # P1: a smaller unit joins a related one
WAIT_LINES = 60                # P2: a smaller provider or user waits instead of PRE
SMALL_SHARED_EDIT_LINES = 20   # P3: a shared edit this small may go first
AFFECTED_TESTS_MAX = 30        # P4: more affected test files run the whole suite
REGISTRATION_MAX_LINES = 5     # a "registration" longer than this is logic
# P1: when the heaviest chain of nodes carries more than this share of all the
# work, the graph becomes the single-agent graph. At 0.7 the ideal speed-up of
# the split graph is at most 1 / 0.7, about 1.43 times, which the fixed cost of
# the extra nodes (starting, reading the code, checking, merging) roughly eats up.
PARALLEL_PATH_MAX = 0.7
INTERFACE_LINES = 5            # P1: the work PRE does for one interface

PROPERTIES = ("p1", "p2", "p3", "p4")
PRE, REG = "PRE", "REG"
FULL_SUITE_FALLBACK = ["git diff --check"]  # only when there are no final checks
_STRENGTH = {"order": 0, "interface": 1, "full": 2}


class SynthesisError(Exception):
    """The program broke one of its own invariants: a bug in the rules, not the model's."""

    def __init__(self, step: str, problems: list[str], decisions: list["Decision"]):
        self.step = step
        self.problems = problems
        self.recent = [decision.label() for decision in decisions[-3:]]
        text = f"after {step}: " + "; ".join(problems)
        if self.recent:
            text += f" (latest decisions: {'; '.join(self.recent)})"
        super().__init__(text)


class FactsConflict(Exception):
    """The facts contradict each other; the model must revise them."""

    def __init__(self, issues: list[Issue]):
        super().__init__("; ".join(issue.format() for issue in issues))
        self.issues = issues


@dataclass
class Decision:
    """One structural choice of the program."""

    property: str  # P1-P4
    rule: str
    units: list[str]
    reason: str
    numbers: dict[str, int] = field(default_factory=dict)
    nodes: list[str] = field(default_factory=list)

    def label(self) -> str:
        """``P2/wait_small (A, B)``."""
        return f"{self.property}/{self.rule} ({', '.join(self.units or self.nodes)})"

    def to_dict(self) -> dict:
        """JSON form, as written to the planner report."""
        return {"property": self.property, "rule": self.rule, "units": self.units,
                "nodes": self.nodes, "numbers": self.numbers, "reason": self.reason}


@dataclass
class Synthesis:
    """The graph and how it was decided."""

    graph: Graph
    decisions: list[Decision]
    node_units: dict[str, list[str]]
    pre: str | None = None
    reg: str | None = None
    ablate: list[str] = field(default_factory=list)
    # node_work, total, critical, ratio, path, threshold, collapsed; None when
    # the units formed one group before any graph was built
    parallelism: dict | None = None

    def counts(self) -> dict[str, int]:
        """Number of decisions per property."""
        counts = {prop: 0 for prop in ("P1", "P2", "P3", "P4")}
        for decision in self.decisions:
            counts[decision.property] += 1
        return counts


@dataclass(frozen=True)
class Placed:
    """One edit of one unit, wherever it currently is."""

    unit: str
    edit: FactEdit


@dataclass
class Bucket:
    """A node under construction: a group of units, or PRE / REG."""

    key: str
    units: list[str]
    kind: str = "implement"
    edits: list[Placed] = field(default_factory=list)
    touch: list[str] = field(default_factory=list)   # PRE: existing files of interfaces
    create: list[str] = field(default_factory=list)
    fill: list[str] = field(default_factory=list)    # new files PRE creates, this fills in
    provides: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # sentences added to the goal
    contributors: list[str] = field(default_factory=list)  # PRE/REG: whose parts they took
    shared: list[str] = field(default_factory=list)  # files edited in parallel with others

    def edit_files(self) -> list[str]:
        return list(dict.fromkeys(placed.edit.file for placed in self.edits))

    def files(self) -> list[str]:
        return list(dict.fromkeys([*self.edit_files(), *self.touch, *self.fill, *self.create]))

    def is_empty(self) -> bool:
        return not self.files()


@dataclass
class _Dep:
    """Group ``user`` uses ``symbol`` from group ``provider``."""

    provider: str
    user: str
    symbol: str
    need: str
    provider_units: list[str]
    user_units: list[str]


@dataclass
class _Interface:
    symbol: str
    path: str
    new_file: bool
    providers: list[str]  # bucket keys implementing it
    users: list[str]      # bucket keys using it from PRE
    registered_as: list[str] = field(default_factory=list)  # names PRE registers it as


@dataclass
class _Registration:
    unit: str
    edit: FactEdit
    targets: dict[str, str | None]  # name -> registered symbol
    users: list[str]


@dataclass
class _SharedEdit:
    unit: str
    edit: FactEdit
    others: list[str]  # other buckets editing the file


def _move_edit(source: Bucket, target: Bucket, placed: Placed) -> None:
    """Move one edit to another bucket."""
    source.edits.remove(placed)
    target.edits.append(placed)


def _move_create(source: Bucket, target: Bucket, path: str) -> None:
    """``target`` creates ``path``; ``source`` fills it in afterwards."""
    source.create.remove(path)
    target.create.append(path)
    source.fill.append(path)


def _unique(items) -> list:
    return list(dict.fromkeys(items))


def _components(keys: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one member, in ``keys`` order."""
    reach: dict[str, set[str]] = {}
    for key in keys:
        seen, stack = set(), list(edges.get(key, ()))
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(edges.get(current, ()))
        reach[key] = seen
    found, assigned = [], set()
    for key in keys:
        if key in assigned or key not in reach[key]:
            continue
        component = [other for other in keys if other == key
                     or (other in reach[key] and key in reach[other])]
        assigned.update(component)
        found.append(component)
    return found


def _module_name(path: str) -> str:
    """``src/click/types.py`` -> ``click.types``."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or path


def _quote(path: str) -> str:
    return f'"{path}"' if any(char.isspace() for char in path) else path


class _Synthesizer:
    def __init__(self, facts: Facts, index: RepoIndex, final_checks: list[str],
                 tests: TestImports, ablate: frozenset[str]):
        self.facts = facts
        self.index = index
        self.final_checks = [command for command in final_checks if command.strip()]
        self.full = self.final_checks or list(FULL_SUITE_FALLBACK)
        self.tests = tests
        self.ablate = ablate
        self.decisions: list[Decision] = []
        self.order = {unit.id: position for position, unit in enumerate(facts.units)}
        self.units: dict[str, Unit] = {unit.id: self._normalized(unit) for unit in facts.units}
        if not self.off("p2"):
            self._class_uses_as_interface()
        self.providers: dict[str, list[str]] = {}
        for unit in self.units.values():
            for symbol in unit.provides:
                self.providers.setdefault(symbol, []).append(unit.id)
        self.registrar: dict[str, str] = {}
        for unit in self.units.values():
            for name in unit.registration_names():
                self.registrar.setdefault(name, unit.id)
        # (provider unit, user unit, symbol, need), the provider never the user
        self.unit_deps = [(provider, unit.id, use.symbol, use.need)
                          for unit in self.units.values() for use in unit.uses
                          for provider in self.providers.get(use.symbol, [])
                          if provider != unit.id]
        self.groups: list[list[str]] = [[unit.id] for unit in facts.units]
        self.buckets: list[Bucket] = []
        self.pre = Bucket(PRE, [], kind="contract")
        self.reg = Bucket(REG, [])
        self.waits: list[tuple[str, str, str, str]] = []  # provider, user, symbol, need
        self.firsts: list[tuple[str, str]] = []           # user bucket, symbol from PRE
        self.after_pre: list[str] = []
        self.before_reg: list[str] = []
        self.interfaces: dict[str, _Interface] = {}
        self.registrations: dict[str, list[_Registration]] = {PRE: [], REG: []}
        self.shared_edits: list[_SharedEdit] = []
        self.file_reasons: dict[str, list[str]] = {}
        self.parallel_files: list[str] = []

    # ── records ──

    def decide(self, prop: str, rule: str, units, reason: str, nodes=(), **numbers) -> None:
        self.decisions.append(Decision(prop, rule, _unique(units), reason, dict(numbers),
                                       list(nodes)))

    def off(self, prop: str) -> bool:
        return prop in self.ablate

    def _normalized(self, unit: Unit) -> Unit:
        edits = []
        for edit in unit.edits:
            if edit.kind == "registration" and edit.lines > REGISTRATION_MAX_LINES:
                self.decide("P3", "registration_too_large", [unit.id],
                            f"{unit.id}'s registration in {edit.file} changes {edit.lines} "
                            f"lines, more than {REGISTRATION_MAX_LINES}: it is treated as "
                            "logic that cannot be written on its own",
                            lines=edit.lines, threshold=REGISTRATION_MAX_LINES)
                edit = edit.model_copy(update={"kind": "logic", "standalone": False})
            edits.append(edit)
        return unit.model_copy(update={"edits": edits})

    def _class_uses_as_interface(self) -> None:
        """P2: a ``behavior`` use of a class that only holds data needs its interface.

        A class here is a symbol whose qualified name has no dot, starts with
        an upper-case letter and has a lower-case one (``RateLimit``, not
        ``SCHEMA``). It only holds data when no unit lists one of its methods
        or attributes (``Class.name``) in ``provides`` or in an edit's
        ``symbols``: its behaviour, if any, would be in those. Using such a
        class means its constructor and fields, which exist as soon as its
        interface does.
        """
        listed = {symbol for unit in self.units.values()
                  for symbol in [*unit.provides,
                                 *(name for edit in unit.edits for name in edit.symbols)]}

        def data_class(symbol: str) -> bool:
            name = parse_symbol(symbol).qualname
            if "." in name or not name[:1].isupper() or not any(c.islower() for c in name):
                return False
            return not any(other.startswith(f"{symbol}.") for other in listed)

        for unit_id, unit in list(self.units.items()):
            uses = []
            for use in unit.uses:
                if use.need == "behavior" and data_class(use.symbol):
                    name = parse_symbol(use.symbol).qualname
                    why = use.why.strip()
                    said = f" (the facts said behavior: {why})" if why else ""
                    self.decide("P2", "class_use_interface", [unit_id],
                                f"{unit_id} uses {name}, a class whose methods no unit "
                                "changes, so only its constructor and fields are needed: "
                                f"interface{said}")
                    use = use.model_copy(update={"need": "interface"})
                uses.append(use)
            self.units[unit_id] = unit.model_copy(update={"uses": uses})

    # ── groups ──

    def size(self, members: list[str]) -> int:
        return sum(self.units[unit_id].size for unit_id in members)

    def group_of(self) -> dict[str, int]:
        return {unit_id: number for number, group in enumerate(self.groups)
                for unit_id in group}

    def merge(self, numbers: list[int]) -> list[str]:
        members = sorted((unit_id for number in numbers for unit_id in self.groups[number]),
                         key=self.order.__getitem__)
        self.groups = [group for number, group in enumerate(self.groups)
                       if number not in numbers] + [members]
        self.groups.sort(key=lambda group: self.order[group[0]])
        return members

    def _files_of(self, members: list[str]) -> set[str]:
        return {path for unit_id in members
                for path in [*self.units[unit_id].edit_files(), *self.units[unit_id].create]}

    def _kinds_on(self, members: list[str], path: str) -> set[str]:
        return {edit.kind for unit_id in members for edit in self.units[unit_id].edits
                if edit.file == path}

    def relation(self, first: list[str], second: list[str]) -> int:
        """Shared files plus distinct symbols used between two groups.

        A file on which both groups only register names does not count: an
        export next to another export is no reason to do two pieces of work
        together.
        """
        shared = [path for path in self._files_of(first) & self._files_of(second)
                  if not self._kinds_on(first, path) == {"registration"}
                  == self._kinds_on(second, path)]
        symbols = {symbol for provider, user, symbol, _ in self.unit_deps
                   if (provider in first and user in second)
                   or (provider in second and user in first)}
        return len(shared) + len(symbols)

    def merge_mutual(self) -> None:
        """Step 0: merge groups that need each other's real results."""
        while True:
            of = self.group_of()
            keys = [group[0] for group in self.groups]
            edges: dict[str, set[str]] = {}
            uses: list[tuple[str, str, str, str]] = []
            for provider, user, symbol, need in self.unit_deps:
                first, second = of[provider], of[user]
                if first != second and (need == "behavior" or self.off("p2")):
                    edges.setdefault(keys[first], set()).add(keys[second])
                    uses.append((provider, user, symbol, need))
            components = _components(keys, edges)
            if not components:
                return
            numbers = [keys.index(key) for key in components[0]]
            members = self.merge(numbers)
            inside = [f"{user} uses {symbol} from {provider} ({need})"
                      for provider, user, symbol, need in uses
                      if provider in members and user in members]
            why = ("p2 is off, so every use waits for its provider and a cycle of uses "
                   "cannot be opened" if self.off("p2") else
                   "they need each other's real results, so neither can be done on its own")
            self.decide("P1", "mutual_behavior_merged", members,
                        f"{', '.join(members)} form a cycle of uses ({'; '.join(inside)}): "
                        f"{why}")

    def merge_together(self) -> None:
        for together in self.facts.together:
            if self.off("p1"):
                self.decide("P1", "ablated", together.units,
                            f"p1 is off: together {together.units} is ignored")
                continue
            of = self.group_of()
            numbers = _unique(of[unit_id] for unit_id in together.units if unit_id in of)
            if len(numbers) > 1:
                members = self.merge(numbers)
                self.decide("P1", "together", members,
                            f"the facts list {', '.join(together.units)} as one piece of "
                            f"work: {together.reason or '(no reason given)'}")

    def merge_small(self) -> None:
        while True:
            for number, group in enumerate(self.groups):
                size = self.size(group)
                if size >= SMALL_UNIT_LINES:
                    continue
                related = [(self.relation(group, other), position)
                           for position, other in enumerate(self.groups) if position != number]
                related = [(strength, position) for strength, position in related if strength]
                if self.off("p1") or not related:
                    continue
                best = max(strength for strength, _ in related)
                target = next(position for strength, position in related if strength == best)
                partner = list(self.groups[target])
                members = self.merge([number, target])
                how = ("the only group it is related to" if len(related) == 1 else
                       f"the group it is most related to, of {len(related)}")
                self.decide("P1", "small_merged", members,
                            f"{', '.join(group)} changes about {size} lines, less than "
                            f"{SMALL_UNIT_LINES}: a node of its own costs more than it "
                            f"saves, so it joins {', '.join(partner)}, {how} (shared files "
                            "and uses)",
                            size=size, threshold=SMALL_UNIT_LINES, relation=best)
                break
            else:
                break
        for group in self.groups:
            size = self.size(group)
            if size >= SMALL_UNIT_LINES:
                continue
            if self.off("p1"):
                self.decide("P1", "ablated", group,
                            f"p1 is off: {', '.join(group)} ({size} lines) is not merged",
                            size=size, threshold=SMALL_UNIT_LINES)
            else:
                self.decide("P1", "small_kept", group,
                            f"{', '.join(group)} changes about {size} lines, less than "
                            f"{SMALL_UNIT_LINES}, but shares no file and no use with "
                            "another unit, so it stays on its own",
                            size=size, threshold=SMALL_UNIT_LINES)

    def logic_symbols(self, members: list[str]) -> list[str]:
        return _unique(symbol for unit_id in members for edit in self.units[unit_id].edits
                       if edit.kind == "logic" for symbol in edit.symbols)

    def merge_same_symbol(self) -> None:
        """Step 3.1: two groups changing the same function become one."""
        reported: set[tuple[str, ...]] = set()
        while True:
            for first in range(len(self.groups)):
                for second in range(first + 1, len(self.groups)):
                    one, two = self.groups[first], self.groups[second]
                    shared = [symbol for symbol in self.logic_symbols(one)
                              if symbol in self.logic_symbols(two)]
                    if not shared:
                        continue
                    if self.off("p3"):
                        key = (*one, "|", *two)
                        if key not in reported:
                            reported.add(key)
                            self.decide("P3", "ablated", [*one, *two],
                                        f"p3 is off: {', '.join(one)} and {', '.join(two)} "
                                        f"both change {', '.join(shared)} but stay apart "
                                        "(ordered by file)")
                        continue
                    members = self.merge([first, second])
                    self.decide("P3", "same_symbol_merged", members,
                                f"{', '.join(one)} and {', '.join(two)} both change "
                                f"{', '.join(shared)}: two workers cannot change one "
                                "function apart, so they are one node",
                                symbols=len(shared))
                    break
                else:
                    continue
                break
            else:
                return

    # ── buckets ──

    def make_buckets(self) -> None:
        self.buckets = []
        for group in self.groups:
            units = [self.units[unit_id] for unit_id in group]
            self.buckets.append(Bucket(
                key=group[0], units=list(group),
                edits=[Placed(unit.id, edit) for unit in units for edit in unit.edits],
                create=_unique(path for unit in units for path in unit.create),
                provides=_unique(symbol for unit in units for symbol in unit.provides)))
        self.bucket_of = {unit_id: bucket.key for bucket in self.buckets
                          for unit_id in bucket.units}
        self.by_key = {bucket.key: bucket for bucket in self.buckets}
        self.by_key[PRE], self.by_key[REG] = self.pre, self.reg

    def all_buckets(self) -> list[Bucket]:
        return [self.pre, *self.buckets, self.reg]

    def check_invariants(self, step: str) -> None:
        """Raise :class:`SynthesisError` if a move lost or doubled something."""
        problems = []
        everyone = self.all_buckets()
        members = [unit_id for bucket in self.buckets for unit_id in bucket.units]
        for unit_id in self.units:
            if members.count(unit_id) != 1:
                problems.append(f"unit {unit_id} is in {members.count(unit_id)} groups")
        placed = [(item.unit, item.edit.file) for bucket in everyone for item in bucket.edits]
        for unit in self.units.values():
            for edit in unit.edits:
                count = placed.count((unit.id, edit.file))
                if count == 0:
                    problems.append(f"the edit of {unit.id} on {edit.file} is in no node "
                                    "(nobody is responsible for it)")
                elif count > 1:
                    problems.append(f"the edit of {unit.id} on {edit.file} is in {count} "
                                    "nodes")
            for path in unit.create:
                creators = [bucket.key for bucket in everyone if path in bucket.create]
                if len(creators) != 1:
                    problems.append(f"new file {path} is created by {len(creators)} nodes")
        for bucket in everyone:
            for path in bucket.fill:
                if path not in self.pre.create:
                    problems.append(f"{bucket.key} fills in {path}, which PRE does not create")
            files = set(bucket.files())
            for symbol in bucket.provides:
                if parse_symbol(symbol).path not in files:
                    problems.append(f"{bucket.key} provides {symbol} but does not edit "
                                    f"{parse_symbol(symbol).path}")
        provided = {symbol for bucket in everyone for symbol in bucket.provides}
        for unit in self.units.values():
            for symbol in unit.provides:
                if symbol not in provided:
                    problems.append(f"{symbol} of {unit.id} is provided by no node")
        for bucket in self.buckets:
            if bucket.is_empty():
                problems.append(f"group {bucket.key} edits no file")
        if problems:
            raise SynthesisError(step, problems, self.decisions)

    # ── step 2 ──

    def group_deps(self) -> list[_Dep]:
        found: dict[tuple[str, str, str], _Dep] = {}
        for provider, user, symbol, need in self.unit_deps:
            first, second = self.bucket_of[provider], self.bucket_of[user]
            if first == second:
                continue
            dep = found.setdefault((first, second, symbol),
                                   _Dep(first, second, symbol, need, [], []))
            if need == "behavior":
                dep.need = "behavior"
            dep.provider_units = _unique([*dep.provider_units, provider])
            dep.user_units = _unique([*dep.user_units, user])
        keys = [bucket.key for bucket in self.buckets]
        return sorted(found.values(), key=lambda dep: (keys.index(dep.user),
                                                       keys.index(dep.provider)))

    def target(self, unit_id: str, name: str) -> str | None:
        """The symbol a registered ``name`` stands for: same last name, another file."""
        ref = parse_symbol(name)
        unit = self.units[unit_id]
        candidates = [*unit.provides, *self.logic_symbols([unit_id])]
        last = ref.qualname.rsplit(".", 1)[-1]
        for symbol in candidates:
            other = parse_symbol(symbol)
            if (other.path != ref.path and symbol not in unit.registration_names()
                    and other.qualname.rsplit(".", 1)[-1] == last):
                return symbol
        return None

    def add_interface(self, symbol: str, users: list[str],
                      registered_as: str | None = None) -> None:
        if symbol in self.interfaces:
            item = self.interfaces[symbol]
            item.users = _unique([*item.users, *users])
            if registered_as is not None:
                item.registered_as = _unique([*item.registered_as, registered_as])
            self.after_pre.extend(users)
            return
        path = parse_symbol(symbol).path
        providers = _unique(self.bucket_of[unit_id] for unit_id in self.providers.get(symbol, []))
        new_file = not self.index.has_file(path)
        if new_file:
            creator = next((bucket for bucket in self.all_buckets() if path in bucket.create),
                           None)
            if creator is not None and creator is not self.pre:
                _move_create(creator, self.pre, path)
        elif path not in self.pre.touch:
            self.pre.touch.append(path)
        self.pre.provides = _unique([*self.pre.provides, symbol])
        self.pre.contributors = _unique([*self.pre.contributors,
                                         *self.providers.get(symbol, [])])
        self.interfaces[symbol] = _Interface(
            symbol, path, new_file, providers, list(users),
            [registered_as] if registered_as is not None else [])
        self.after_pre.extend([*providers, *users])

    def step2(self) -> None:
        deps = self.group_deps()
        keys = [bucket.key for bucket in self.buckets]
        edges: dict[str, set[str]] = {}
        for dep in deps:
            edges.setdefault(dep.provider, set()).add(dep.user)
        cyclic = {key: number for number, component in enumerate(_components(keys, edges))
                  for key in component}
        ordering: dict[tuple[str, str, str], str] = {}
        for dep in deps:
            provider, user = self.by_key[dep.provider], self.by_key[dep.user]
            sizes = {"provider_size": self.size(provider.units),
                     "user_size": self.size(user.units)}
            units = [*dep.provider_units, *dep.user_units]
            what = (f"{', '.join(dep.user_units)} uses {dep.symbol} from "
                    f"{', '.join(dep.provider_units)}")
            registrar = self.registrar.get(dep.symbol)
            registered = registrar in dep.provider_units
            on_cycle = (dep.provider in cyclic and dep.user in cyclic
                        and cyclic[dep.provider] == cyclic[dep.user])
            small = min(sizes.values()) < WAIT_LINES
            if self.off("p2"):
                rule, reason = "ablated", f"p2 is off: {what} ({dep.need}), so it waits"
            elif dep.need == "behavior":
                rule, reason = "wait_for_behavior", (
                    f"{what} and needs its real behaviour, so it waits until the provider "
                    "is done")
            elif small and not on_cycle:
                rule, reason = "wait_small", (
                    f"{what}; the interface would be enough, but one side changes fewer "
                    f"than {WAIT_LINES} lines, so waiting costs less than a node that "
                    "defines the interface first")
            elif registered and self.target(registrar, dep.symbol) is None:
                rule, reason = "registration_target_unknown", (
                    f"{what}, a name {registrar} registers, but the symbol it stands for "
                    "is not known, so its interface cannot go first: the user waits")
            else:
                ordering[(dep.provider, dep.user, dep.symbol)] = "first"
                if not registered:
                    self.add_interface(dep.symbol, [dep.user])
                    why = (f"one side changes fewer than {WAIT_LINES} lines, but the use is "
                           "on a cycle of uses, so waiting is impossible" if small else
                           f"both sides are large (at least {WAIT_LINES} lines)")
                    self.decide("P2", "interface_first", units,
                                f"{what}; the interface is enough and {why}, so the "
                                "interface goes into PRE and both sides start after it",
                                nodes=[PRE], threshold=WAIT_LINES, **sizes)
                    self.firsts.append((dep.user, dep.symbol))
                continue
            ordering[(dep.provider, dep.user, dep.symbol)] = "wait"
            self.waits.append((dep.provider, dep.user, dep.symbol, dep.need))
            self.decide("P2", rule, units, reason, threshold=WAIT_LINES, **sizes)
        self.place_registrations(deps, ordering)

    def place_registrations(self, deps: list[_Dep],
                            ordering: dict[tuple[str, str, str], str]) -> None:
        for unit in self.units.values():
            for edit in unit.edits:
                if edit.kind != "registration":
                    continue
                self.place_registration(unit, edit, deps, ordering)

    def place_registration(self, unit: Unit, edit: FactEdit, deps: list[_Dep],
                           ordering: dict[tuple[str, str, str], str]) -> None:
        names = list(edit.symbols)
        home = self.by_key[self.bucket_of[unit.id]]
        placed = Placed(unit.id, edit)
        mine = [dep for dep in deps if dep.symbol in names and unit.id in dep.provider_units]
        first = _unique(dep.user for dep in mine
                        if ordering.get((dep.provider, dep.user, dep.symbol)) == "first")
        waiting = _unique(dep.user for dep in mine
                          if ordering.get((dep.provider, dep.user, dep.symbol)) == "wait")
        own = [member for member in home.units
               if any(use.symbol in names for use in self.units[member].uses)]
        label = ", ".join(names) or "(no names)"
        alone = self.is_all_of(home, placed)
        if self.off("p2"):
            self.decide("P2", "ablated", [unit.id],
                        f"p2 is off: {unit.id} registers {label} in {edit.file} itself")
            return
        targets = {name: self.target(unit.id, name) for name in names}
        unknown = [name for name, symbol in targets.items() if symbol is None]
        if first and (alone or unknown):
            for dep in mine:
                key = (dep.provider, dep.user, dep.symbol)
                if ordering.get(key) == "first":
                    ordering[key] = "wait"
                    self.waits.append((dep.provider, dep.user, dep.symbol, dep.need))
            why = (f"that is all of {home.key}'s work" if alone else
                   f"the symbol {', '.join(unknown)} stands for is not known, so its "
                   "interface cannot go first")
            self.decide("P2", "registration_kept", [unit.id, *self.units_of(first)],
                        f"{', '.join(self.units_of(first))} use {label}, which {unit.id} "
                        f"registers in {edit.file}, but {why}: the registration stays with "
                        f"{unit.id} and its users wait for it")
            return
        if first:
            self.register_first(home, placed, targets, first)
            for user in first:
                for name in names:
                    if ordering.get((home.key, user, name)) == "first":
                        self.firsts.append((user, name))
            interfaces = ", ".join(symbol for symbol in targets.values() if symbol) or "-"
            self.decide("P2", "registration_first", [unit.id, *self.units_of(first)],
                        f"{', '.join(self.units_of(first))} use {label}, which {unit.id} "
                        f"registers in {edit.file}, before {unit.id} is done. Registering "
                        "a name that does not exist yet breaks importing the package, so "
                        f"the registration goes into PRE together with the interface of "
                        f"{interfaces}", nodes=[PRE])
            return
        if waiting or own:
            who = (f"only {', '.join(own)} of the same group" if own and not waiting else
                   f"{', '.join(self.units_of(waiting))}, which wait for {unit.id} anyway")
            self.decide("P2", "registration_kept", [unit.id, *self.units_of(waiting)],
                        f"{unit.id} registers {label} in {edit.file}; {who} need it, so "
                        f"{unit.id} registers it itself")
            return
        if alone:
            self.decide("P2", "registration_kept", [unit.id],
                        f"nobody uses {label}, but registering it in {edit.file} is all of "
                        f"{home.key}'s work, so it stays there")
            return
        _move_edit(home, self.reg, placed)
        self.reg.provides = _unique([*self.reg.provides, *names])
        self.reg.contributors = _unique([*self.reg.contributors, unit.id])
        home.provides = [symbol for symbol in home.provides if symbol not in names]
        self.before_reg.append(home.key)
        self.registrations[REG].append(_Registration(unit.id, edit, targets, []))
        modules = (_unique(_module_name(parse_symbol(symbol).path)
                           for symbol in targets.values() if symbol)
                   or ["the module that defines it"])
        bare = ", ".join(parse_symbol(name).qualname for name in names) or label
        if edit.file in home.files():
            home.notes.append(f"Another step registers {bare} in {edit.file}; do not "
                              f"register it yourself, and import from "
                              f"{', '.join(modules)} in your tests.")
        else:
            home.notes.append(f"Another step registers {bare} in {edit.file}; do not edit "
                              f"that file, and import from {', '.join(modules)} in your "
                              "tests.")
        self.decide("P2", "registration_last", [unit.id],
                    f"no unit uses {label}, which {unit.id} registers in {edit.file}, so "
                    "the registration moves into REG, which runs after "
                    f"{unit.id} and keeps {edit.file} out of the parallel work",
                    nodes=[REG])

    def is_all_of(self, bucket: Bucket, placed: Placed) -> bool:
        """Whether moving ``placed`` out would leave ``bucket`` without files."""
        path = placed.edit.file
        return set(bucket.files()) == {path} and len(
            [item for item in bucket.edits if item.edit.file == path]) == 1

    def register_first(self, home: Bucket, placed: Placed, targets: dict[str, str | None],
                       users: list[str]) -> None:
        """Move a registration into PRE with the interfaces of what it registers.

        ``home`` and ``users`` start after PRE; members of ``home`` that use a
        registered name now take it from PRE.
        """
        names = list(placed.edit.symbols)
        _move_edit(home, self.pre, placed)
        self.pre.provides = _unique([*self.pre.provides, *names])
        self.pre.contributors = _unique([*self.pre.contributors, placed.unit])
        home.provides = [symbol for symbol in home.provides if symbol not in names]
        for name, symbol in targets.items():
            if symbol is not None:
                self.add_interface(symbol, [], registered_as=name)
        self.after_pre.extend([home.key, *users])
        for member in home.units:
            self.firsts.extend((home.key, name) for name in names
                               if any(use.symbol == name for use in self.units[member].uses))
        own = [home.key] if any(use.symbol in names for member in home.units
                                for use in self.units[member].uses) else []
        self.registrations[PRE].append(_Registration(placed.unit, placed.edit, targets,
                                                     _unique([*users, *own])))
        home.notes.append(f"An earlier step registers {', '.join(names)} in "
                          f"{placed.edit.file}; do not register it again.")

    def share_kept_registrations(self) -> None:
        """Step 2c: registrations kept by several nodes in one file go into PRE.

        Each node that kept a registration needs its registered name early (its
        own tests, or users that wait for it). When two or more nodes keep
        registrations in the same file, ordering them by that file would queue
        the whole nodes, not just the few lines of the registrations; so the
        registrations go into PRE with the interfaces of what they register.
        A registration whose symbol is unknown (no interface can go first), or
        that is all of its node's work, stays and is ordered.
        """
        if self.off("p2") or self.off("p3"):
            return  # both fall back to registering in place, ordered by file
        kept: dict[str, list[tuple[Bucket, Placed]]] = {}
        for bucket in self.buckets:
            for placed in bucket.edits:
                if placed.edit.kind == "registration":
                    kept.setdefault(placed.edit.file, []).append((bucket, placed))
        for path, entries in kept.items():
            keys = _unique(bucket.key for bucket, _ in entries)
            if len(keys) < 2:
                continue
            stayed: list[tuple[Bucket, Placed, str]] = []
            for bucket, placed in entries:
                names = list(placed.edit.symbols)
                label = ", ".join(names) or "(no names)"
                targets = {name: self.target(placed.unit, name) for name in names}
                unknown = [name for name, symbol in targets.items() if symbol is None]
                if unknown or self.is_all_of(bucket, placed):
                    why = (f"the symbol {', '.join(unknown)} stands for is not known, so "
                           "its interface cannot go first" if unknown else
                           f"it is all of {bucket.key}'s work")
                    stayed.append((bucket, placed, why))
                    continue
                users = _unique(user for provider, user, symbol, _ in self.waits
                                if provider == bucket.key and symbol in names)
                self.register_first(bucket, placed, targets, users)
                interfaces = ", ".join(symbol for symbol in targets.values() if symbol)
                self.decide("P3", "registration_first_shared", [placed.unit],
                            f"{', '.join(keys)} all keep registrations in {path} and need "
                            "their registered names early; registered by each of them, "
                            f"the whole nodes would queue on {path}. So {placed.unit}'s "
                            f"registration of {label} goes into PRE together with the "
                            f"interface of {interfaces}, and {bucket.key} starts after PRE",
                            nodes=[PRE], nodes_sharing=len(keys))
            for bucket, placed, why in stayed:
                others = [other.key for other in self.all_buckets()
                          if other is not bucket and path in other.files()]
                label = ", ".join(placed.edit.symbols) or "(no names)"
                self.decide("P3", "registration_kept_ordered", [placed.unit],
                            f"{placed.unit} keeps its registration of {label} in {path} "
                            f"because {why}; {bucket.key} therefore queues, as a whole "
                            f"node, with the other nodes that edit {path} "
                            f"({', '.join(others) or 'none'})",
                            nodes=[key for key in others if key in (PRE, REG)],
                            nodes_sharing=len(keys))

    def units_of(self, keys: list[str]) -> list[str]:
        return [unit_id for key in keys for unit_id in self.by_key[key].units]

    def check_cycles(self) -> None:
        """After step 2: a dependency cycle left now is a contradiction in the facts."""
        keys = [bucket.key for bucket in self.all_buckets()]
        edges: dict[str, set[str]] = {}
        reasons: dict[tuple[str, str], list[str]] = {}

        def add(first: str, second: str, reason: str) -> None:
            if first != second:
                edges.setdefault(first, set()).add(second)
                reasons.setdefault((first, second), []).append(reason)

        for provider, user, symbol, need in self.waits:
            users = [unit_id for unit_id in self.by_key[user].units
                     if any(use.symbol == symbol for use in self.units[unit_id].uses)]
            add(provider, user, f"{', '.join(users)} uses {symbol} ({need}) from "
                                f"{', '.join(self.by_key[provider].units)}")
        for key in _unique(self.after_pre):
            add(PRE, key, f"{key} starts after PRE")
        for key in _unique(self.before_reg):
            add(key, REG, f"REG runs after {key}")
        creators = {path: bucket.key for bucket in self.all_buckets() for path in bucket.create}
        for bucket in self.buckets:
            for unit_id in bucket.units:
                for ref in self.units[unit_id].context_files:
                    path = ref.split("#", 1)[0]
                    if path in creators and not self.index.has_file(path):
                        add(creators[path], bucket.key,
                            f"{unit_id} reads {path}, which "
                            f"{', '.join(self.by_key[creators[path]].units) or creators[path]} "
                            "creates")
        issues = []
        for component in _components(keys, edges):
            inside = [text for first in component for second in component
                      for text in reasons.get((first, second), [])]
            units = [unit_id for key in component for unit_id in self.by_key[key].units]
            issues.append(Issue(
                "CONFLICT", units,
                "the facts contradict each other: these units depend on each other in a "
                f"cycle that no interface can open ({'; '.join(inside)}). Check each "
                "`need` (\"interface\" when the name and signature are enough), whether a "
                "unit really reads a file another creates, and list units that only make "
                "sense together in `together`"))
        if issues:
            raise FactsConflict(issues)

    # ── step 3 ──

    def step3(self) -> None:
        editors: dict[str, list[str]] = {}
        for bucket in self.buckets:
            for path in bucket.edit_files():
                editors.setdefault(path, []).append(bucket.key)
        for path, keys in editors.items():
            if len(keys) < 2:
                continue
            reasons = self.file_reasons.setdefault(path, [])
            if self.off("p3"):
                self.decide("P3", "ablated", self.units_of(keys),
                            f"p3 is off: {', '.join(keys)} keep their edits of {path} "
                            "and are ordered")
                continue
            for key in keys:
                bucket = self.by_key[key]
                for placed in [item for item in bucket.edits
                               if item.edit.file == path and item.edit.kind == "logic"]:
                    edit = placed.edit
                    if edit.lines > SMALL_SHARED_EDIT_LINES:
                        reasons.append(f"{placed.unit} changes {edit.lines} lines (more "
                                       f"than {SMALL_SHARED_EDIT_LINES})")
                    elif not edit.standalone:
                        reasons.append(f"{placed.unit}'s edit cannot be written on its own")
                    elif set(bucket.files()) == {path}:
                        reasons.append(f"{placed.unit}'s edit is all of {key}'s work")
                    elif not self.index.has_file(path) and path not in self.pre.create:
                        reasons.append(f"{path} is new and {self.creator_of(path)} creates "
                                       "it, so PRE cannot edit it first")
                    else:
                        self.move_shared(bucket, placed, [other for other in keys
                                                          if other != key])

    def move_shared(self, bucket: Bucket, placed: Placed, others: list[str]) -> None:
        edit = placed.edit
        _move_edit(bucket, self.pre, placed)
        unit = self.units[placed.unit]
        moved = [symbol for symbol in unit.provides if parse_symbol(symbol).path == edit.file]
        self.pre.provides = _unique([*self.pre.provides, *moved])
        self.pre.contributors = _unique([*self.pre.contributors, placed.unit])
        if edit.file not in bucket.files():
            bucket.provides = [symbol for symbol in bucket.provides
                               if parse_symbol(symbol).path != edit.file]
        self.after_pre.append(bucket.key)
        self.shared_edits.append(_SharedEdit(placed.unit, edit, others))
        bucket.notes.append(f"An earlier step makes {placed.unit}'s change to {edit.file} "
                            "(see the request and the goal above); do not edit that file "
                            "for it again.")
        self.decide("P3", "small_shared_edit_first", [placed.unit, *self.units_of(others)],
                    f"{', '.join(others)} also edit {edit.file}; {placed.unit}'s edit there "
                    f"is small ({edit.lines} lines, at most {SMALL_SHARED_EDIT_LINES}) and "
                    "can be written on its own, so PRE makes it first and the others do not "
                    "queue behind it", nodes=[PRE], lines=edit.lines,
                    threshold=SMALL_SHARED_EDIT_LINES)

    def creator_of(self, path: str) -> str:
        """The bucket that creates the new file ``path`` (PRE included)."""
        return next((bucket.key for bucket in self.all_buckets() if path in bucket.create),
                    "another node")

    def edits_on(self, bucket: Bucket, path: str) -> list[Placed]:
        """``bucket``'s edits of ``path``.

        Filling in a new file that PRE creates (its creator's part, moved out
        when an interface in it went first) counts as a ``logic`` edit of the
        symbols the creator provides there.
        """
        items = [item for item in bucket.edits if item.edit.file == path]
        if path in bucket.fill:
            for unit_id in bucket.units:
                unit = self.units[unit_id]
                if path in unit.create:
                    symbols = [symbol for symbol in unit.provides
                               if parse_symbol(symbol).path == path]
                    items.append(Placed(unit_id, FactEdit(file=path, kind="logic",
                                                          symbols=symbols, lines=unit.size)))
        return items

    def share_files(self) -> None:
        """Step 3b (P3): nodes changing different symbols of one file run in parallel.

        After the small shared edits went first, a file that two or more nodes
        (not PRE or REG) still edit is edited in parallel when every edit
        there is ``logic``, lists its symbols, and no two nodes' symbols
        overlap (the same symbol, or a class and one of its members). Each
        node then lists the file in ``edit_set.shared`` and is told which
        parts the others change; a conflict is resolved when merging.
        Otherwise the nodes are ordered on the file, as before. A new file
        that PRE creates counts as edited by the node that fills it in (see
        :meth:`edits_on`); a new file that a node creates itself is not
        shared, since it does not exist until that node is done.
        """
        if self.off("p3"):
            return  # step 3 recorded that these files are only ordered
        editors: dict[str, list[Bucket]] = {}
        for bucket in self.buckets:
            for path in _unique([*bucket.edit_files(), *bucket.fill]):
                editors.setdefault(path, []).append(bucket)
        for path, buckets in editors.items():
            if len(buckets) < 2:
                continue
            edits = {bucket.key: self.edits_on(bucket, path) for bucket in buckets}
            why = self._why_not_parallel(edits)
            if why:
                self.file_reasons.setdefault(path, []).append(why)
                continue
            symbols = {key: _unique(symbol for item in items for symbol in item.edit.symbols)
                       for key, items in edits.items()}
            for bucket in buckets:
                bucket.shared.append(path)
                others = [parse_symbol(symbol).qualname for key, own in symbols.items()
                          if key != bucket.key for symbol in own]
                bucket.notes.append(
                    f"Other nodes change {', '.join(others)} in {path} at the same time. "
                    "Change only your own parts of it and leave theirs as they are; if "
                    "your changes conflict with theirs when merging, you will be asked to "
                    "resolve the conflict.")
            self.parallel_files.append(path)
            parts = "; ".join(f"{key}: {', '.join(parse_symbol(s).qualname for s in own)}"
                              for key, own in symbols.items())
            first = " PRE edits it before all of them." if path in self.pre.files() else ""
            self.decide("P3", "parallel_different_symbols",
                        [unit_id for bucket in buckets for unit_id in bucket.units],
                        f"{', '.join(edits)} edit different parts of {path} ({parts}): "
                        "they run in parallel, and a conflict between them is resolved "
                        f"when merging.{first}", files=len(buckets))

    @staticmethod
    def _why_not_parallel(edits: dict[str, list[Placed]]) -> str:
        """Why the nodes in ``edits`` must be ordered on their file ('' if they need not)."""
        registering = [key for key, items in edits.items()
                       if any(item.edit.kind == "registration" for item in items)]
        if registering:
            return f"{', '.join(registering)} register names there, so the nodes queue"
        bare = [item.unit for items in edits.values() for item in items if not item.edit.symbols]
        if bare:
            return (f"{', '.join(bare)} list no symbols for their edit there, so it is not "
                    "known which parts they change")
        keys = list(edits)
        for number, first in enumerate(keys):
            for second in keys[number + 1:]:
                for one in (s for item in edits[first] for s in item.edit.symbols):
                    for two in (s for item in edits[second] for s in item.edit.symbols):
                        if one == two or two.startswith(f"{one}.") or one.startswith(f"{two}."):
                            what = ("both change " + parse_symbol(one).qualname if one == two
                                    else f"change {parse_symbol(one).qualname} and "
                                         f"{parse_symbol(two).qualname}, which overlap")
                            return f"{first} and {second} {what}"
        return ""

    def record_order(self) -> None:
        if self.off("p3"):
            return
        nodes = [bucket for bucket in self.all_buckets() if not bucket.is_empty()]
        editors: dict[str, list[Bucket]] = {}
        for bucket in nodes:
            for path in bucket.files():
                editors.setdefault(path, []).append(bucket)
        for path, buckets in editors.items():
            if len(buckets) < 2 or path in self.parallel_files:
                continue
            keys = [bucket.key for bucket in buckets]
            kept = [bucket.key for bucket in buckets
                    if any(item.edit.file == path and item.edit.kind == "registration"
                           for item in bucket.edits) and bucket.key not in (PRE, REG)]
            parts = list(self.file_reasons.get(path, []))
            others = ", ".join(key for key in keys if key != PRE)
            creator = next((key for key in keys if path in self.by_key[key].create), None)
            if path in self.pre.create:
                parts.insert(0, f"PRE creates it with only the interfaces; {others} "
                                "fill it in")
            elif creator is not None:
                adders = [key for key in keys if key != creator]
                parts.insert(0, f"{creator} creates it, so it goes first; "
                                f"{', '.join(adders)} {'adds' if len(adders) == 1 else 'add'} "
                                "to it")
            elif path in self.pre.touch:
                parts.insert(0, f"PRE adds the interfaces first; {others} build on them")
            if len(kept) > 1:
                parts.insert(0, f"registrations kept in {', '.join(kept)} queue, a few "
                                "lines each")
            detail = "; ".join(parts) or "they change different parts of it"
            self.decide("P3", "ordered", [unit_id for bucket in buckets
                                          for unit_id in bucket.units],
                        f"{', '.join(keys)} edit {path}: {detail}. They run one after the "
                        "other (order edges)", nodes=[key for key in keys if key in (PRE, REG)])

    # ── step 4 ──

    def check_for(self, bucket: Bucket, counts: dict[str, int]) -> dict:
        units = list(bucket.units)  # empty for PRE and REG: the decision is about the node
        marker = [bucket.key] if bucket.key in (PRE, REG) else []
        changed = bucket.files()
        own = _unique(path for unit_id in bucket.units for path in self.units[unit_id].tests)
        if self.off("p4"):
            self.decide("P4", "ablated", units,
                        f"p4 is off: {bucket.key} runs the whole suite in both checks",
                        nodes=marker)
            return {"commands": list(self.full), "post_merge_commands": list(self.full)}
        py_files = [path for path in changed if path.endswith(".py")]
        affected = [path for path in self.tests.affected(changed) if path not in own]
        if self.tests.unresolved:
            commands = list(self.full)
            self.decide("P4", "full_suite_fallback", units,
                        f"the imports of {len(self.tests.unresolved)} Python file(s) cannot be "
                        f"read ({', '.join(self.tests.unresolved[:5])}), so it is unknown "
                        f"which tests {bucket.key} affects: it runs the whole suite",
                        nodes=marker, unresolved=len(self.tests.unresolved))
        elif len(affected) > AFFECTED_TESTS_MAX:
            commands = list(self.full)
            self.decide("P4", "full_suite_fallback", units,
                        f"{len(affected)} existing test files import what {bucket.key} "
                        f"changes, more than {AFFECTED_TESTS_MAX}: it runs the whole suite",
                        nodes=marker, affected=len(affected), threshold=AFFECTED_TESTS_MAX)
        elif own or affected:
            commands = ["python -m pytest -q " + " ".join(_quote(path)
                                                          for path in [*own, *affected])]
            self.decide("P4", "affected_tests", units,
                        f"{bucket.key} runs its own tests ({len(own)}) and the existing test "
                        f"files that import what it changes ({len(affected)})",
                        nodes=marker, own=len(own), affected=len(affected))
        elif py_files:
            commands = ["python -m compileall -q " + " ".join(_quote(path)
                                                              for path in py_files)]
            self.decide("P4", "compile_only", units,
                        f"{bucket.key} has no tests of its own and no existing test imports "
                        "what it changes: its check compiles the Python files it changes",
                        nodes=marker)
        else:
            commands = list(self.full)
            self.decide("P4", "full_suite_fallback", units,
                        f"{bucket.key} changes no Python file and has no tests: it runs the "
                        "whole suite", nodes=marker)
        shared = [path for path in changed if counts.get(path, 0) > 1]
        if bucket.key in (PRE, REG) or shared:
            why = (f"{bucket.key} changes what many nodes build on" if not shared else
                   f"{', '.join(shared)} {'is' if len(shared) == 1 else 'are'} also edited "
                   "by other nodes")
            self.decide("P4", "shared_file_full_check", units,
                        f"{why}, so the effect of merging is hardest to predict: after its "
                        "merge, the whole suite runs", nodes=marker)
            return {"commands": commands, "post_merge_commands": list(self.full)}
        return {"commands": commands}

    # ── step 5 ──

    def goal_of(self, bucket: Bucket, ids: dict[str, str]) -> str:
        if bucket.key == PRE:
            body = self.pre_goal(ids)
        elif bucket.key == REG:
            body = self.reg_goal(ids)
        elif len(bucket.units) == 1:
            body = self.units[bucket.units[0]].goal.strip()
        else:
            parts = [Part(unit_id, self.units[unit_id].title, self.units[unit_id].goal)
                     for unit_id in bucket.units]
            body = merged_goal(parts, [])
        if bucket.notes:
            body = f"{body.rstrip()}\n\n" + " ".join(bucket.notes)
        return goal_with_conventions(body, self.facts.conventions)

    def _names(self, keys: list[str], ids: dict[str, str]) -> str:
        return ", ".join(ids[key] for key in _unique(keys)) or "none"

    def pre_goal(self, ids: dict[str, str]) -> str:
        lines = ["Prepare what other sub-tasks build on, before they start. Do exactly the "
                 "items below and nothing else: no stubs for other symbols, no feature "
                 "logic beyond these items, no tests."]
        number = 0
        for item in self.interfaces.values():
            number += 1
            fillers = [bucket.key for bucket in self.buckets
                       if item.path in bucket.fill or item.path in bucket.edit_files()]
            verb = "fills" if len(_unique(fillers or item.providers)) == 1 else "fill"
            where = (f"create {item.path} with only this interface; "
                     f"{self._names(fillers or item.providers, ids)} {verb} in the module "
                     "later" if item.new_file else f"in {item.path}")
            used = []
            if item.users:
                used.append(f"used by {self._names(item.users, ids)}")
            if item.registered_as:
                used.append(f"registered below as "
                            f"{', '.join(f'`{name}`' for name in item.registered_as)}")
            lines.append(
                f"{number}. The interface of `{item.symbol}` ({where}): its name and "
                "signature (for a field: its type and default) with a docstring, and a body "
                "that runs but does nothing useful (return an empty value or raise "
                f"NotImplementedError). Implemented by {self._names(item.providers, ids)}; "
                f"{'; '.join(used) or 'used by none'}.")
        for registration in self.registrations[PRE]:
            number += 1
            names = ", ".join(f"`{name}`" for name in registration.edit.symbols)
            note = f" ({registration.edit.note})" if registration.edit.note else ""
            interfaces = [symbol for symbol in registration.targets.values() if symbol]
            made = (f" The interface of {', '.join(f'`{s}`' for s in interfaces)} above makes "
                    "the name exist." if interfaces else "")
            lines.append(f"{number}. In {registration.edit.file}, register {names}{note}, "
                         f"as {registration.unit} (\"{self.units[registration.unit].title}\") "
                         f"would. Used by {self._names(registration.users, ids)}.{made}")
        for shared in self.shared_edits:
            number += 1
            unit = self.units[shared.unit]
            symbols = f"; symbols: {', '.join(shared.edit.symbols)}" if shared.edit.symbols else ""
            note = f"; {shared.edit.note}" if shared.edit.note else ""
            lines.append(f"{number}. {shared.unit}'s (\"{unit.title}\") change to "
                         f"{shared.edit.file}, in full (about {shared.edit.lines} lines"
                         f"{symbols}{note}); {self._names(shared.others, ids)} also edit "
                         f"that file. {shared.unit}'s goal: {unit.goal.strip()}")
        return "\n\n".join(lines)

    def reg_goal(self, ids: dict[str, str]) -> str:
        lines = ["Register names that other sub-tasks defined, after they have finished. "
                 "Make only these registrations; change nothing else and write no tests."]
        for registration in self.registrations[REG]:
            names = ", ".join(f"`{name}`" for name in registration.edit.symbols)
            note = f" ({registration.edit.note})" if registration.edit.note else ""
            defined = [symbol for symbol in registration.targets.values() if symbol]
            source = f", defined as {', '.join(f'`{s}`' for s in defined)}" if defined else ""
            lines.append(f"- In {registration.edit.file}: register {names}{note}, from "
                         f"{ids[self.bucket_of[registration.unit]]} "
                         f"(\"{self.units[registration.unit].title}\"){source}.")
        return "\n".join(lines)

    def node_of(self, bucket: Bucket, node_id: str, ids: dict[str, str], check: dict,
                implemented: set[str]) -> dict:
        provides = list(bucket.provides)
        requires, requires_impl = [], []
        for provider, user, symbol, need in self.waits:
            if user == bucket.key:
                if need == "behavior" and symbol in implemented:
                    requires_impl.append(symbol)
                else:
                    requires.append(symbol)
        requires += [symbol for user, symbol in self.firsts if user == bucket.key]
        requires_impl = [symbol for symbol in _unique(requires_impl) if symbol not in provides]
        requires = [symbol for symbol in _unique(requires)
                    if symbol not in provides and symbol not in requires_impl]
        created = set(bucket.create)
        sources = bucket.units or bucket.contributors
        context = _unique(ref for unit_id in sources for ref in self.units[unit_id].context_files
                          if ref.split("#", 1)[0] not in created)
        if bucket.key in (PRE, REG):
            context = [ref for ref in context if self.index.has_file(ref.split("#", 1)[0])]
        files = set(bucket.files())
        symbols = _unique(symbol for item in bucket.edits if item.edit.kind == "logic"
                          for symbol in item.edit.symbols if parse_symbol(symbol).path in files)
        if bucket.key == PRE:
            title = "Shared interfaces and edits, first"
        elif bucket.key == REG:
            title = "Registrations, last"
        else:
            title = " + ".join(self.units[unit_id].title for unit_id in bucket.units)
        return {
            "id": node_id, "title": title, "kind": bucket.kind,
            "goal": self.goal_of(bucket, ids),
            "edit_set": {"modify": _unique([*bucket.edit_files(), *bucket.touch, *bucket.fill]),
                         "create": list(bucket.create), "symbols": symbols,
                         "shared": list(bucket.shared)},
            "requires": requires, "requires_impl": requires_impl, "provides": provides,
            "check": check, "context_files": context,
            "run_after_failed_upstream": bucket.key == REG,
        }

    def edges(self, ids: dict[str, str]) -> list[dict]:
        best: dict[tuple[str, str], dict] = {}

        def add(first: str, second: str, kind: str, reason: str) -> None:
            if first == second:
                return
            pair = (ids[first], ids[second])
            current = best.get(pair)
            if current is None or _STRENGTH[kind] > _STRENGTH[current["type"]]:
                best[pair] = {"from": pair[0], "to": pair[1], "type": kind,
                              "source": "derived", "reason": reason}

        for key in _unique(self.after_pre):
            add(PRE, key, "interface", f"P2/P3: {ids[key]} starts after PRE, which makes "
                                       "what it builds on")
        for provider, user, symbol, need in self.waits:
            add(provider, user, "full", f"P2: {ids[user]} uses {symbol} ({need}) and waits "
                                        f"for {ids[provider]}")
        for key in _unique(self.before_reg):
            add(key, REG, "full", f"P2: REG registers names {ids[key]} defines")
        return list(best.values())

    def build(self, info: GraphInfo, revised: bool) -> Synthesis:
        nodes = [bucket for bucket in self.all_buckets() if not bucket.is_empty()]
        if self.pre.is_empty() and self.after_pre:
            raise SynthesisError("step 5", ["nodes wait for PRE, which has nothing to do"],
                                 self.decisions)
        counts: dict[str, int] = {}
        for bucket in nodes:
            for path in bucket.files():
                counts[path] = counts.get(path, 0) + 1
        checks = {bucket.key: self.check_for(bucket, counts) for bucket in nodes}
        ids: dict[str, str] = {}
        taken = set(self.units)
        for bucket in self.buckets:
            members = bucket.units
            ids[bucket.key] = (members[0] if len(members) == 1 else
                               _new_id(members, (taken - set(members)) | set(ids.values())))
        for key in (PRE, REG):
            ids[key] = _new_id([key], taken | set(ids.values()))
        implemented = {symbol for bucket in nodes if bucket.kind == "implement"
                       for symbol in [*bucket.provides,
                                      *(s for item in bucket.edits if item.edit.kind == "logic"
                                        for s in item.edit.symbols)]}
        data_nodes = [self.node_of(bucket, ids[bucket.key], ids, checks[bucket.key],
                                   implemented) for bucket in nodes]
        log = [RevisionEntry(action="merge", nodes=bucket.units, into=ids[bucket.key],
                             reason=f"units {', '.join(bucket.units)} form one node "
                                    "(planner v3, see the decisions)")
               for bucket in self.buckets if len(bucket.units) > 1]
        roles = {PRE: "shared interfaces and edits first", REG: "registrations last"}
        log += [RevisionEntry(action="add_node", nodes=[ids[bucket.key]],
                              reason=f"{ids[bucket.key]} added by planner v3 "
                                     f"({roles[bucket.key]})")
                for bucket in (self.pre, self.reg) if not bucket.is_empty()]
        graph = Graph.model_validate({
            "request_id": info.request_id, "request": info.request, "repo": info.repo,
            "base_commit": info.base_commit, "final_checks": info.final_checks,
            "generator": generator(info, revised, self.ablate),
            "nodes": data_nodes, "edges": self.edges(ids),
            "revision_log": [entry.model_dump(mode="json") for entry in log],
        })
        derived, entries = derive_edges(graph, self.index)
        report = validate(derived, self.index)
        if report.errors:
            skipped = [entry for entry in entries if "would create a cycle" in entry.reason]
            if skipped:
                units = {node_id: self.units_of([key]) for key, node_id in ids.items()
                         if key in self.by_key}
                raise FactsConflict([Issue(
                    "CONFLICT", [unit_id for node_id in entry.nodes
                                 for unit_id in units.get(node_id, [node_id])],
                    f"the facts contradict each other: {entry.reason} "
                    f"(validation: {'; '.join(issue.format() for issue in report.errors)}). "
                    "Check the `need`, `uses` and `context_files` of these units")
                    for entry in skipped])
            raise SynthesisError("step 5", [f"the graph is invalid: {issue.format()}"
                                            for issue in report.errors], self.decisions)
        node_ids = {bucket.key: ids[bucket.key] for bucket in nodes}
        for decision in self.decisions:
            decision.nodes = _unique([*(node_ids[self.bucket_of[unit_id]]
                                        for unit_id in decision.units
                                        if self.bucket_of.get(unit_id) in node_ids),
                                      *(node_ids[key] for key in decision.nodes
                                        if key in node_ids)])
        node_units = {ids[bucket.key]: list(bucket.units or bucket.contributors)
                      for bucket in nodes}
        work = {node_ids[bucket.key]: self.work_of(bucket) for bucket in nodes}
        parallelism = measure_parallelism(derived, work)
        if self.judge_parallelism(parallelism):
            return self.single(info, revised, parallelism)
        return Synthesis(derived, self.decisions, node_units,
                         pre=node_ids.get(PRE), reg=node_ids.get(REG),
                         ablate=sorted(self.ablate), parallelism=parallelism)

    def work_of(self, bucket: Bucket) -> int:
        """Lines of work in a node: its units' sizes without what moved to PRE or REG.

        PRE does the edits moved into it plus ``INTERFACE_LINES`` per
        interface; REG does the registrations moved into it.
        """
        if bucket is self.pre:
            return (sum(item.edit.lines for item in bucket.edits)
                    + INTERFACE_LINES * len(self.interfaces))
        if bucket is self.reg:
            return sum(item.edit.lines for item in bucket.edits)
        moved = sum(item.edit.lines for other in (self.pre, self.reg) for item in other.edits
                    if item.unit in bucket.units)
        return max(self.size(bucket.units) - moved, 0)

    def judge_parallelism(self, parallelism: dict) -> bool:
        """P1: record whether the graph is parallel enough; True to use one agent."""
        path = " -> ".join(parallelism["path"])
        critical, total = parallelism["critical"], parallelism["total"]
        ratio, limit = parallelism["ratio"], PARALLEL_PATH_MAX
        numbers = {"critical": critical, "total": total, "ratio": ratio, "threshold": limit}
        chain = (f"the longest chain of nodes {path} has {critical} lines, "
                 f"{ratio:.0%} of all {total} lines")
        if self.off("p1"):
            self.decide("P1", "ablated", [], f"p1 is off: the graph stays split; {chain}",
                        nodes=parallelism["path"], **numbers)
            return False
        if ratio > limit:
            parallelism["collapsed"] = True
            self.decide("P1", "low_parallelism_single", [],
                        f"{chain}, more than {limit:.0%}: split, most of the work would "
                        "still queue, and every extra node adds its fixed cost (starting, "
                        "reading the code, checking, merging), so one agent does it all",
                        nodes=["solve"], **numbers)
            return True
        self.decide("P1", "parallel_enough", [],
                    f"{chain}, at most {limit:.0%}: enough of the work runs in parallel to "
                    "pay for the extra nodes, so the graph stays split",
                    nodes=parallelism["path"], **numbers)
        return False

    def single(self, info: GraphInfo, revised: bool,
               parallelism: dict | None = None) -> Synthesis:
        """The single-agent graph: all units are one group, or the split graph is
        not parallel enough (``parallelism`` given; the decisions before it then
        describe that split graph, which is not used)."""
        members = [unit.id for unit in self.facts.units]
        if parallelism is None:
            members = self.groups[0]
            self.decide("P1", "single_unit", members,
                        f"after merging, all units ({', '.join(members)}) are one group: "
                        "the single-agent graph, as on the fast path", nodes=["solve"])
            for decision in self.decisions:
                decision.nodes = ["solve"]
        why = ("all units form one group" if parallelism is None else
               f"the split graph is not parallel enough (longest chain "
               f"{parallelism['critical']} of {parallelism['total']} lines)")
        graph = Graph.model_validate({
            "request_id": info.request_id, "request": info.request, "repo": info.repo,
            "base_commit": info.base_commit, "final_checks": info.final_checks,
            "generator": generator(info, revised, self.ablate),
            "nodes": [single_node(info.request, info.final_checks)],
            "edges": [],
            "revision_log": [{"action": "other", "nodes": ["solve"],
                              "reason": f"planner v3: {why} ({', '.join(members)})"}],
        })
        return Synthesis(graph, self.decisions, {"solve": list(members)},
                         ablate=sorted(self.ablate), parallelism=parallelism)


def measure_parallelism(graph: Graph, work: dict[str, int]) -> dict:
    """The heaviest chain of nodes along every edge (interface, full and order).

    ``work`` maps node ids to lines of work. Returns ``node_work``, ``total``,
    ``critical`` (the work on the heaviest chain), ``ratio`` (``critical /
    total``, 0 without any work), ``path`` (the chain's nodes, first to
    last), ``threshold`` and ``collapsed`` (False; set by the caller).
    """
    order = [node.id for node in graph.nodes]
    preds: dict[str, list[str]] = {node_id: [] for node_id in order}
    for edge in usable_edges(graph):
        if edge.from_ not in preds[edge.to]:
            preds[edge.to].append(edge.from_)
    best: dict[str, int] = {}
    before: dict[str, str | None] = {}

    def heaviest(node_id: str) -> int:  # the graph is acyclic (validated)
        if node_id not in best:
            previous = [(heaviest(pred), -order.index(pred), pred) for pred in preds[node_id]]
            top = max(previous, default=(0, 0, None))
            best[node_id] = work.get(node_id, 0) + top[0]
            before[node_id] = top[2]
        return best[node_id]

    for node_id in order:
        heaviest(node_id)
    end = max(order, key=lambda node_id: (best[node_id], -order.index(node_id)))
    path = [end]
    while before[path[-1]] is not None:
        path.append(before[path[-1]])
    total = sum(work.get(node_id, 0) for node_id in order)
    critical = best[end]
    return {"node_work": {node_id: work.get(node_id, 0) for node_id in order},
            "total": total, "critical": critical,
            "ratio": round(critical / total, 4) if total else 0.0,
            "path": list(reversed(path)), "threshold": PARALLEL_PATH_MAX,
            "collapsed": False}


def generator(info: GraphInfo, revised: bool, ablate) -> dict:
    """The ``generator`` of a v3 graph; ``ablate`` lists the properties turned off."""
    return {"kind": "planner", "planner_version": PLANNER_VERSION, "model": info.model,
            "revision_mode": "llm" if revised else "none",
            "ablate": sorted(ablate) or None}


def parse_ablate(text: str | None) -> frozenset[str]:
    """``"p1,p3"`` -> ``{"p1", "p3"}``; unknown names raise ``ValueError``."""
    names = [name.strip().lower() for name in (text or "").split(",") if name.strip()]
    unknown = [name for name in names if name not in PROPERTIES]
    if unknown:
        raise ValueError(f"unknown properties: {', '.join(unknown)} (use {', '.join(PROPERTIES)})")
    return frozenset(names)


def synthesize(facts: Facts, index: RepoIndex, info: GraphInfo,
               tests: TestImports | None = None, *, ablate=frozenset(),
               revised: bool = False) -> Synthesis:
    """Build the graph of ``facts`` (already checked by :func:`facts.check_facts`).

    ``tests`` are the imports of the existing test files (:mod:`affected`);
    ``ablate`` names the properties (``p1``-``p4``) whose rules are turned
    off. Raises :class:`FactsConflict` when the facts contradict each other and
    :class:`SynthesisError` when a rule broke an invariant of the program.
    """
    unknown = set(ablate) - set(PROPERTIES)
    if unknown:
        raise ValueError(f"unknown properties: {', '.join(sorted(unknown))}")
    synth = _Synthesizer(facts, index, info.final_checks, tests or TestImports(),
                         frozenset(ablate))
    synth.merge_mutual()
    synth.merge_together()
    synth.merge_small()
    synth.merge_same_symbol()
    synth.merge_mutual()
    if len(synth.groups) == 1:
        return synth.single(info, revised)
    synth.make_buckets()
    synth.check_invariants("step 1 (groups)")
    synth.step2()
    synth.share_kept_registrations()
    synth.check_invariants("step 2 (real dependencies)")
    synth.check_cycles()
    synth.step3()
    synth.check_invariants("step 3 (shared edits)")
    synth.share_files()
    synth.record_order()
    return synth.build(info, revised)


__all__ = ["AFFECTED_TESTS_MAX", "Decision", "FactsConflict", "INTERFACE_LINES",
           "PARALLEL_PATH_MAX", "PRE", "PROPERTIES", "REG", "measure_parallelism",
           "SMALL_SHARED_EDIT_LINES", "SMALL_UNIT_LINES", "Synthesis", "SynthesisError",
           "WAIT_LINES", "parse_ablate", "synthesize"]
