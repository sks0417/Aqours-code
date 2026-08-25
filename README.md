# Aqours_code

`Aqours_code` is a local coding-agent harness for working with software repositories. It connects model inference, tool execution, context compaction, permission enforcement, structured tracing, and isolated Docker evaluation into one inspectable workflow. The project is intended for studying and evaluating coding-agent engineering decisions rather than providing a production-hosted service.

## Core Capabilities

- Runs an interactive agent loop in the current repository so a model can inspect, modify, and test code.
- Provides Bash, file read/write/edit, Glob, Todo, and Context Compact tools.
- Applies permission checks to dangerous operations and bounded recovery to transient model failures.
- Stores redacted traces, timelines, final responses, and run metadata for every execution.
- Evaluates agents inside Docker and scores a clean-room workspace with a separate trusted grader.

## Feature Status (v0.1)

**Stable** means the default single-agent path is integrated and supported by code and automated tests. **Experimental** means the implementation remains available for research and iteration but is not part of the v0.1 stability guarantee.

| Status | Feature | Current Scope |
| --- | --- | --- |
| Stable | Agent Loop | Interactive and non-interactive single-agent execution |
| Stable | Bash | Foreground commands, timeouts, failure results, and permission boundaries |
| Stable | Read / Write / Edit | Workspace-scoped file reading, writing, and exact replacement |
| Stable | Glob / Search | Native Glob; text search through `rg` or platform search commands via Bash |
| Stable | Todo | Optional lightweight checklist with stable IDs, content, and status |
| Stable | Context Compact | Preserves atomic tool exchanges, the root task, and recent context; retains safe history on failure |
| Stable | Permission Hooks | Blocks dangerous operations and denies unapproved actions in non-interactive runs |
| Stable | Retry / Recovery | Bounded retries; v0.1 never switches silently to another model |
| Stable | Trace | Redacted events, timelines, run state, indexes, and minimal runtime metadata |
| Stable | Docker Eval | One-shot agent container, clean-room workspace, separate trusted grader, and trusted scoring |
| Experimental | Subagent / Plan Review | Bounded temporary roles and reviewer flow; not required by the default single-agent path |
| Experimental | Persistent Task / Teammate / Message Bus / Multi-Agent | Shared-task and collaboration prototypes without a complete lifecycle guarantee |
| Experimental | Worktree | Prototype for isolated worker changes and explicit integration |
| Experimental | Skills / MCP | Dynamic extension points that depend on the runtime environment and configuration |
| Experimental | Background / Cron | Background execution and scheduling prototypes outside the default v0.1 path |

## Architecture

```text
Aqours_code CLI
  -> AgentRuntime + Agent Loop
  -> Model adapter
  -> authoritative ToolRegistry
  -> local workspace
  -> .aqours_code/runs/<run_id> trace artifacts

Docker Eval
  -> host Model Broker (credentials remain on the host)
  -> network-disabled one-shot Agent container
  -> disposable workspace + collected artifacts
  -> separate trusted Grader container
  -> pass/fail + continuous score
```

The default entry point starts only the single-agent coding workflow. Experimental modules are not forced into the main execution path merely to make the harness appear more feature-complete.

## Quick Start

The supported environment for v0.1 is **Python 3.11**.

```bash
git clone https://github.com/sks0417/Aqours-code.git
cd Aqours-code

python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate

python -m pip install -e .
```

Create a local configuration file from the provided example:

```bash
# Windows PowerShell
Copy-Item .env.example .env

# macOS / Linux
cp .env.example .env
```

Fill in the required model settings, then run the CLI from the repository you want the agent to modify:

```bash
Aqours_code
```

The startup banner shows the project name, active workspace, provider, and model without displaying the API key. The workspace defaults to the directory from which the command is launched.

## Model Configuration

`.env.example` and the runtime use the same authoritative fields:

