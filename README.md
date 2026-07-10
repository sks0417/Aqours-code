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

Eval grading uses a clean-room flow: the agent edits an isolated
`agent_workspace`, the runner records a change manifest, then a fresh
`grading_workspace` is copied from the original case fixture and only
`allowed_changes` are applied before the grader runs.
