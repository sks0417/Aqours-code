"""The limits of one ``plan`` across all its model rounds, and what the rounds used.

A plan runs Step 1 (the draft items) once or twice and Step 2 once plus up to
``max_revisions`` revision rounds, each a new agent. Planning dask 2023.8.0
spent 15M input tokens in three rounds and gave no graph, so a plan has three
limits, over all rounds of both steps: model calls, input tokens and wall time
(``plan --max-calls``, ``--max-input-tokens``, ``--max-time``; 0 turns one off).

Each round gets what is left: its timeout is cut to the time left, and the
calls and input tokens left go to the planner agent as a model budget
(``WorkerRequest.model_budget``), which the Aqours loop reads through the
call-budget interface of ``aqours_code.model_budget``: it asks for the final
answer near the end and disables tools for the last call. No round starts
when a limit is reached.

A round whose agent failed (``worker_error``, ``worker_timeout``,
``model_budget``) gave no answer, so it is not revised: the plan stops with an
error naming the step and round, the reason, and the calls and tokens used.

With ``plan --facts per-item`` several investigators run at once in Step 2;
:class:`SharedBudget` keeps the plan's limits for all of them together, and
:class:`InvestigatorLimits` are the limits of each run.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from .validate import Issue
from .workers import WorkerResult

DEFAULT_MAX_MODEL_CALLS = 150
DEFAULT_MAX_INPUT_TOKENS = 6_000_000
DEFAULT_MAX_PLAN_S = 2400.0
# No round is started with less time than this left.
MIN_ROUND_S = 60.0

# limit -> (unit in messages, option of ``plan``)
LIMITS = {"model_calls": ("model calls", "--max-calls"),
          "input_tokens": ("input tokens", "--max-input-tokens"),
          "seconds": ("s", "--max-time")}


@dataclass
class PlanLimits:
    """Limits of one plan; None turns a limit off."""

    model_calls: int | None = DEFAULT_MAX_MODEL_CALLS
    input_tokens: int | None = DEFAULT_MAX_INPUT_TOKENS
    seconds: float | None = DEFAULT_MAX_PLAN_S

    def text(self, limit: str) -> str:
        """``the plan's limit of 150 model calls``."""
        value = getattr(self, limit)
        unit, _option = LIMITS[limit]
        amount = f"{value:,.0f}" if limit != "seconds" else f"{value:.0f}"
        return f"the plan's limit of {amount} {unit}"


NO_LIMITS = PlanLimits(None, None, None)


