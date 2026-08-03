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

from .model_api import build_model_client, provider_from_env
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
MAX_RETRIES = _env_int("AQOURS_CODE_MODEL_MAX_RETRIES", 3)
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 128000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
TRACE_CLEANUP_ENABLED = _env_bool("AQOURS_CODE_TRACE_CLEANUP_ENABLED", True)
TRACE_RETENTION_MAX_DAYS = _env_float("AQOURS_CODE_TRACE_RETENTION_MAX_DAYS", 7)
TRACE_RETENTION_MAX_RUNS = _env_int("AQOURS_CODE_TRACE_RETENTION_MAX_RUNS", 100)
TRACE_RETENTION_MAX_MB = _env_float("AQOURS_CODE_TRACE_RETENTION_MAX_MB", 300)
TRACE_MAX_RUN_MB = _env_float("AQOURS_CODE_TRACE_MAX_RUN_MB", 20)
TRACE_KEEP_PINNED = _env_bool("AQOURS_CODE_TRACE_KEEP_PINNED", True)
