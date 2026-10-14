"""Plan-then-parallel baseline: a lead plans tasks, teammates do them in parallel.

A control for the task graph method, modelled on the published workflow of
Claude Code's agent teams (a lead builds a task list, teammates take tasks
that are not blocked, work at the same time, and the lead wraps up) and of
Cursor's planner/worker agents. It runs in the same runtime as ``run``: the
same model, worker, Docker sandbox, final checks and hidden tests. Only the
structure differs.

What it deliberately does not have: contracts, interface versus
implementation dependencies, the planner checks and program revision, context
packs and the soft wall, per-task checks, post-merge checks and merge
repairs. Splitting and conflict avoidance are rules of thumb in the lead's
prompt; a conflict is resolved afterwards by the teammate that hit it.
"""
from __future__ import annotations

import json
import platform
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from string import Template
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from . import gitops
from .checks import (
    HIDDEN_TESTS_DIR,
    CheckOutcome,
    Checks,
    checks_for,
    run_checks_with_retry,
    run_hidden_tests,
)
from .cleanup import remove_reserved_files, remove_tree
from .coordinator import (
    AQOURS_SOURCE,
    FINAL_CHECK_TIMEOUT_S,
    EventLog,
    NodeRecord,
    RunResult,
    new_run_id,
    worker_sandbox,
)
from .escapes import scan_run
from .line_endings import LineEndings
from .planner import DraftError, _file_list, extract_json_block
from .prompting import FAILURE_EXCERPT_CHARS, RETRY_TEMPLATE, SANDBOX_NOTE, AttemptFailure
from .repo_index import build_index
from .schema import NODE_ID_PATTERN, validate_repo_path
from .workers import Worker, WorkerRequest, WorkerResult, write_json_atomic

TEAM_VERSION = "team-v0.2"
LEAD_PLAN = "lead_plan"
LEAD_WRAPUP = "lead_wrapup"
DEFAULT_TEAM_WORKERS = 4
LEAD_ATTEMPTS = 2  # the plan is asked for once more after a format error
MAX_TASKS = 20
REPORT_CHARS = 1500
CONFLICT_MARKER = b"<<<<<<<"

LEAD_PROMPT = Template("""\
# Request

$request

# Repository files

$files

# Your role

You are the lead of a team of $n teammates. Each teammate is a coding agent
with its own copy of the repository; they work at the same time, and their
finished work is merged into one branch. You do not write code. Read what
you need of the repository, then break the request into tasks for the team.

Guidelines:
- Make each task a self-contained unit with a clear deliverable that one
  teammate can finish alone.
- Give each teammate a different set of files. Two teammates editing the
  same file overwrite each other's work.
- Work that is sequential, touches the same files, or has many dependencies
  is done better by one teammate: keep it in one task. A single task is a
  valid plan when the request should not be split.
- Too small a task costs more in coordination than it saves; too large a
  task delays everything that waits for it.
- A task that needs another task's finished work lists it in `blocked_by`;
  it starts only after that task is completed. Use `blocked_by` only when
  it is really needed, so that teammates can work in parallel.
- Teammates do not see this conversation. Each task description must say
  everything its teammate needs: what to build, where, which existing code
  to use, and which tests to add.

Answer with one JSON block and nothing after it:

```json
{"tasks": [
  {"id": "T1", "title": "...", "description": "...",
   "files": ["path/one.py", "tests/test_one.py"], "blocked_by": []}
]}
```
""")

LEAD_FIX = Template("""
# Fix your previous answer

Your previous answer could not be used as a task list. Answer again with the
complete task list as the last ```json block.

## Previous answer

$answer

## Problems

$problems
""")

# The rules a teammate and the lead's wrap-up share.
WORKSPACE_RULES = """\
Do not run git commands that change repository state (commit, checkout,
branch, reset, stash, merge, rebase, and so on). Your work is committed and
merged after you finish.

Change existing files with edit_file, only where your task needs it; never
rewrite a whole existing file with write_file. Use write_file only for
files you create.

Work only inside your workspace. Files outside it are not available to you.
"""

TEAMMATE_PROMPT = Template("""\
# Overall request

$request

# Team task list

$task_list

# Reports from teammates who finished

$reports

# Your task: $id $title

$description

Files assigned to you: $files

# Rules

Other teammates are changing other files at the same time. Change the files
assigned to you; change another file only if your task cannot be done
without it, and say so in your final answer.

Before you finish, run the tests that cover your work and make sure they
pass.

Your final answer is shared with the team: say briefly what you built, which
files you changed, and anything other teammates must know.

$rules$sandbox$extra""")

