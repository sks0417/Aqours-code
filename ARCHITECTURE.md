# Codepilot S20 Runtime Architecture

## Direction

The runtime is migrating from module-wide mutable globals to one explicit
`AgentRuntime` per Agent execution. The migration is intentionally incremental:
existing CLI, Eval, tools, and role behavior remain available while core code
accepts the runtime explicitly.

The ownership rule is:

```text
AgentRuntime
  RuntimeConfig    immutable choices for one execution
  RuntimePaths     workspace and state-owned filesystem paths
  RunState         mutable task state
  RuntimeServices  model, command, and Trace services
```

## Current explicit boundary

The following paths now prefer an explicit runtime:

- interactive CLI and non-interactive `run_agent_task()`;
- main Agent Loop and model calls;
- Context and Prompt assembly;
- Memory, Skill, transcript, and persisted tool-result paths;
- file, Bash, Todo, and compact tools;
- dynamic tool handler binding;
- synchronous General, Explorer, Reviewer, and Worker roles.

`runtime_state.py` remains a compatibility adapter for modules that have not
yet migrated and for callers using the old function signatures. New code should
not add another module-global per-run value.

## Runtime invariants

1. A workspace path comes from `runtime.paths.workdir` when a runtime exists.
2. General Agent state comes from `runtime.paths.state_root`.
   `trace_storage_root` only selects trusted Trace/Eval output and never
   redirects Skills, Memory, Tasks, Worktrees, mailboxes, transcripts,
   scheduled tasks, or persisted tool results.
3. Context compaction does not own a filesystem root or persist Tool Results.
4. Todo, changed-file, and Lead read state belongs to `runtime.state`.
5. Main and child roles share bounded services but receive separate mutable
   `RunState` objects.
6. Runtime-aware functions retain their old no-runtime entry point only for
   compatibility and tests.
7. A migration step must preserve the existing full test suite and Docker Eval
   smoke before another subsystem is moved.

## Tool registry

`tool_defs.py` builds one canonical `TOOL_REGISTRY` of `ToolSpec` records.
Each record owns its API description/schema, handler, safety policy, background
policy, role access, and runtime-binding metadata.

- A role surface is computed as:
  `Registry.allowed_roles ∩ AgentProfile.tool_names ∩ parent Runtime policy
  ∩ environment policy`.
- Delegation cannot enlarge the parent Runtime's `allowed_tools` by default.
  The only supported exception is an explicit per-role
  `delegated_tool_policy` with both `allowed_tools` and
  `allow_parent_permission_expansion=true`; the environment policy remains an
  absolute upper bound.
- General, Explorer, Reviewer, Worker, and Teammate projections use the same
  effective-permission function. Synchronous roles and asynchronous Teammates
  therefore cannot obtain Bash or write tools merely because their profile
  lists them.
- `BUILTIN_TOOLS` and `BUILTIN_HANDLERS` are derived compatibility views.
- Dynamic MCP schemas are still appended by `assemble_tool_pool()`.
- API schemas are authoritative, so the system prompt no longer repeats all
  tool descriptions.

Canonical schemas are recursively frozen when `ToolSpec` is created.
`api_schema()` recursively copies them back into provider-facing dictionaries,
so mutations to a Lead, role, or Teammate projection cannot affect another
projection or the Registry.

Every declared policy name has an executable dispatcher. Safety policies are
handled by `SAFETY_POLICY_VALIDATORS` in the pre-tool Hook, including
`destructive_confirmation` and `workspace_integration`. Background policies
are handled by `BACKGROUND_POLICY_ROUTERS`; an unknown policy cannot be
registered as if it were active.

The fixed empty-context system prompt plus complete Lead-tool JSON payload is guarded
below 12,000 characters. Capability-group lazy exposure is deferred; this phase
does not remove any Lead tool.

## Context lifecycle and compaction

Every model turn is budgeted as one assembled request: rebuilt system prompt,
API tool schemas, dynamic runtime state, and messages. Ordinary turns leave
history untouched. At 85% of the configured context budget, the lifecycle is:

```text
assemble the complete current request
-> choose prefix + recent raw suffix from the untouched history
-> keep the latest user instruction and four latest Tool exchanges raw
-> replace copied Tool Results above 6,000 estimated tokens with placeholders
-> summarize the older prefix into one Markdown continuation checkpoint
-> assemble checkpoint + recent complete tail
-> verify the complete next request fits the target
```

