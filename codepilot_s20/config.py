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

from .model_api import build_model_client, default_model_for_provider, provider_from_env

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


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

WORKDIR = Path(os.getenv("CODEPILOT_S20_WORKDIR", Path.cwd())).resolve()
MODEL_PROVIDER = provider_from_env()
MODEL = os.getenv("MODEL_ID", default_model_for_provider(MODEL_PROVIDER))
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

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
        _CLIENT = build_model_client(provider)
        _CLIENT_PROVIDER = provider
    return _CLIENT


class LazyModelClient:
    @property
    def messages(self):
        return get_model_client().messages


client = LazyModelClient()

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = _env_int("MODEL_MAX_RETRIES", 3)
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
TRACE_CLEANUP_ENABLED = _env_bool("TRACE_CLEANUP_ENABLED", True)
TRACE_RETENTION_MAX_DAYS = _env_float("TRACE_RETENTION_MAX_DAYS", 7)
TRACE_RETENTION_MAX_RUNS = _env_int("TRACE_RETENTION_MAX_RUNS", 100)
TRACE_RETENTION_MAX_MB = _env_float("TRACE_RETENTION_MAX_MB", 300)
TRACE_MAX_RUN_MB = _env_float("TRACE_MAX_RUN_MB", 20)
TRACE_KEEP_PINNED = _env_bool("TRACE_KEEP_PINNED", True)