class PlanBudget:
    """What the rounds of one plan used, against its limits."""

    def __init__(self, limits: PlanLimits | None = None, started: float | None = None):
        self.limits = limits if limits is not None else NO_LIMITS
        self.started = time.monotonic() if started is None else started
        self.model_calls = self.input_tokens = self.output_tokens = 0
        self.rounds: list[dict] = []  # per round: step, round, usage, how it ended
        self.stopped: dict | None = None  # why the plan stopped early, if it did
        self._cut = False  # the last round's timeout was cut to the time left

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def seconds_left(self) -> float | None:
        if self.limits.seconds is None:
            return None
        return self.limits.seconds - self.elapsed()

    def reached(self) -> str:
        """The first limit that leaves no room for another round, or ""."""
        if self.limits.model_calls is not None and self.model_calls >= self.limits.model_calls:
            return "model_calls"
        if (self.limits.input_tokens is not None
                and self.input_tokens >= self.limits.input_tokens):
            return "input_tokens"
        left = self.seconds_left()
        if left is not None and left < MIN_ROUND_S:
            return "seconds"
        return ""

    def request_fields(self, timeout_s: float) -> dict:
        """``timeout_s`` and ``model_budget`` of the next round's WorkerRequest."""
        left = self.seconds_left()
        timeout = timeout_s if left is None else max(0.0, min(timeout_s, left))
        self._cut = timeout < timeout_s
        budget = {
            "max_calls": (None if self.limits.model_calls is None
                          else max(0, self.limits.model_calls - self.model_calls)),
            "max_input_tokens": (None if self.limits.input_tokens is None
                                 else max(0, self.limits.input_tokens - self.input_tokens)),
        }
        has_limit = any(value is not None for value in budget.values())
        return {"timeout_s": timeout, "model_budget": budget if has_limit else None}

    def add(self, step: int, number: int, result: WorkerResult, item: str | None = None) -> None:
        """Count one finished round (with ``item``: one investigator of a per-item round)."""
        self.model_calls += result.model_calls
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        entry = {"step": step, "round": number}
        if item is not None:
            entry["item"] = item
        entry.update({"model_calls": result.model_calls,
                      "input_tokens": result.input_tokens,
                      "output_tokens": result.output_tokens,
                      "duration_s": round(result.duration_s, 3),
                      "ended": "answer" if result.ok else (result.reason or "worker_error")})
        self.rounds.append(entry)

    def so_far(self) -> str:
        return (f"{self.model_calls} model calls, {self.input_tokens:,} input and "
                f"{self.output_tokens:,} output tokens, {self.elapsed():.0f} s")

    def _stop(self, code: str, step: int, number: int, reason: str, message: str) -> Issue:
        self.stopped = {"code": code, "step": step, "round": number, "reason": reason,
                        "message": message}
        return Issue(code, [], message)

    def stop(self, code: str, step: int, number: int, reason: str, message: str,
             item: str | None = None) -> Issue:
        """Record why the plan stopped early (``item``: whose investigator) and return the error."""
        issue = self._stop(code, step, number, reason, message)
        if item is not None:
            self.stopped["item"] = item
        return issue

    def limit_issue(self, step: int, number: int) -> Issue | None:
        """Before a round: a LIMIT error when a limit leaves no room for it."""
        limit = self.reached()
        if not limit:
            return None
        return self._stop(
            "LIMIT", step, number, limit,
            f"planning stopped before step {step} round {number}: "
            f"{self.limits.text(limit)} was reached. So far: {self.so_far()}. "
            f"Raise it with {LIMITS[limit][1]}.")

    def failure_issue(self, step: int, number: int, result: WorkerResult) -> Issue:
        """After a round whose agent failed: stop (LIMIT when a plan limit ended it)."""
        limit = ""
        if result.reason == "model_budget":
            limit = self.reached() or next(
                (name for name in ("input_tokens", "model_calls") if name in result.error),
                "model_calls")
        elif result.reason == "worker_timeout" and self._cut:
            limit = "seconds"
        if limit:
            return self._stop(
                "LIMIT", step, number, limit,
                f"planning stopped in step {step} round {number}: {self.limits.text(limit)} "
                f"was reached before the planner answered. So far: {self.so_far()}. "
                f"Raise it with {LIMITS[limit][1]}.")
        reason = result.reason or "worker_error"
        return self._stop(
            "AGENT", step, number, reason,
            f"planning stopped: the planner agent of step {step} round {number} failed "
            f"({reason}): {result.error[-2000:]}. A revision is only for an answer the "
            f"model gave, so none was started. So far: {self.so_far()}.")

    def report(self) -> dict:
        """The plan report's fields about limits and usage."""
        return {"limits": asdict(self.limits),
                "used": {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                         "output_tokens": self.output_tokens,
                         "seconds": round(self.elapsed(), 3)},
                "stopped": self.stopped,
                "round_usage": self.rounds}


# ── investigators running at the same time (``plan --facts per-item``) ──

DEFAULT_INVESTIGATOR_CALLS = 40
DEFAULT_INVESTIGATOR_INPUT_TOKENS = 1_500_000
DEFAULT_INVESTIGATOR_S = 900.0

INVESTIGATOR_LIMITS = {"model_calls": "--investigator-max-calls",
                       "input_tokens": "--investigator-max-input-tokens",
                       "seconds": "--investigator-max-time"}


@dataclass
class InvestigatorLimits:
    """Limits of one investigator run; None turns a limit off."""

    model_calls: int | None = DEFAULT_INVESTIGATOR_CALLS
    input_tokens: int | None = DEFAULT_INVESTIGATOR_INPUT_TOKENS
    seconds: float | None = DEFAULT_INVESTIGATOR_S