CONFLICT_SECTION = Template("""
# Your work conflicts with teammates' work

While you worked, teammates merged changes to some of the same files. Your
workspace now holds your work merged with theirs; these files contain
conflict markers (<<<<<<<, =======, >>>>>>>): $files.

Resolve every conflict so that both your work and theirs are kept, run the
tests that cover your work, and finish.
""")

WRAPUP_PROMPT = Template("""\
# Overall request

$request

# What the team did

$team

# Checks on the merged work

$checks

# Your task

You are the team lead. Your teammates have finished and their work is
merged in your workspace. Check that the request is fully implemented and
that the parts work together. Finish or fix whatever is missing or broken,
including failed tasks, and make the checks pass. If everything is complete,
change nothing.

$rules$sandbox""")


# ── The lead's plan ──

class PlanError(Exception):
    """The lead's answer is not a usable task list."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _as_id(value):
    """Let a model write a task id as a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


TaskId = Annotated[str, BeforeValidator(_as_id)]


class TeamTask(BaseModel):
    """One task of the lead's plan."""

    model_config = ConfigDict(extra="ignore")

    id: TaskId = Field(pattern=NODE_ID_PATTERN)
    title: str = ""
    description: str
    files: list[str]
    blocked_by: list[TaskId] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be empty")
        return value

    @field_validator("files")
    @classmethod
    def _repository_paths(cls, values: list[str]) -> list[str]:
        return [validate_repo_path(value) for value in values]


class TeamPlan(BaseModel):
    """The lead's task list."""

    model_config = ConfigDict(extra="ignore")

    tasks: list[TeamTask] = Field(min_length=1, max_length=MAX_TASKS)


def _cycle(tasks: list[TeamTask]) -> list[str] | None:
    """Ids of one ``blocked_by`` cycle, or None."""
    blockers = {task.id: task.blocked_by for task in tasks}
    state: dict[str, int] = {}  # 1: on the current path, 2: finished

    def visit(task_id: str, path: list[str]) -> list[str] | None:
        if state.get(task_id) == 2:
            return None
        if state.get(task_id) == 1:
            return path[path.index(task_id):]
        state[task_id] = 1
        for blocker in blockers.get(task_id, []):
            found = visit(blocker, [*path, task_id])
            if found:
                return found
        state[task_id] = 2
        return None

    for task in tasks:
        found = visit(task.id, [])
        if found:
            return found
    return None


def parse_plan(text: str) -> TeamPlan:
    """Parse the lead's final answer; every problem found raises :class:`PlanError`."""
    try:
        block = extract_json_block(text)
    except DraftError as exc:
        raise PlanError([issue.message for issue in exc.issues]) from None
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise PlanError([f"the last ```json block is not valid JSON: {exc}"]) from None
    try:
        plan = TeamPlan.model_validate(data)
    except ValidationError as exc:
        raise PlanError([f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                         for error in exc.errors()]) from None
    problems = []
    ids = [task.id for task in plan.tasks]
    for task_id in sorted({task_id for task_id in ids if ids.count(task_id) > 1}):
        problems.append(f"task id {task_id} is used by {ids.count(task_id)} tasks")
    for task_id in ids:
        if task_id in (LEAD_PLAN, LEAD_WRAPUP):
            problems.append(f"task id {task_id} is reserved")
    for task in plan.tasks:
        for blocker in task.blocked_by:
            if blocker == task.id:
                problems.append(f"task {task.id} lists itself in blocked_by")
            elif blocker not in ids:
                problems.append(f"task {task.id} is blocked by {blocker}, which is not a task")
    if not problems:
        cycle = _cycle(plan.tasks)
        if cycle:
            problems.append("blocked_by forms a cycle: " + " -> ".join([*cycle, cycle[0]]))
    if problems:
        raise PlanError(problems)
    return plan


def build_lead_prompt(request: str, files: str, teammates: int,
                      previous: str | None = None,
                      problems: list[str] | None = None) -> str:
    """The lead's planning prompt; with ``problems``, the request to answer again."""
    prompt = LEAD_PROMPT.substitute(request=request.strip(), files=files, n=teammates)
    if problems:
        prompt += LEAD_FIX.substitute(
            answer=(previous or "").strip() or "(empty answer)",
            problems="\n".join(f"- {problem}" for problem in problems))
    return prompt


