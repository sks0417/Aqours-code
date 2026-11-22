from __future__ import annotations

import ast, json, os, random, re, subprocess, threading, time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    class _YamlFallback:
        class YAMLError(Exception): pass
        @staticmethod
        def safe_load(text): return {}
    yaml = _YamlFallback()

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): return False

from collections.abc import Mapping as _Mapping

from .model_api import (
    build_model_client,
    effective_escalated_max_tokens,
    effective_initial_max_tokens,
    provider_from_env,
)
from .command_executor import LocalCommandExecutor

load_dotenv(override=False)


def _clean_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

WORKDIR = Path(os.getenv("AQOURS_CODE_WORKDIR", Path.cwd())).resolve()
COMMAND_EXECUTOR = LocalCommandExecutor()
TOOL_POLICY = None
CASE_DEADLINE = None
CURRENT_ROOT_TASK = ""
BACKGROUND_TASKS_ENABLED = True
APPROVAL_MODE = "interactive"
MODEL_PROVIDER = provider_from_env()
MODEL = _clean_env("AQOURS_CODE_MODEL")
BASE_URL = _clean_env("AQOURS_CODE_BASE_URL")
API_KEY = _clean_env("AQOURS_CODE_API_KEY")
PRIMARY_MODEL = MODEL
# v0.1 has one authoritative model setting and never silently switches models.
FALLBACK_MODEL = None

_CLIENT = None
_CLIENT_PROVIDER = None


def _active_model_provider() -> str:
    try:
        from . import runtime_state as _state
        return getattr(_state, "MODEL_PROVIDER", MODEL_PROVIDER)
    except Exception:
        return MODEL_PROVIDER


def get_model_client(provider: str | None = None):
    global _CLIENT, _CLIENT_PROVIDER
    provider = provider or _active_model_provider()
    if _CLIENT is None or _CLIENT_PROVIDER != provider:
        _CLIENT = build_model_client(
            provider,
            api_key=API_KEY,
            base_url=BASE_URL,
        )
        _CLIENT_PROVIDER = provider
    return _CLIENT