The checkpoint is an ordinary internal user message marked
`[Context checkpoint]`. A later compact includes the old checkpoint in the
older prefix and replaces it with one new cumulative checkpoint; summaries do
not stack. The summary is free-form Markdown. There is no JSON schema, semantic
merge, file-card state, Tool-result acknowledgement protocol, or deterministic
semantic fallback.

The old prefix is selected near `COMPACT_CHUNK_TOKENS` using the nearest safe
history-unit boundary. Assistant Tool-use and matching user Tool-result
messages are one atomic unit and cannot be cut apart; a parallel Tool batch is
one unit as well. The latest human instruction is retained verbatim even if it
also appears in the summarized prefix. The most recent
`RECENT_TOOL_RESULT_COUNT` (currently four) complete exchanges remain in the
raw tail. Checkpoint-boundary user messages are safely merged so provider role
ordering remains valid.

Before either copied context is used, a Tool Result above
`MAX_TOOL_RESULT_TOKENS` (currently 6,000 estimated tokens) has only its content
replaced by a short size/reason placeholder. The Tool-result message and
`tool_use_id` remain intact. Normal results are unchanged, the caller's
original history is never mutated, and no result is written to disk or made
recoverable later.

One compact attempt issues at most one summary model call. The complete summary
prompt is measured before that call. Model failure, empty output, an unsafe
boundary, or an over-budget assembled candidate returns the original history;
there is no recursive summary, retry, disk fallback, or second call.

Compact trace records reason, before/after messages and token estimates,
summarized prefix size, raw-tail size, summary length, outcome,
oversized-result placeholder count, and the exact summary-call count.
Successful compaction increments a small runtime generation
counter used only by the post-compact redundant-read metric.

**Status: Implemented, not Validated.** Local tests cover the lightweight
checkpoint/tail behavior. No paid real-model stress or paired Eval was run for
this change.

## RunKnowledge verification state

`RunState.knowledge` is deterministic working memory for one Agent run. It is
not long-term Memory and is never retrieved by embeddings. It retains:

- read paths with SHA-256 digest, monotonic file version, read count, and
  current/stale evidence state;
- Python symbols confirmed by parsing an observed file version;
- contracts derived from Acceptance and bounded Explorer evidence;
- modified paths and recent test commands/results;
- Acceptance status/evidence and structured Reviewer findings.

Evidence state is explicit:

- `verified`: at least one explicit provenance source exists and every bound
  file version, TestKnowledge record, or Reviewer finding is current;
- `stale`: a previously bound source no longer matches current state;
- `unbound`: no verifiable provenance exists. Arbitrary evidence text never
  makes a completed Acceptance item verified.

Workspace mutation tracking uses one snapshot/execute/reconcile boundary.
Content fingerprints are compared before and after `write_file`, `edit_file`,
foreground Bash, background Bash, and successful Worktree integration. Every
actual added, changed, or deleted path is sent through the same versioned
invalidation method. Mutation windows and RunKnowledge updates are locked so
background workers cannot race evidence versions. This does not parse shell
commands or infer writes from command text.

`TestKnowledge.workspace_versions_at_run` and
`workspace_fingerprints_at_run` describe Workspace state when a test ran.
They are not coverage claims. `covered_source_versions` remains empty unless a
caller supplies an explicit coverage/dependency mapping; a targeted pytest
command never implicitly validates every modified source file.

A later read confirms the new file version but does not silently revive
Contract, Acceptance, or Reviewer evidence from an older version.

`RunKnowledge` remains internal to `AgentRuntime` for Acceptance, tests, and
Reviewer evidence. It is not injected wholesale into model Context and
compaction neither reads nor merges it. Current Acceptance todos are still
rebuilt dynamically in the system prompt.

**Status: Implemented.** The deterministic proof behavior and regression suite
are passing locally. It is not `Validated`: Working Memory must retain that
status until the paired ledger pass-rate/read/token exit criteria are all met.

## Remaining migration order

1. Move Trace ownership from `CURRENT_TRACE` to `RuntimeServices`.
2. Move Background and Cron collections into `RunState` and pass the runtime
   into their worker threads.
3. Move MCP and asynchronous Teammate state into explicit runtime-owned
   collections.
4. Replace wildcard `runtime_state` imports with narrow imports.
5. Delete compatibility mirroring only after no execution path needs it.

This order keeps cleanup and thread-lifecycle behavior stable while removing
the most dangerous hidden state first.
