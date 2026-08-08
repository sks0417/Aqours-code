from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "AQOURS_CODE_API_KEY",
        "AQOURS_CODE_BASE_URL",
        "AQOURS_CODE_MODEL",
        "AQOURS_CODE_PROVIDER",
        "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS",
        "AQOURS_CODE_CONTEXT_LIMIT_TOKENS",
        "AQOURS_CODE_COMPACT_TRIGGER_TOKENS",
        "AQOURS_CODE_SUMMARY_INPUT_LIMIT_TOKENS",
        "AQOURS_CODE_SUMMARY_MAX_TOKENS",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return env


def test_module_help_does_not_require_model_configuration(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "aqours_code", "--help"],
        cwd=tmp_path,
        env=_clean_env(),
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: Aqours_code" in result.stdout
    assert "AQOURS_CODE_API_KEY" not in result.stdout


def test_cli_fails_early_with_actionable_missing_configuration(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "aqours_code"],
        cwd=tmp_path,
        env=_clean_env(),
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "Aqours_code configuration error" in result.stderr
    assert "AQOURS_CODE_API_KEY" in result.stderr
    assert "AQOURS_CODE_BASE_URL" in result.stderr
    assert "AQOURS_CODE_MODEL" in result.stderr


def test_cli_rejects_out_of_range_context_window(tmp_path):
    env = _clean_env()
    env.update({
        "AQOURS_CODE_PROVIDER": "openai_compatible",
        "AQOURS_CODE_API_KEY": "test-key",
        "AQOURS_CODE_BASE_URL": "https://example.invalid/v1",
        "AQOURS_CODE_MODEL": "test-model",
        "AQOURS_CODE_CONTEXT_LIMIT_TOKENS": "8000",
    })
    result = subprocess.run(
        [sys.executable, "-m", "aqours_code"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "AQOURS_CODE_CONTEXT_LIMIT_TOKENS" in result.stderr


def test_context_window_environment_setting_is_token_based(tmp_path):
    env = _clean_env()
    env["AQOURS_CODE_CONTEXT_LIMIT_TOKENS"] = "96000"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aqours_code.config import CONTEXT_LIMIT; print(CONTEXT_LIMIT)",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "288000"


def test_new_context_and_summary_environment_settings_are_token_based(tmp_path):
    env = _clean_env()
    env.update({
        "AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS": "160000",
        "AQOURS_CODE_CONTEXT_LIMIT_TOKENS": "96000",
        "AQOURS_CODE_COMPACT_TRIGGER_TOKENS": "120000",
        "AQOURS_CODE_SUMMARY_INPUT_LIMIT_TOKENS": "300000",
        "AQOURS_CODE_SUMMARY_MAX_TOKENS": "7000",
    })
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from aqours_code.config import "
                "AGENT_CONTEXT_LIMIT_TOKENS, CONTEXT_LIMIT, "
                "COMPACT_TRIGGER_TOKENS, SUMMARY_INPUT_LIMIT_TOKENS, "
                "SUMMARY_MAX_TOKENS; "
                "print(AGENT_CONTEXT_LIMIT_TOKENS, CONTEXT_LIMIT, "
                "COMPACT_TRIGGER_TOKENS, SUMMARY_INPUT_LIMIT_TOKENS, "
                "SUMMARY_MAX_TOKENS)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "160000 480000 120000 300000 7000"


def test_configured_cli_starts_without_printing_api_key(tmp_path):
    env = _clean_env()
    env.update({
        "AQOURS_CODE_PROVIDER": "openai_compatible",
        "AQOURS_CODE_API_KEY": "sk-test-must-not-appear",
        "AQOURS_CODE_BASE_URL": "https://example.invalid/v1",
        "AQOURS_CODE_MODEL": "test-model",
        "AQOURS_CODE_WORKDIR": str(tmp_path),
    })
    result = subprocess.run(
        [sys.executable, "-m", "aqours_code"],
        cwd=tmp_path,
        env=env,
        input="q\n",
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "Aqours_code 0.1.0" in result.stdout
    assert f"Workspace: {tmp_path.resolve()}" in result.stdout
    assert "Model: test-model (openai_compatible)" in result.stdout
    assert "sk-test-must-not-appear" not in result.stdout
    assert "sk-test-must-not-appear" not in result.stderr