# ── Teammates ──

@dataclass
class TaskState:
    """A task of the plan and what has happened to it so far."""

    task: TeamTask
    status: str = "pending"  # pending, in_progress, completed, failed
    reason: str = ""
    report: str = ""  # the final answer of its last attempt
    conflict: bool = False
    conflict_resolved: bool = False
    conflict_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The task as recorded in ``summary.json``."""
        return {"id": self.task.id, "title": self.task.title, "files": self.task.files,
                "blocked_by": self.task.blocked_by, "status": self.status,
                "reason": self.reason, "conflict": self.conflict,
                "conflict_resolved": self.conflict_resolved,
                "conflict_files": self.conflict_files}


def task_line(state: TaskState, status: str | None = None) -> str:
    """One line of the team task list."""
    task = state.task
    return (f"- {task.id} [{status or state.status}] {task.title} — files: "
            f"{', '.join(task.files) or '(none)'} — blocked by: "
            f"{', '.join(task.blocked_by) or '(none)'}")


def _report(text: str) -> str:
    text = text.strip() or "(no report)"
    return text if len(text) <= REPORT_CHARS else text[:REPORT_CHARS].rstrip() + " [...]"


def build_teammate_prompt(request: str, state: TaskState, task_list: str, reports: str,
                          *, sandbox: str = "none", extra: str = "") -> str:
    """The prompt of one teammate attempt; ``extra`` is a retry or conflict section."""
    task = state.task
    return TEAMMATE_PROMPT.substitute(
        request=request.strip(), task_list=task_list, reports=reports or "(none yet)",
        id=task.id, title=task.title, description=task.description.strip(),
        files=", ".join(task.files) or "(none)", rules=WORKSPACE_RULES,
        sandbox=SANDBOX_NOTE if sandbox == "docker" else "", extra=extra)


def build_wrapup_prompt(request: str, team: str, checks: str, *,
                        sandbox: str = "none") -> str:
    """The prompt of the lead's wrap-up."""
    return WRAPUP_PROMPT.substitute(
        request=request.strip(), team=team, checks=checks, rules=WORKSPACE_RULES,
        sandbox=SANDBOX_NOTE if sandbox == "docker" else "")


def retry_section(attempt: int, failure: AttemptFailure) -> str:
    """The ``# Previous attempt failed`` section of the graph workers."""
    output = failure.output[-FAILURE_EXCERPT_CHARS:].strip() or "(no output)"
    return RETRY_TEMPLATE.format(attempt=attempt, reason=failure.reason, output=output,
                                 timeout_note="")


# ── The run ──

@dataclass
class TeamOptions:
    """Parameters of one team run."""

    request_path: Path
    repo: Path
    out_dir: Path = Path("runs")
    workers: int = DEFAULT_TEAM_WORKERS
    max_attempts: int = 2
    worker_timeout_s: float = 1800.0
    lead_timeout_s: float = 1800.0
    final_checks: list[str] = field(default_factory=list)
    hidden_tests: Path | None = None
    # Where checks run. None: where the teammates' shell commands run.
    checks: Checks | None = None


