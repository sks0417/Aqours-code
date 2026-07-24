# codepilot_s20

Function-parity split of learn-claude-code s20 with multi-provider model adapter.

## Requirements

- Python 3.10 or newer

## Runtime ownership

Each CLI or non-interactive execution now creates an explicit `AgentRuntime`
containing immutable configuration, runtime-owned paths, mutable task state,
and external services. Prompt/Context assembly, core tools, compaction, and the
bounded role runtime receive this object directly. `runtime_state.py` remains
only as an incremental compatibility adapter for Background/Cron/Teammate and
older call sites. The ownership rules and remaining migration order are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

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

压力 Eval 暴露的 Harness 问题、根因、方案与验证状态统一维护在
[`HARNESS_ISSUES.md`](HARNESS_ISSUES.md)。修复只有通过原始复现 case 后才标记为
`Validated`。

`stress_inventory_reservation_consistency` 是中型跨文件回归；
`stress_distributed_ledger_recovery` 是 difficulty 6 综合能力 case，覆盖跨 partition 原子写入、
exactly-once、分区序列和 checkpoint+tail recovery。后者按独立 outcome group 给部分分，不要求调用
subagent/multiagent；角色是否有价值必须从 Lead calls/reads、通过率、耗时和 actual tokens 的对照证明。

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

The Agent container runs the normal `run_agent_task()` with all 30 built-in
tools, dynamic MCP tools, Skill, Memory, Task, Cron, Background, Subagent,
Teammate, Protocol, and Worktree support. It receives only `/workspace`,
per-case `/state`, `/runtime`, and the model forwarding directories. The host
project, API keys, Docker socket, trusted grader, and grading workspace are not
mounted. The container is network-disabled, non-root, and resource limited.

The 30 built-ins and every role-specific projection come from one `ToolSpec`
registry. API schemas are the authoritative tool descriptions, so the system
prompt no longer duplicates them. A regression budget keeps the fixed prompt
plus 30-tool schema below 12,000 characters without hiding any Lead tool.

Each run also owns deterministic `RunKnowledge`: file digests/versions,
confirmed Python symbols and contracts, modified paths, recent test results,
Acceptance state, and Reviewer findings. Context compaction preserves this
state independently of raw messages. Workspace writes from file tools,
foreground/background Bash, and Worktree integration are detected from
before/after fingerprints and invalidate only evidence bound to changed paths.
Evidence is `verified`, `stale`, or `unbound`; text without explicit file,
test, or Reviewer provenance is never verified. Test workspace snapshots are
not presented as source coverage.

For complex code changes, `todo_write` distinguishes execution steps
(`kind=plan`) from external requirements (`kind=acceptance`). Completed
acceptance items require concise evidence. The compact live prompt preserves
only acceptance state across Context compression, and the Agent performs one
bounded review before finalizing. Unverified requirements are surfaced in the
final result instead of silently being treated as completed.
Acceptance items have stable IDs. Reviewer findings use revision-scoped IDs
such as `review:r3:f1`, so the Lead can update status and evidence without
copying generated wording or consuming another checklist slot.

Complex tasks also receive role-based orchestration capabilities. Static
complexity adds only an advisory; it does not force an Explorer, and observed
read pressure is telemetry rather than an automatic trigger. An `explorer` can use a
fresh, read-only context to map contracts and code paths. On the first final
attempt for a changed complex revision, the Harness runs one bounded `reviewer`
automatically when the shared model budget permits and attaches its result to
the same pre-final contract audit, avoiding a second Lead round just to request
review. Reviewer output is capped to five short structured findings. Its final
synthesis uses a fresh, tool-free evidence packet so an unfinished tool intent
cannot leak into the JSON result. Invalid or truncated JSON retains explicit
actionable concerns, while a purely malformed result stays blocked without
creating a fake acceptance finding. Real findings become pending acceptance work
that must be resolved or rejected with code evidence. An optional `worker`
implements one bounded slice in an automatically managed Git worktree.
Worker changes are committed by the Harness but remain outside the main
workspace until the Lead calls `integrate_worktree`; overlapping Lead and Worker
file changes are rejected without discarding the worktree. The Lead remains
responsible for decomposition, integration, tests, and the final answer.

The legacy `task` tool is now only a compatibility entry into the same bounded
role runtime. Delegated inspection is routed to `explorer`, implementation to a
`worker` worktree, final correctness audit to `reviewer`, and a small unmatched
question to a read-only `general` helper. Routing uses generic action intent, not
case names or repository paths. Every route shares model-call accounting,
deadlines, Trace events, finalization reserve, structured results, and per-role
unique read-path limits; exact repeated reads inside one delegation are reused.
Failures and budget skips return a structured blocked result so the Lead can
continue directly instead of treating role success as a gate.
When the tail reserve cannot afford an automatic Reviewer, Trace records
`reviewer_status=skipped_budget`; no blocked envelope is presented as an
attached independent review.

