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

Docker execution follows one small pipeline:

```text
Host prepares a disposable case copy
-> one-shot Agent container runs the complete Harness
-> Host collects workspace changes, Trace, timeline, final, stdout, and stderr
-> separate one-shot Grader container applies allowed changes to a clean copy
-> trusted tests produce the final summary
```

The Agent container runs the normal `run_agent_task()` with all 28 built-in
tools, dynamic MCP tools, Skill, Memory, Task, Cron, Background, Subagent,
Teammate, Protocol, and Worktree support. It receives only `/workspace`,
per-case `/state`, `/runtime`, and the model forwarding directories. The host
project, API keys, Docker socket, trusted grader, and grading workspace are not
mounted. The container is network-disabled, non-root, and resource limited.

The host-side Model Broker exists only so API keys stay on the host. It accepts
schema-checked `messages.create` calls for the selected model and applies one
case-wide call budget and requested-token budget. These budgets cover the main
Agent and all nested agents together. Main-Agent LLM rounds and tool calls are
not execution budgets; Trace records them as `llm_requests` and `tool_calls`
for reporting and process/efficiency grading.

After the Agent exits, the runner verifies the trusted case did not change,
creates `grading_workspace` from the original workspace, and applies only
`allowed_changes`. The independent Grader receives the clean case and
trace/final/stdout/stderr inputs read-only. This prevents modified public tests
or grader files from becoming the source of truth.

`--docker-timeout` is the wall-clock deadline for preparation, Agent execution,
grading, and cleanup. Background work may run inside the Agent container; a
one-shot Eval waits for it only until that deadline and injects completed output
as `task_notification`. Interactive CLI sessions keep true background behavior.

Docker is the default. `--scripted` changes only the host-side model response;
the complete Agent and Grader containers still run:

```powershell
python evals/run_eval.py --scripted --execution docker --docker-build
python evals/run_eval.py --execution docker --case mini_auth_service_security_fix
```

The disposable Git baseline trusts only `/workspace` and
`/state/.worktrees/*` through process-local configuration, which keeps Windows
Docker Desktop and Worktree operations usable without changing the non-root
container user or writing host Git configuration. Docker CLI output is decoded
as UTF-8 with replacement for invalid bytes.

Local execution remains an explicit development option and never receives an
automatic fallback from Docker failures:

```powershell
python evals/run_eval.py --scripted --execution local
```

Docker startup/build failure is a hard eval failure and never falls back to
local execution.
