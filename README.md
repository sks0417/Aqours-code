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

Docker full-runtime execution is the safe default. `--scripted` changes only
the host-side model implementation; it still runs the complete Agent container:

```powershell
python evals/run_eval.py --scripted --docker-build
```

Docker mode starts one one-shot container whose entrypoint runs the normal
`run_agent_task()` with `LocalCommandExecutor`. Agent Loop, file tools, Bash,
Skill, Memory, persistent Task, subagent, teammate, protocol, Worktree, Cron,
background work, and MCP handlers all execute inside that container. The full
28-tool policy and dynamically connected `mcp__...` tools are recorded in the
trace. Memory, skill catalog, MCP state, and active teammate state are assembled
from a fresh per-case state tree.

The Agent image contains an immutable installed copy of `codepilot_s20` and Git.
It receives only the writable case workspace at `/workspace`, isolated Harness
state at `/state`, result/trace storage at `/runtime`, and a narrow Model Broker
IPC at `/broker`. The original project tree, host `.env`, Docker socket,
`trusted_eval`, and `grading_workspace` are never mounted. A disposable Git
baseline is initialized inside the container so Worktree operations never run
host Git.

The network-disabled container has no model credentials. Its
`BrokerModelClient` writes case-scoped, schema-validated `messages.create`
requests to per-case IPC. The nonce selects the case channel; it is not treated
as authentication from code already running in that container. A host broker
binds the one allowed model, enforces a 16,000-token per-call limit plus trusted
case call/total-token budgets, and rejects mismatched model or token requests
before invoking the provider client. The broker exposes no filesystem, command,
or general RPC surface. Requests, rejections, model calls, requested-token
budget, deadline, and cleanup are tracked in host-side case metadata.
After the Agent process stops, grading runs in a different one-shot container
with read-only mounts for `trusted_eval`, `grading_workspace`, and the
trace/final/stdout/stderr inputs.

`/runtime` is writable because the in-container Runtime records its normal run
artifacts there, so trace/timeline/metadata/final files are explicitly
Agent-authored and untrusted as process evidence. Trace claims never decide
pass/fail or prove that a test/permission event occurred. A read-only response
case may grade the submitted final answer as its outcome, but may not use it to
prove how the Agent worked. Code correctness and constraints come from the host
change manifest, the clean grading workspace, and tests executed by the
independent Grader. Host-observed Broker calls and wall-clock duration are the
trusted process metrics. The container-reported command count is retained for
diagnostics and marked untrusted.

```powershell
python evals/run_eval.py --scripted --execution docker --docker-build
python evals/run_eval.py --execution docker --case mini_auth_service_security_fix
```

The eval image is pinned to Python 3.11.9 with eval dependencies in
`evals/docker/requirements.lock`. Docker sandboxes use no network, a read-only
root filesystem, a non-root user, dropped capabilities, no-new-privileges,
bounded memory/CPU/PIDs/file descriptors, and a size-limited `/tmp`. Docker
failure is reported as `sandbox_error`; it never falls back to host command
execution. The normal interactive CLI continues to use the local executor.

On POSIX hosts the Agent/Grader numeric UID and GID match the non-root host
owner so Linux and WSL2 bind mounts remain writable/readable. Windows Docker
Desktop uses the image's fixed non-root identity. When the host itself is UID 0,
only disposable per-case copies are prepared for UID/GID 10001; symlinks are not
followed and original cases or project files are never chowned.
`--docker-timeout` is one wall-clock budget for the complete case:
workspace preparation, Agent execution, model requests, Bash, result transfer,
grading, and cleanup. Cleanup shares one absolute deadline with a bounded
three-second grace; it does not receive a fresh timeout per Docker command.

With no explicit `--case`, `--scripted` runs only cases marked
`scripted_supported: true`. Explicitly selecting an unsupported case returns a
clear command-line error instead of fabricating an eval failure.

The permission hook remains active in Docker mode. It is an application policy
layer in addition to the container boundary, not a replacement for it. Docker
Eval explicitly uses non-interactive approval mode: an approval-gated Bash or
deploy MCP call receives a structured permission denial without reading stdin;
the interactive CLI retains its `Allow? [y/N]` prompt.

When a model tries to finish while a background task is still running, the
Agent waits for completion up to the shared case deadline, injects the completed
task notification back into the loop, and only then permits a final answer.
`cleanup_grace` is reserved for stopping workers, teammates, and the scheduler
after normal execution; deadline exhaustion is a structured case failure.

For development compatibility only, local execution remains explicit:

```powershell
python evals/run_eval.py --scripted --execution local
```

Docker startup/build failure is a hard eval failure and never falls back to
local execution.
