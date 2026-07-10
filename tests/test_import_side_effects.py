import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def clean_env(tmp_path):
    env = os.environ.copy()
    for key in [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "MODEL_API_KEY",
    ]:
        env.pop(key, None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["CODEPILOT_S20_WORKDIR"] = str(tmp_path / "agent-workdir")
    return env


def run_python(script: str, tmp_path):
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=clean_env(tmp_path),
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_package_import_without_env_or_api_key(tmp_path):
    result = run_python(
        "import codepilot_s20; print('import ok')",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import ok"


def test_config_import_does_not_build_model_client(tmp_path):
    result = run_python(
        """
import codepilot_s20.model_api as model_api
def fail(provider):
    raise RuntimeError("build_model_client called")
model_api.build_model_client = fail
import codepilot_s20.config
print("config import ok")
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "config import ok"


def test_package_import_does_not_start_scheduler_or_create_workdir(tmp_path):
    result = run_python(
        """
import os
import threading
from pathlib import Path
import codepilot_s20
print(any(t.name == "codepilot-s20-cron" for t in threading.enumerate()))
print(Path(os.environ["CODEPILOT_S20_WORKDIR"]).exists())
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["False", "False"]