def validate_runtime_configuration() -> None:
    """Fail before the interactive loop when the public model settings are incomplete."""
    supported = {"anthropic", "deepseek", "openai", "openai_compatible"}
    if MODEL_PROVIDER not in supported:
        raise RuntimeError(
            "AQOURS_CODE_PROVIDER must be one of: " + ", ".join(sorted(supported))
        )
    missing = [
        name
        for name, value in (
            ("AQOURS_CODE_API_KEY", API_KEY),
            ("AQOURS_CODE_BASE_URL", BASE_URL),
            ("AQOURS_CODE_MODEL", MODEL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in your provider settings."
        )
    if not 16_000 <= AGENT_CONTEXT_LIMIT_TOKENS <= 1_000_000:
        raise RuntimeError(
            "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS (or legacy "
            "AQOURS_CODE_CONTEXT_LIMIT_TOKENS) must be between 16000 and "
            "1000000"
        )
    if not 1_000 <= COMPACT_TRIGGER_TOKENS <= AGENT_CONTEXT_LIMIT_TOKENS:
        raise RuntimeError(
            "AQOURS_CODE_COMPACT_TRIGGER_TOKENS must be between 1000 and "
            "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS"
        )
    if not 16_000 <= SUMMARY_INPUT_LIMIT_TOKENS <= 1_000_000:
        raise RuntimeError(
            "AQOURS_CODE_SUMMARY_INPUT_LIMIT_TOKENS must be between 16000 "
            "and 1000000"
        )
    if not 1_000 <= SUMMARY_MAX_TOKENS <= 64_000:
        raise RuntimeError(
            "AQOURS_CODE_SUMMARY_MAX_TOKENS must be between 1000 and 64000"
        )
    if MAX_TOKENS_ERROR:
        raise RuntimeError(MAX_TOKENS_ERROR)


class LazyModelClient:
    @property
    def messages(self):
        return get_model_client().messages


client = LazyModelClient()

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
# The output limit (max_tokens) of a model call, and the limit of the one retry
# after a reply cut at that limit. DeepSeek models with a 128K output limit
# ignore both (model_api.effective_initial_max_tokens).
MAX_TOKENS_ENV = "AQOURS_CODE_MAX_TOKENS"
ESCALATED_MAX_TOKENS_ENV = "AQOURS_CODE_ESCALATED_MAX_TOKENS"
BUILTIN_MAX_TOKENS = 8_000
BUILTIN_ESCALATED_MAX_TOKENS = 16_000
_WHOLE_NUMBER = re.compile(r"[0-9]+")


def parse_max_tokens(environ: _Mapping[str, str]) -> tuple[int, int, str]:
    """``(initial, escalated, error)`` from AQOURS_CODE_MAX_TOKENS and
    AQOURS_CODE_ESCALATED_MAX_TOKENS in ``environ``.

    An unset or empty variable takes its default (8000 and 16000). Each must
    be a positive whole number, and the escalated limit must be at least the
    initial one. Otherwise ``error`` says what is wrong, and both limits are
    the defaults (``configured_max_tokens`` then refuses to give them).
    """
    values, errors, defaulted = {}, [], set()
    for name, default in ((MAX_TOKENS_ENV, BUILTIN_MAX_TOKENS),
                          (ESCALATED_MAX_TOKENS_ENV, BUILTIN_ESCALATED_MAX_TOKENS)):
        raw = str(environ.get(name) or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1].strip()
        if not raw:
            values[name] = default
            defaulted.add(name)
        elif _WHOLE_NUMBER.fullmatch(raw) and int(raw) > 0:
            values[name] = int(raw)
        else:
            errors.append(f"{name} must be a positive whole number, got {raw!r}")
    if not errors and values[ESCALATED_MAX_TOKENS_ENV] < values[MAX_TOKENS_ENV]:
        initial, escalated = values[MAX_TOKENS_ENV], values[ESCALATED_MAX_TOKENS_ENV]
        if ESCALATED_MAX_TOKENS_ENV in defaulted:
            errors.append(f"{ESCALATED_MAX_TOKENS_ENV} must be at least {MAX_TOKENS_ENV} "
                          f"({initial}); it is not set, and its default is {escalated}: "
                          f"set {ESCALATED_MAX_TOKENS_ENV} as well")
        else:
            errors.append(f"{ESCALATED_MAX_TOKENS_ENV} ({escalated}) must be at least "
                          f"{MAX_TOKENS_ENV} ({initial})")
    if errors:
        return BUILTIN_MAX_TOKENS, BUILTIN_ESCALATED_MAX_TOKENS, "; ".join(errors)
    return values[MAX_TOKENS_ENV], values[ESCALATED_MAX_TOKENS_ENV], ""


DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_TOKENS_ERROR = parse_max_tokens(os.environ)


def configured_max_tokens() -> tuple[int, int]:
    """The configured ``(initial, escalated)`` output limits, read at each use.

    Raises ``RuntimeError`` when AQOURS_CODE_MAX_TOKENS or
    AQOURS_CODE_ESCALATED_MAX_TOKENS is invalid, so no model call is made
    with a limit nobody asked for.
    """
    if MAX_TOKENS_ERROR:
        raise RuntimeError(MAX_TOKENS_ERROR)
    return DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS


def model_max_tokens(provider: str, model: str) -> dict:
    """The output limits a call to ``model`` actually uses: ``{"initial", "escalated"}``.

    ``escalated`` is the limit of the retry after a reply cut at ``initial``;
    when it equals ``initial`` there is no such retry.
    """
    initial, escalated = configured_max_tokens()
    first = effective_initial_max_tokens(provider, model,
                                         configured_default_max_tokens=initial)
    return {"initial": first,
            "escalated": effective_escalated_max_tokens(
                provider, model, current_max_tokens=first,
                configured_escalated_max_tokens=escalated)}


MAX_RETRIES = _env_int("AQOURS_CODE_MODEL_MAX_RETRIES", 3)
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 0
BASE_DELAY_MS = 500
# The harness estimates serialized request characters because Providers do not
# share one tokenizer. Public context and compact settings remain token-based;
# each distinct budget is converted independently at its point of use.
CONTEXT_CHARS_PER_TOKEN = 3
AGENT_CONTEXT_LIMIT_TOKENS = _env_int(
    "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS",
    _env_int("AQOURS_CODE_CONTEXT_LIMIT_TOKENS", 200_000),
)
# Compatibility aliases for embedders that still import the old names.
CONTEXT_LIMIT_TOKENS = AGENT_CONTEXT_LIMIT_TOKENS
CONTEXT_LIMIT = AGENT_CONTEXT_LIMIT_TOKENS * CONTEXT_CHARS_PER_TOKEN
COMPACT_TRIGGER_TOKENS = _env_int(
    "AQOURS_CODE_COMPACT_TRIGGER_TOKENS", 80_000,
)
COMPACT_TRIGGER_RATIO = (
    COMPACT_TRIGGER_TOKENS / AGENT_CONTEXT_LIMIT_TOKENS
)
SUMMARY_INPUT_LIMIT_TOKENS = _env_int(
    "AQOURS_CODE_SUMMARY_INPUT_LIMIT_TOKENS", 256_000,
)
SUMMARY_MAX_TOKENS = _env_int(
    "AQOURS_CODE_SUMMARY_MAX_TOKENS", 6_000,
)
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
TRACE_CLEANUP_ENABLED = _env_bool("AQOURS_CODE_TRACE_CLEANUP_ENABLED", True)
TRACE_RETENTION_MAX_DAYS = _env_float("AQOURS_CODE_TRACE_RETENTION_MAX_DAYS", 7)
TRACE_RETENTION_MAX_RUNS = _env_int("AQOURS_CODE_TRACE_RETENTION_MAX_RUNS", 100)
TRACE_RETENTION_MAX_MB = _env_float("AQOURS_CODE_TRACE_RETENTION_MAX_MB", 300)
TRACE_MAX_RUN_MB = _env_float("AQOURS_CODE_TRACE_MAX_RUN_MB", 20)
TRACE_KEEP_PINNED = _env_bool("AQOURS_CODE_TRACE_KEEP_PINNED", True)