class Team:
    """Executes one team run in one run directory."""

    def __init__(self, request: str, worker: Worker, lead_worker: Worker,
                 options: TeamOptions, run_dir: Path, events: EventLog, base_commit: str,
                 checks: Checks):
        self.request = request
        self.worker = worker
        self.lead_worker = lead_worker
        self.options = options
        self.run_dir = run_dir
        self.repo = run_dir / "repo"
        self.events = events
        self.base_commit = base_commit
        self.checks = checks
        self.sandbox = worker_sandbox(worker)
        self.git_lock = threading.Lock()
        self.merge_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.integration_head = base_commit
        self.records: dict[str, NodeRecord] = {}
        self.states: dict[str, TaskState] = {}
        self.finished: list[str] = []  # completed tasks, in the order they finished
        self.max_parallel = 0
        self.plan_problems: list[str] = []
        self.wrapup_changed_files: list[str] = []
        self.line_endings = LineEndings(self.repo)
        self.final_check_timeout_retries = 0

    # ── one agent call ──

    def _run_agent(self, node_id: str, record: NodeRecord, worker: Worker, prompt: str,
                   workspace: Path, timeout_s: float) -> WorkerResult:
        """Run one agent on ``workspace``; no context pack and no soft wall."""
        attempt = record.attempts + 1
        record.attempts = attempt
        log_dir = self.run_dir / "nodes" / node_id
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"prompt_{attempt}.md").write_text(prompt, encoding="utf-8")
        self.events.emit("worker_start", node=node_id, attempt=attempt)
        request = WorkerRequest(node_id=node_id, attempt=attempt, prompt=prompt,
                                workspace=workspace, log_dir=log_dir, timeout_s=timeout_s,
                                soft_wall_enabled=False)
        try:
            result = worker.run(request)
        except Exception as exc:  # noqa: BLE001 - a broken worker fails the attempt
            result = WorkerResult(ok=False, reason="worker_error",
                                  error=f"{type(exc).__name__}: {exc}")
        write_json_atomic(log_dir / f"worker_{attempt}.json", result.to_dict())
        record.worker_time_s += result.duration_s
        record.model_calls += result.model_calls
        record.input_tokens += result.input_tokens
        record.output_tokens += result.output_tokens
        record.worker_reasons.append("" if result.ok else (result.reason or "worker_error"))
        self.events.emit("worker_end", node=node_id, attempt=attempt, ok=result.ok,
                         reason=result.reason, duration_s=round(result.duration_s, 3),
                         model_calls=result.model_calls, input_tokens=result.input_tokens,
                         output_tokens=result.output_tokens)
        return result

    def _clean_worktree(self, node_id: str, record: NodeRecord, worktree: Path,
                        base: str) -> None:
        """What the Coordinator does after every worker attempt, before the commit."""
        # A Windows host cannot commit or delete files named nul, con, ...
        removed = remove_reserved_files(worktree)
        if removed:
            record.removed_reserved_files.extend(
                path for path in removed if path not in record.removed_reserved_files)
            self.events.emit("reserved_files_removed", node=node_id,
                             attempt=record.attempts, files=removed)
        # Files the agent turned into CRLF go back to LF.
        with self.git_lock:
            restored = self.line_endings.restore(worktree, base)
        if restored:
            record.normalized_line_endings.extend(
                path for path in restored if path not in record.normalized_line_endings)
            self.events.emit("line_endings_normalized", node=node_id,
                             attempt=record.attempts, files=restored)

    def _checks(self, kind: str, label: str, record: NodeRecord | None = None
                ) -> CheckOutcome:
        """Run the final checks on the integration branch, once more after a timeout."""
        outcome = run_checks_with_retry(
            self.options.final_checks, self.repo, FINAL_CHECK_TIMEOUT_S,
            stop_on_failure=False, checks=self.checks, label=label)
        if outcome.timeout_retries:
            if record is not None:
                record.check_timeout_retries += outcome.timeout_retries
            else:
                self.final_check_timeout_retries += outcome.timeout_retries
            self.events.emit("check_timeout_retry", node=LEAD_WRAPUP if record else None,
                             kind=kind, ok=outcome.ok)
        return outcome

    # ── 1. the lead plans ──

    def plan(self) -> TeamPlan | None:
        """Ask the lead for the task list, once more after a format error."""
        record = self.records[LEAD_PLAN] = NodeRecord(status="running",
                                                      start_t=self.events.elapsed())
        self.events.emit("node_start", node=LEAD_PLAN)
        workspace = self.run_dir / "wt" / LEAD_PLAN
        with self.git_lock:
            gitops.add_detached_worktree(self.repo, workspace, self.base_commit)
        files = _file_list(build_index(self.repo, self.base_commit))
        plan: TeamPlan | None = None
        problems: list[str] = []
        previous: str | None = None
        rounds = []
        try:
            for _ in range(LEAD_ATTEMPTS):
                prompt = build_lead_prompt(self.request, files, self.options.workers,
                                           previous, problems)
                result = self._run_agent(LEAD_PLAN, record, self.lead_worker, prompt,
                                         workspace, self.options.lead_timeout_s)
                try:
                    plan, problems = parse_plan(result.final_answer), []
                except PlanError as exc:
                    plan, problems = None, exc.problems
                if not result.ok:
                    plan = None
                    problems = [f"the lead agent failed ({result.reason}): "
                                f"{result.error[-2000:]}", *problems]
                rounds.append({"round": record.attempts, "answer": result.final_answer,
                               "problems": problems})
                if plan is not None:
                    break
                previous = result.final_answer
        finally:
            with self.git_lock:
                gitops.remove_worktree(self.repo, workspace)
        write_json_atomic(self.run_dir / "lead_plan.json", {
            "tasks": [task.model_dump() for task in plan.tasks] if plan else [],
            "valid": plan is not None, "problems": problems, "rounds": rounds})
        self.plan_problems = problems
        record.end_t = self.events.elapsed()
        if plan is None:
            record.status, record.reason = "failed", "lead_plan_invalid"
            record.error = "; ".join(problems)[:4000]
            self.events.emit("node_failed", node=LEAD_PLAN, reason=record.reason,
                             attempts=record.attempts, error=record.error[:2000])
        else:
            record.status = "completed"
            self.events.emit("node_completed", node=LEAD_PLAN, attempts=record.attempts,
                             tasks=[task.id for task in plan.tasks])
        return plan

    # ── 2. teammates work in parallel ──

    def _unblocked(self, state: TaskState) -> bool:
        return all(self.states[blocker].status in ("completed", "failed")
                   for blocker in state.task.blocked_by)

    def execute(self, plan: TeamPlan) -> None:
        """Run every task: in list order, as soon as its blockers have finished."""
        for task in plan.tasks:
            self.states[task.id] = TaskState(task)
            self.records[task.id] = NodeRecord()
        running: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
            while True:
                with self.state_lock:
                    ready = [state for state in self.states.values()
                             if state.status == "pending" and self._unblocked(state)]
                    for state in ready:  # no file locking: teammates may overlap
                        if len(running) >= self.options.workers:
                            break
                        state.status = "in_progress"
                        self.records[state.task.id].status = "running"
                        running[pool.submit(self._run_task_safely, state)] = state.task.id
                        self.max_parallel = max(self.max_parallel, len(running))
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    running.pop(future)

    def _finish(self, state: TaskState, status: str, reason: str = "") -> None:
        record = self.records[state.task.id]
        with self.state_lock:
            state.status, state.reason = status, reason
            record.status, record.reason = status, reason
            if status == "completed":
                self.finished.append(state.task.id)
        record.end_t = self.events.elapsed()
        if status == "completed":
            self.events.emit("node_completed", node=state.task.id, attempts=record.attempts,
                             commit=record.merge_commit)
        else:
            self.events.emit("node_failed", node=state.task.id, reason=reason,
                             attempts=record.attempts, error=record.error[:2000])

    def _run_task_safely(self, state: TaskState) -> None:
        record = self.records[state.task.id]
        try:
            self._run_task(state, record)
        except Exception as exc:  # noqa: BLE001 - recorded, the run continues
            record.error = f"{type(exc).__name__}: {exc}"
            self._finish(state, "failed", "team_error")

    def _prompt_parts(self, current: TaskState) -> tuple[str, str]:
        """The task list and the reports as they are right now."""
        with self.state_lock:
            task_list = "\n".join(task_line(state) for state in self.states.values())
            reports = "\n\n".join(
                f"## {task_id} {self.states[task_id].task.title}\n\n"
                f"{_report(self.states[task_id].report)}" for task_id in self.finished)
        return task_list, reports

    def _teammate(self, state: TaskState, record: NodeRecord, worktree: Path,
                  base: str, extra: str = "") -> WorkerResult:
        task_list, reports = self._prompt_parts(state)
        prompt = build_teammate_prompt(self.request, state, task_list, reports,
                                       sandbox=self.sandbox.kind, extra=extra)
        result = self._run_agent(state.task.id, record, self.worker, prompt, worktree,
                                 self.options.worker_timeout_s)
        state.report = result.final_answer or result.error
        self._clean_worktree(state.task.id, record, worktree, base)
        return result

    def _squash(self, node_id: str, title: str) -> str | None:
        """Squash-merge a node's branch into the integration branch; no check follows."""
        with self.merge_lock, self.git_lock:
            self.events.emit("merge_start", node=node_id)
            commit = gitops.squash_merge(self.repo, gitops.NODE_BRANCH_PREFIX + node_id,
                                         f"[team] {node_id}: {title}")
            if commit is not None:
                self.integration_head = commit
            self.events.emit("merge_end", node=node_id, ok=commit is not None, commit=commit)
        return commit

    def _run_task(self, state: TaskState, record: NodeRecord) -> None:
        task = state.task
        log_dir = self.run_dir / "nodes" / task.id
        log_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.run_dir / "wt" / task.id
        record.start_t = self.events.elapsed()
        self.events.emit("node_start", node=task.id)
        with self.git_lock:
            base = self.integration_head
            gitops.add_node_worktree(self.repo, task.id, worktree, base)
        try:
            reason = self._attempts(state, record, worktree, base)
            if reason:
                self._finish(state, "failed", reason)
                return
            commit = self._squash(task.id, task.title)
            if commit is None:
                commit, reason, base = self._resolve_conflict(state, record, worktree, base)
            record.merge_commit = commit
            self._finish(state, "failed" if commit is None else "completed", reason)
        finally:
            with self.git_lock:
                branch = gitops.NODE_BRANCH_PREFIX + task.id
                record.changed_files = gitops.changed_files(self.repo, base, branch)
                (log_dir / "diff.patch").write_text(
                    gitops.diff_patch(self.repo, base, branch), encoding="utf-8")
                gitops.remove_worktree(self.repo, worktree)
            record.out_of_scope_files = [path for path in record.changed_files
                                         if path not in task.files]
            if record.end_t is None:
                record.end_t = self.events.elapsed()

    def _attempts(self, state: TaskState, record: NodeRecord, worktree: Path,
                  base: str) -> str:
        """Run the teammate until it changes something; return "" or the failure reason.

        A task has no check of its own: a teammate that ends normally with
        changes is done.
        """
        reason, extra = "", ""
        for attempt in range(1, self.options.max_attempts + 1):
            result = self._teammate(state, record, worktree, base, extra)
            with self.git_lock:
                gitops.commit_all(worktree, f"attempt {attempt}")
                changed = gitops.changed_files(worktree, base)
            reason = record.worker_reasons[-1]
            if reason:
                failure = AttemptFailure(reason, result.error or result.final_answer)
                record.error = (result.error or result.final_answer)[-2000:]
            elif not changed:
                reason = "no_changes"
                failure = AttemptFailure(reason,
                                         "The attempt finished without changing any file.")
            else:
                return ""
            extra = retry_section(attempt + 1, failure)
        return reason

    def _resolve_conflict(self, state: TaskState, record: NodeRecord, worktree: Path,
                          base: str) -> tuple[str | None, str, str]:
        """One resolution round by the same task; return (commit, reason, diff base)."""
        task = state.task
        state.conflict = True
        with self.git_lock:
            target = self.integration_head
            files = gitops.merge_keep_conflicts(worktree, target)
        state.conflict_files = files
        self.events.emit("merge_conflict", node=task.id, files=files)
        if files:
            result = self._teammate(state, record, worktree, base,
                                    CONFLICT_SECTION.substitute(files=", ".join(files)))
            reason = record.worker_reasons[-1]
            if reason:
                record.error = (result.error or result.final_answer)[-2000:]
                return None, reason, base
            if any((worktree / path).is_file()
                   and CONFLICT_MARKER in (worktree / path).read_bytes() for path in files):
                return None, "merge_conflict", base
        with self.git_lock:
            gitops.commit_merge(worktree, "resolve conflicts with teammates' work")
        commit = self._squash(task.id, task.title)
        if commit is None:
            return None, "merge_conflict", target
        state.conflict_resolved = True
        self.events.emit("merge_conflict_resolved", node=task.id)
        # The branch now contains the integration commit: measure from there.
        return commit, "", target

    # ── 3. the lead wraps up ──

    def team_report(self) -> str:
        """The final task list and every task's report, for the lead."""
        lines = [task_line(state) for state in self.states.values()]
        reports = []
        for state in self.states.values():
            title = f"## {state.task.id} [{state.status}] {state.task.title}"
            if state.status == "failed":
                title += f" (failed: {state.reason})"
            reports.append(f"{title}\n\n{_report(state.report)}")
        return "\n".join(lines) + "\n\n" + "\n\n".join(reports)

    def wrapup(self) -> None:
        """Run the checks on the merged work, then let the lead finish or fix it."""
        record = self.records[LEAD_WRAPUP] = NodeRecord(status="running",
                                                        start_t=self.events.elapsed())
        self.events.emit("node_start", node=LEAD_WRAPUP)
        outcome = self._checks("wrapup", "wrapup", record)
        (self.run_dir / "wrapup_checks.txt").write_text(outcome.text, encoding="utf-8")
        record.check_time_s += outcome.duration_s
        self.events.emit("wrapup_checks_end", ok=outcome.ok, results=outcome.results)
        checks = outcome.text[-FAILURE_EXCERPT_CHARS:].strip() or "(no checks were given)"
        log_dir = self.run_dir / "nodes" / LEAD_WRAPUP
        log_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.run_dir / "wt" / LEAD_WRAPUP
        with self.git_lock:
            base = self.integration_head
            gitops.add_node_worktree(self.repo, LEAD_WRAPUP, worktree, base)
        try:
            prompt = build_wrapup_prompt(self.request, self.team_report(), checks,
                                         sandbox=self.sandbox.kind)
            result = self._run_agent(LEAD_WRAPUP, record, self.worker, prompt, worktree,
                                     self.options.lead_timeout_s)
            self._clean_worktree(LEAD_WRAPUP, record, worktree, base)
            with self.git_lock:
                gitops.commit_all(worktree, "lead wrap-up")
                changed = gitops.changed_files(worktree, base)
            record.status, record.reason = "completed", record.worker_reasons[-1]
            if record.reason:
                record.status = "failed"
                record.error = (result.error or result.final_answer)[-2000:]
            if changed:  # nothing else merges now, so this cannot conflict
                record.merge_commit = self._squash(LEAD_WRAPUP, "lead wrap-up")
                if record.merge_commit is None:
                    record.status, record.reason = "failed", "merge_conflict"
                else:
                    self.wrapup_changed_files = changed
        except Exception as exc:  # noqa: BLE001 - recorded, the run is still scored
            record.status, record.reason = "failed", "team_error"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self.git_lock:
                branch = gitops.NODE_BRANCH_PREFIX + LEAD_WRAPUP
                record.changed_files = gitops.changed_files(self.repo, base, branch)
                (log_dir / "diff.patch").write_text(
                    gitops.diff_patch(self.repo, base, branch), encoding="utf-8")
                gitops.remove_worktree(self.repo, worktree)
            record.end_t = self.events.elapsed()
        event = "node_completed" if record.status == "completed" else "node_failed"
        self.events.emit(event, node=LEAD_WRAPUP, reason=record.reason,
                         changed_files=self.wrapup_changed_files)

    # ── 4. the same final stage as a graph run ──

    def final_checks(self) -> list[dict]:
        """Run the final checks on the integration branch; record each command."""
        outcome = self._checks("final", "final")
        (self.run_dir / "final_checks.txt").write_text(outcome.text, encoding="utf-8")
        self.events.emit("final_checks_end", ok=outcome.ok, results=outcome.results)
        return outcome.results

    def hidden_tests(self, tests_dir: Path) -> dict:
        """Run hidden tests on a clean checkout of the integration branch."""
        final = self.run_dir / "final"
        with self.git_lock:
            gitops.add_detached_worktree(self.repo, final, gitops.INTEGRATION_BRANCH)
        stats = run_hidden_tests(final, tests_dir, self.checks,
                                 xml_path=self.run_dir / "hidden_tests.xml",
                                 text_path=self.run_dir / "hidden_tests.txt")
        self.events.emit("hidden_tests_end", **stats)
        return stats

    def audit_escapes(self) -> int:
        """Record each agent's escape attempts from its traces; return the total."""
        for node_id, escapes in scan_run(self.run_dir).items():
            if node_id in self.records:
                self.records[node_id].escape_attempts = escapes.count
                self.records[node_id].escape_samples = escapes.samples()
        total = sum(record.escape_attempts for record in self.records.values())
        self.events.emit("escape_audit_end", total=total)
        return total

    def remove_answers(self) -> list[str]:
        """Delete the hidden tests and worktrees; a failure is a warning, not an error."""
        warnings = []
        for path in (self.run_dir / "final" / HIDDEN_TESTS_DIR, self.run_dir / "wt"):
            try:
                remove_tree(path)
            except Exception as exc:  # noqa: BLE001 - the results are already written
                error = f"{type(exc).__name__}: {exc}"
                self.events.emit("cleanup_warning", path=str(path), error=error[:2000])
                warnings.append(f"could not remove {path}: {error}")
        try:
            with self.git_lock:
                gitops.git(self.repo, "worktree", "prune", check=False)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self.events.emit("cleanup_warning", path=str(self.repo), error=error[:2000])
            warnings.append(f"could not prune worktrees of {self.repo}: {error}")
        return warnings


