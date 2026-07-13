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

By default evals remain local and backward compatible:

```powershell
python evals/run_eval.py --scripted --execution local
```

Docker mode runs the Agent Loop and model API client in a dedicated, killable
host process and routes every `bash` call through `docker exec` into one
per-case container. The model is offered only workspace-safe file tools,
containerized `bash`, todo/compact, and the synchronous subagent. Host worktree,
persistent task, teammate, cron, skill, and MCP tools are not exposed. Disabled
tools and the active policy are recorded in the trace.

The Agent container sees only the writable `agent_workspace` at `/workspace`.
Run traces and their index are written under a sibling trusted `agent_runtime`
directory, then exported for grading, so the Agent cannot rewrite its own audit
trail through Bash or file tools.
After the Agent process stops, grading runs in a different one-shot container
with read-only mounts for `trusted_eval`, `grading_workspace`, and the
trace/final/stdout/stderr inputs.

```powershell
python evals/run_eval.py --scripted --execution docker --docker-build
python evals/run_eval.py --execution docker --case mini_auth_service_security_fix
```

The eval image is pinned to Python 3.11.9 with locked Python dependencies in
`evals/docker/requirements.lock`. Docker sandboxes use no network, a read-only
root filesystem, a non-root user, dropped capabilities, no-new-privileges,
bounded memory/CPU/PIDs/file descriptors, and a size-limited `/tmp`. Docker
failure is reported as `sandbox_error`; it never falls back to host command
execution. The normal interactive CLI continues to use the local executor.

On POSIX hosts the Agent/Grader numeric UID and GID match the non-root host
owner so Linux and WSL2 bind mounts remain writable/readable. Windows Docker
Desktop uses the image's fixed non-root identity. Agent startup performs a real
write/delete probe inside `/workspace` and fails closed if the mount is not
writable. `--docker-timeout` is enforced by the parent process: it terminates
the complete Agent process, performs bounded cleanup by container name, records
`tool_loop`, and continues with later cases.

The permission hook remains active in Docker mode. It is an application policy
layer in addition to the container boundary, not a replacement for it.