def _least(*values):
    present = [value for value in values if value is not None]
    return min(present) if present else None


@dataclass
class Reservation:
    """What one investigator run may use: its limits, cut to what the plan has left."""

    model_calls: int | None
    input_tokens: int | None
    timeout_s: float
    # the part of the plan's limits held for it while it runs (0 when the plan has none)
    held_calls: int = 0
    held_tokens: int = 0
    # which of its limits are smaller than the investigator's own, because of the plan's
    cut_by_plan: tuple[str, ...] = ()

    def model_budget(self) -> dict | None:
        """``WorkerRequest.model_budget``."""
        if self.model_calls is None and self.input_tokens is None:
            return None
        return {"max_calls": self.model_calls, "max_input_tokens": self.input_tokens}

    def to_dict(self) -> dict:
        return {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                "seconds": round(self.timeout_s, 3), "cut_by_plan": list(self.cut_by_plan)}


class SharedBudget:
    """The plan's limits shared by investigators that run at the same time.

    The model calls and input tokens of the plan cannot be counted live
    across child processes, so they are reserved: a starting investigator
    gets its own limits (:class:`InvestigatorLimits`), cut to what the plan
    has left after the reservations of the investigators still running, and
    its reservation is held until it ends. What it used is then counted and
    the rest is freed. An investigator that finds nothing left waits while
    others run (they may free some); with none running, it does not start.
    Its timeout is the round timeout, its own time limit and the plan's time
    left, whichever is smallest. All of the plan's limits are so kept for all
    investigators together.
    """

    def __init__(self, budget: PlanBudget, limits: InvestigatorLimits):
        self.budget = budget
        self.limits = limits
        self.held_calls = self.held_tokens = 0
        self.running = 0
        self._condition = threading.Condition()

    def _free(self) -> tuple[int | None, int | None]:
        plan = self.budget.limits
        calls = (None if plan.model_calls is None
                 else plan.model_calls - self.budget.model_calls - self.held_calls)
        tokens = (None if plan.input_tokens is None
                  else plan.input_tokens - self.budget.input_tokens - self.held_tokens)
        return calls, tokens

    def _exhausted(self) -> str:
        """The plan limit that leaves nothing for another investigator now, or ""."""
        calls, tokens = self._free()
        if calls is not None and calls <= 0:
            return "model_calls"
        if tokens is not None and tokens <= 0:
            return "input_tokens"
        left = self.budget.seconds_left()
        if left is not None and left < MIN_ROUND_S:
            return "seconds"
        return ""

    def reserve(self, timeout_s: float) -> Reservation | str:
        """A reservation for one investigator, or the plan limit that prevents it."""
        with self._condition:
            while True:
                limit = self._exhausted()
                if not limit:
                    break
                if limit == "seconds" or self.running == 0:
                    return limit
                self._condition.wait()
            calls, tokens = self._free()
            own = self.limits
            model_calls = _least(own.model_calls, calls)
            input_tokens = _least(own.input_tokens, tokens)
            left = self.budget.seconds_left()
            timeout = _least(timeout_s, own.seconds, left)
            cut = tuple(name for name, value, mine in (
                ("model_calls", model_calls, own.model_calls),
                ("input_tokens", input_tokens, own.input_tokens),
                ("seconds", timeout, _least(timeout_s, own.seconds)))
                if value is not None and (mine is None or value < mine))
            reservation = Reservation(model_calls, input_tokens, max(0.0, timeout),
                                      held_calls=model_calls if calls is not None else 0,
                                      held_tokens=input_tokens if tokens is not None else 0,
                                      cut_by_plan=cut)
            self.held_calls += reservation.held_calls
            self.held_tokens += reservation.held_tokens
            self.running += 1
            return reservation

    def release(self, reservation: Reservation, step: int, number: int,
                result: WorkerResult | None, item: str) -> None:
        """Count what an investigator used (``result`` None: it raised) and free the rest."""
        with self._condition:
            self.held_calls -= reservation.held_calls
            self.held_tokens -= reservation.held_tokens
            self.running -= 1
            if result is not None:
                self.budget.add(step, number, result, item=item)
            self._condition.notify_all()
