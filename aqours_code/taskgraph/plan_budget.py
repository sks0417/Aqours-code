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

``plan --gate`` makes one more model call between the steps, the gate's
location call; it is counted as step :data:`GATE_STEP`, round 1.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .validate import Issue
from .workers import WorkerResult

DEFAULT_MAX_MODEL_CALLS = 150
DEFAULT_MAX_INPUT_TOKENS = 6_000_000
DEFAULT_MAX_PLAN_S = 2400.0
# No round is started with less time than this left.
MIN_ROUND_S = 60.0
# The step of the gate's location call (``plan --gate``), between steps 1 and 2.
GATE_STEP = "gate"
GATE_ROUND = "the gate's location call"


def round_name(step, number: int) -> str:
    """``step 2 round 1``, or the gate's location call."""
    return GATE_ROUND if step == GATE_STEP else f"step {step} round {number}"

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

    def add(self, step: int, number: int, result: WorkerResult) -> None:
        """Count one finished round."""
        self.model_calls += result.model_calls
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.rounds.append({"step": step, "round": number,
                            "model_calls": result.model_calls,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                            "duration_s": round(result.duration_s, 3),
                            "ended": "answer" if result.ok else (result.reason or "worker_error")})

    def so_far(self) -> str:
        return (f"{self.model_calls} model calls, {self.input_tokens:,} input and "
                f"{self.output_tokens:,} output tokens, {self.elapsed():.0f} s")

    def _stop(self, code: str, step: int, number: int, reason: str, message: str) -> Issue:
        self.stopped = {"code": code, "step": step, "round": number, "reason": reason,
                        "message": message}
        return Issue(code, [], message)

    def stop(self, code: str, step, number: int, reason: str, message: str) -> Issue:
        """Record that the plan stops after a round for a reason of its own (the gate)."""
        return self._stop(code, step, number, reason, message)

    def limit_issue(self, step: int, number: int) -> Issue | None:
        """Before a round: a LIMIT error when a limit leaves no room for it."""
        limit = self.reached()
        if not limit:
            return None
        return self._stop(
            "LIMIT", step, number, limit,
            f"planning stopped before {round_name(step, number)}: "
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
                f"planning stopped in {round_name(step, number)}: {self.limits.text(limit)} "
                f"was reached before the planner answered. So far: {self.so_far()}. "
                f"Raise it with {LIMITS[limit][1]}.")
        reason = result.reason or "worker_error"
        if step == GATE_STEP:
            return self._stop(
                "GATE", step, number, reason,
                f"planning stopped: {GATE_ROUND} failed ({reason}): {result.error[-2000:]}. "
                f"Without the gate's decision the plan does not go on. So far: "
                f"{self.so_far()}.")
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
