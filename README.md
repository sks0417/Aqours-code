# codepilot_s20

Function-parity split of learn-claude-code s20 with multi-provider model adapter.

## Requirements

- Python 3.10 or newer

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Copy `.env.example` to `.env`, then fill in the model provider settings and API key you want to use.

```powershell
Copy-Item .env.example .env
```

## Run

```powershell
codepilot-s20
```

## Test

```powershell
python -m pytest -q
```

Pytest is configured to collect only the project unit tests under `tests/`.
The intentionally failing code under `evals/cases/**/workspace/` is eval fixture
material for agents to repair, not part of the project unit test suite.

## Eval

Install the dev dependencies before running evals so graders use the real
pytest package from the controlled Python environment:

```powershell
pip install -e ".[dev]"
```

Eval grading uses a clean-room flow: before the agent runs, the runner creates
a trusted baseline copy of `task.md`, `metadata.yaml`, `grader.py`,
`grader_tests/`, and `workspace/`. The agent edits an isolated
`agent_workspace`; the runner records a change manifest, verifies that the
trusted case files were not modified, creates a fresh `grading_workspace` from
the trusted baseline, and applies only `allowed_changes`.

Symlinks are recorded in manifests but are never submitted to the grading
workspace. If case/grader files change during a run, the case fails with
`constraint_violation`. Grader pytest runs use `sys.executable -m pytest` with
plugin autoloading and user site packages disabled for reproducibility.

This is still not a strong OS sandbox. The `bash` tool runs with the host
process permissions; truly adversarial isolation requires a future container or
separate restricted process.