The host-side Model Broker exists only so API keys stay on the host. It accepts
schema-checked `messages.create` calls for the selected model and applies one
case-wide call budget and requested-token budget. These budgets cover the main
Agent, nested roles, retries, and model-generated compact summaries together.
The container reads the Broker's live non-secret counters through a read-only
Docker stats mount and reserves 20% of
the call budget, bounded to 4-8 calls, for final fixes, targeted verification,
and the final response. A new role is not started unless its bounded worst-case
rounds leave that reserve intact. In the reserve, automatic compaction uses a
deterministic checkpoint built from the root task, live acceptance state, and
recent messages instead of another model call. Compact-summary requests now
appear in Trace with `purpose=compact_summary`. When exactly one call remains,
tools are disabled and that call is forced to produce the final response; a
request beyond the Broker limit is never issued. Main-Agent LLM rounds and tool
calls are not execution budgets; Trace records them as `llm_requests` and
`tool_calls` for reporting and continuous efficiency grading. Successful
Provider responses also contribute actual input, output, cache-creation, and
cache-read token counters; requested `max_tokens` remains a budget guard and is
never presented as actual consumption.

After the Agent exits, the runner verifies the trusted case did not change,
creates `grading_workspace` from the original workspace, and applies only
`allowed_changes`. The independent Grader receives the clean case and
trace/final/stdout/stderr inputs read-only. This prevents modified public tests
or grader files from becoming the source of truth.

Eval reports `passed` independently from its continuous 100-point score. The
trusted case Grader owns functional correctness (50 points) and deterministic
code quality (20); the host Harness adds runtime efficiency (15) and actual
token cost (15), because only the host can observe wall time and Provider usage.
Lower-is-better dimensions use explicit per-case target and hard-limit values
from `metadata.yaml`. A passing solution can therefore score below 100, while a
fast empty failure cannot earn operational credit because those points are
gated by its functional-correctness ratio. Raw metrics and ungated operational
points remain in `metrics.scoring` for audit and later threshold calibration.

`--docker-timeout` is the wall-clock deadline for preparation, Agent execution,
grading, and cleanup. Background work may run inside the Agent container; a
one-shot Eval waits for it only until that deadline and injects completed output
as `task_notification`. Interactive CLI sessions keep true background behavior.
Lifecycle reporting separates `all_started_containers_cleanup_succeeded` from
`lifecycle_complete`, so a grader that never started is not misreported as a
container leak. Command metadata similarly exposes whether a zero execution
count is known or came from an older/missing result schema.

`--request-timeout` is the per-attempt Host Provider timeout. Docker Eval keeps
API keys and one transient-error retry in the Host Model Broker. The container's
IPC wait covers both Provider attempts, the 1-second retry delay, and a 5-second
delivery grace (for example, a 60-second Provider timeout yields a 126-second
IPC window). Every attempt consumes the case call and requested-token budgets,
and all waits are still capped by `--docker-timeout`.

Docker is the default. `--scripted` changes only the host-side model response;
the complete Agent and Grader containers still run:

```powershell
python evals/run_eval.py --scripted --execution docker --docker-build
python evals/run_eval.py --execution docker --case mini_auth_service_security_fix
python evals/run_eval.py --execution docker --docker-build --request-timeout 60 --case stress_distributed_ledger_recovery --docker-timeout 900
```

Phase 3 paired summaries can be checked without hand-calculating medians:

```powershell
python evals/compare_phase3.py `
  --baseline path/to/baseline-1/summary.json `
  --baseline path/to/baseline-2/summary.json `
  --candidate path/to/candidate-1/summary.json `
  --candidate path/to/candidate-2/summary.json
```

Each argument may also point directly to a persisted
`evals/results/runs/<run-id>` directory, so historical runs remain comparable
after the top-level `summary.json` is replaced by a newer Eval.

The comparison passes only when ledger median `read_file` calls are below 45,
pass rate does not fall, and metered median tokens fall by at least 15%.

Current Trace/timeline diagnostics share the runtime schema and tolerate
malformed JSONL while reporting its line number:

```powershell
python analyze_trace.py evals/results/runs/<run-id>/<case>
python analyze_timeline.py evals/results/runs/<run-id>/<case>
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