```dotenv
AQOURS_CODE_PROVIDER=
AQOURS_CODE_API_KEY=
AQOURS_CODE_BASE_URL=
AQOURS_CODE_MODEL=

# AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS=200000
# AQOURS_CODE_COMPACT_TRIGGER_TOKENS=80000
# AQOURS_CODE_SUMMARY_INPUT_LIMIT_TOKENS=256000
# AQOURS_CODE_SUMMARY_MAX_TOKENS=6000
# AQOURS_CODE_CONTEXT_LIMIT_TOKENS=200000  # legacy alias
```

`AQOURS_CODE_PROVIDER` accepts `openai_compatible`, `openai`, `deepseek`, or `anthropic`. The first three use an OpenAI-compatible messages API; `anthropic` uses the Anthropic SDK. The runtime has no default model and does not silently fall back to provider-specific credentials or model names. Missing required fields are reported before the interactive loop starts.

`.env` is ignored by Git. API keys and authentication headers are removed from traces, and authentication-related user information or query parameters are removed from recorded base URLs.

All public Context and Compact settings use estimated tokens. The active agent context has a default hard limit of `200000` tokens and starts automatic compaction at `80000`. A summary request may read up to `256000` input tokens and produce up to `6000` output tokens. Internally, each budget is converted independently using a conservative estimate of three characters per token. `AQOURS_CODE_CONTEXT_LIMIT_TOKENS` remains a legacy alias for `AQOURS_CODE_AGENT_CONTEXT_LIMIT_TOKENS`.

`pyproject.toml` is the authoritative dependency declaration. `evals/docker/requirements.lock` only pins the evaluation image and is not a second application dependency list.

## Running a Coding Task

Launch `Aqours_code` from the target repository root and provide a concrete task, for example:

```text
Aqours_code >> Fix the order-total rounding bug, run the relevant pytest suite, and preserve the public API.
```

The agent inspects the workspace, tracks optional Todo items, applies code changes, and runs tests. Each execution is written to `.aqours_code/runs/<run_id>/`. Permission failures, test failures, and grader failures are never converted into successful outcomes.

## Tests and Docker Evaluation

Install development dependencies and run the project checks:

```bash
python -m pip install -e ".[dev]"
python -m compileall aqours_code
python -m pytest -q
Aqours_code --help
```

The minimal Docker smoke test uses a deterministic scripted model while still running a real agent container and a separate grader container:

```bash
python evals/run_eval.py --scripted --execution docker --case read_file_basic
```

The evaluation runner builds the Docker image when it is missing. Use `--docker-build` to force a rebuild.

For a real-model evaluation, omit `--scripted` and use the same provider settings from `.env`. Docker startup or build failures are treated as hard failures and never fall back silently to local execution. Use `--execution local` only for explicit development debugging.

## Trace Artifacts

Every run produces at least `metadata.json`, `trace.jsonl`, `timeline.jsonl`, `timeline.md`, and `final.md`:

```json
{
  "run_id": "20260803-120000-a1b2c3d4",
  "started_at": "2026-08-03T04:00:00+00:00",
  "project_version": "0.1.0",
  "git_commit": "<commit-or-null>",
  "git_dirty": true,
  "model_provider": "openai_compatible",
  "model": "<configured-model>",
  "base_url": "https://gateway.example/v1",
  "python_version": "3.11.9",
  "platform": "<runtime-platform>",
  "workspace": "<absolute-workspace>"
}
```

Git fields may be empty when Git is unavailable or the workspace is not a repository. Metadata collection failures are recorded as degraded metadata and do not terminate the agent.

## Known Limitations

- v0.1 supports Python 3.11; other Python versions are outside the tested compatibility range.
- The stable entry point is a local interactive single-agent workflow, not a web interface, remote service, or GitHub pull-request automation system.
- Docker Eval requires a running Docker daemon and remains subject to local image-build and resource constraints.
- Shell behavior depends on the host platform; task prompts must account for platform-specific commands and paths.
- Test coverage for an Experimental module does not imply production-ready integration or make that module a dependency of the default coding workflow.
- The project does not provide long-term memory, production credential management, service-level guarantees, or multi-tenant isolation.

Remaining v0.1 release checks are tracked in [`V0_1_TODO.md`](V0_1_TODO.md).