def team_status(states: list[TaskState], final_results: list[dict]) -> str:
    """success, partial, or failed, by the rule of a graph run.

    A failed task or a failed final check makes the run partial; without any
    completed task it is failed. Hidden tests do not count.
    """
    completed = [state for state in states if state.status == "completed"]
    if not completed:
        return "failed"
    if len(completed) == len(states) and all(item["ok"] for item in final_results):
        return "success"
    return "partial"


def run_team(options: TeamOptions, worker: Worker, lead_worker: Worker) -> RunResult:
    """Run the team baseline on ``options.repo``; return the run summary.

    ``lead_worker`` plans with read-only tools; ``worker`` runs the teammates
    and the lead's wrap-up. Raises ``gitops.GitError`` / ``RuntimeError`` /
    ``OSError`` when the run cannot be set up.
    """
    request = options.request_path.read_text(encoding="utf-8")
    repo = Path(options.repo).resolve()
    base_commit = gitops.head(repo)
    preflight = getattr(worker, "preflight", None)
    if preflight is not None:
        preflight()  # SandboxUnavailable (a RuntimeError) stops the run here
    sandbox = worker_sandbox(worker)
    checks = options.checks or checks_for(sandbox)

    run_id = new_run_id()
    run_dir = Path(options.out_dir).resolve() / run_id
    run_dir.mkdir(parents=True)
    config = {
        "run_id": run_id,
        "method": "team",
        "repo": str(repo),
        "base_commit": base_commit,
        "request_id": options.request_path.stem,
        "request": str(options.request_path),
        "workers": options.workers,
        "max_attempts": options.max_attempts,
        "worker_timeout_s": options.worker_timeout_s,
        "lead_timeout_s": options.lead_timeout_s,
        "final_checks": list(options.final_checks),
        "hidden_tests": str(options.hidden_tests) if options.hidden_tests else None,
        "worker": worker.describe(),
        "lead_worker": lead_worker.describe(),
        **sandbox.to_dict(),
        "checks_sandbox": checks.kind,
        "taskgraph_version": TEAM_VERSION,
        "aqours_commit": gitops.source_state(AQOURS_SOURCE),
        "python": platform.python_version(),
        "platform": sys.platform,
    }
    write_json_atomic(run_dir / "config.json", config)
    events = EventLog(run_dir / "events.jsonl")
    events.emit("run_start", run_id=run_id, method="team", workers=options.workers,
                checks_sandbox=checks.kind)

    base = gitops.clone_for_run(repo, run_dir / "repo", base_commit)
    team = Team(request, worker, lead_worker, options, run_dir, events, base, checks)
    plan = team.plan()
    final_results: list[dict] = []
    hidden = None
    if plan is not None:
        team.execute(plan)
        team.wrapup()
        final_results = team.final_checks()
        if options.hidden_tests:
            hidden = team.hidden_tests(Path(options.hidden_tests))
    escapes_total = team.audit_escapes()
    states = list(team.states.values())
    status = team_status(states, final_results)
    wall_time = events.elapsed()

    records = team.records
    summary = {
        "run_id": run_id,
        "method": "team",
        "status": status,
        "reason": "" if plan is not None else "lead_plan_invalid",
        "wall_time_s": wall_time,
        "config": config,
        "nodes": {node_id: asdict(record) for node_id, record in records.items()},
        "totals": {key: sum(getattr(record, key) for record in records.values())
                   for key in ("model_calls", "input_tokens", "output_tokens")},
        "integration_commit": gitops.head(run_dir / "repo"),
        "final_checks": final_results,
        "hidden_tests": hidden,
        **sandbox.to_dict(),
        "checks_sandbox": checks.kind,
        "escape_attempts_total": escapes_total,
        "check_timeout_retries_total": (
            sum(record.check_timeout_retries for record in records.values())
            + team.final_check_timeout_retries),
        "team": {
            "tasks": [state.to_dict() for state in states],
            "n_tasks": len(states),
            "max_parallel": team.max_parallel,
            "conflicts": sum(state.conflict for state in states),
            "conflicts_resolved": sum(state.conflict_resolved for state in states),
            "out_of_scope_total": sum(len(records[state.task.id].out_of_scope_files)
                                      for state in states),
            "failed_tasks": [state.task.id for state in states if state.status == "failed"],
            "wrapup_changed_files": team.wrapup_changed_files,
            "plan_problems": team.plan_problems,
        },
    }
    write_json_atomic(run_dir / "summary.json", summary)
    # Clean up only now: whatever goes wrong here, the results are on disk.
    cleanup_warnings = team.remove_answers()
    events.emit("run_end", status=status, wall_time_s=wall_time)
    return RunResult(run_dir=run_dir, summary=summary, cleanup_warnings=cleanup_warnings)
