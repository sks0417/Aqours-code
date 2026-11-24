from .runtime_state import *
from . import config as _config
from .provider_errors import RateLimitRetriesExhausted
from .provider_errors import is_rate_limit_error as _is_rate_limit_error
from .provider_errors import is_transient_provider_error as _is_transient_provider_error
from .provider_errors import provider_error_kind as _provider_error_kind
from .provider_errors import rate_limit_wait as _rate_limit_wait

# ── Error Recovery ──

class RecoveryState:
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt: int) -> float:
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState):
    # AQOURS_CODE_RATE_LIMIT_RETRIES / AQOURS_CODE_RATE_LIMIT_MAX_DELAY: an
    # invalid setting stops here, before any model call.
    rate_limit_retries, rate_limit_max_delay = _config.configured_rate_limit()
    attempt = 0  # failures other than 429, at most MAX_RETRIES as before
    rate_limited = 0  # retries after 429, counted apart
    while attempt < MAX_RETRIES:
        try:
            result = fn()
            state.consecutive_529 = 0
            _record_broker_rate_limit_retries(result)
            return result
        except Exception as e:
            # Docker Broker retries Provider transport failures only after the
            # previous Host request has ended. Retrying the logical IPC call
            # again here could multiply attempts or overlap unknown IPC state.
            # (RateLimitRetriesExhausted is marked the same way: its retries
            # are done.)
            if getattr(e, "retry_managed", False):
                _record_broker_rate_limit_retries(e)
                raise
            name = type(e).__name__.lower()
            msg = str(e).lower()
            kind = _provider_error_kind(e)
            if _is_rate_limit_error(e, kind):
                # 429: Retry-After, else 10, 20, 40, 60 ... s; given up with
                # the provider's message once the retries are used up or the
                # wait would pass the run's deadline.
                if rate_limited >= rate_limit_retries:
                    raise RateLimitRetriesExhausted(
                        e, retries=rate_limited, max_retries=rate_limit_retries) from e
                wait = _rate_limit_wait(e, rate_limited + 1, rate_limit_max_delay)
                if _past_deadline(wait.delay_s):
                    raise RateLimitRetriesExhausted(
                        e, retries=rate_limited, max_retries=rate_limit_retries,
                        reason="deadline") from e
                rate_limited += 1
                record_rate_limit_retry(
                    source="direct", **wait.record(rate_limited, rate_limit_retries))
                note = ""
                if wait.retry_after:
                    note = (f" (Retry-After {wait.retry_after_s:g}s"
                            f"{', capped' if wait.capped else ''})")
                print(f"  \033[33m[429] retry {rate_limited}/{rate_limit_retries} "
                      f"after {wait.delay_s:.1f}s{note}\033[0m")
                _sleep(wait.delay_s)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    state.current_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                _sleep(delay)
                attempt += 1
                continue
            # A timeout, a reset or refused connection, a TLS error or a 5xx:
            # the same failures the Docker Broker retries (provider_errors).
            if _is_transient_provider_error(kind):
                delay = retry_delay(attempt)
                # The last attempt raises the error itself, so the caller sees
                # what failed; a retry never starts after the agent's deadline.
                if attempt + 1 >= MAX_RETRIES or _past_deadline(delay):
                    raise
                print(f"  \033[33m[{kind}] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                _sleep(delay)
                attempt += 1
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def _record_broker_rate_limit_retries(outcome) -> None:
    """Trace the 429 retries the Docker Broker made on the host for this call.

    BrokerModelClient puts them on its response, or on the BrokerRemoteError
    it raises, as ``rate_limit_retries``; other clients have none.
    """
    records = getattr(outcome, "rate_limit_retries", None)
    if not isinstance(records, (list, tuple)):
        return
    for record in records:
        if isinstance(record, dict):
            try:
                record_rate_limit_retry(source="broker", **record)
            except TypeError:
                continue


def _sleep(seconds: float) -> None:
    """Wait before a retry (tests replace it)."""
    time.sleep(seconds)


def _past_deadline(delay: float) -> bool:
    """Whether a retry after ``delay`` seconds would start after the run's deadline."""
    return CASE_DEADLINE is not None and time.monotonic() + delay >= CASE_DEADLINE


def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
