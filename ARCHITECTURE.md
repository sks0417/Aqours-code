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
2. A state artifact comes from `runtime.paths.state_root`, never from a host
   workspace inferred later.
3. Todo, changed-file, and Lead read state belongs to `runtime.state`.
4. Main and child roles share bounded services but receive separate mutable
   `RunState` objects.
5. Runtime-aware functions retain their old no-runtime entry point only for
   compatibility and tests.
6. A migration step must preserve the existing full test suite and Docker Eval
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
- The Lead projection has 31 built-in tools, including the narrow
  `read_archived_tool_result` recovery surface.
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

The fixed empty-context system prompt plus 31-tool JSON payload is guarded
below 12,000 characters. Capability-group lazy exposure is deferred; this phase
does not remove any Lead tool.

## Context lifecycle and compaction

Every model turn is budgeted as one assembled request: rebuilt system prompt,
API tool schemas, dynamic runtime state, and messages. Ordinary turns leave
history untouched. At 85% of the configured context budget, the lifecycle is:

```text
assemble the complete current request
-> choose prefix + recent raw suffix from the untouched history
-> archive every Tool Result in the outgoing prefix into the current Trace run
-> fit one summary request; mask only archived copies when strictly necessary
-> summarize the older prefix into one Markdown continuation checkpoint
-> append a programmatic archive locator
-> assemble checkpoint + unchanged complete recent tail
-> verify the complete next request fits the target
```

The checkpoint is an ordinary internal user message marked
`[Context checkpoint]`. A later compact includes the old checkpoint in the
older prefix and replaces it with one new cumulative checkpoint; summaries do
not stack. The summary is free-form Markdown. There is no JSON schema, semantic
merge, file-card state, Tool-result acknowledgement protocol, or deterministic
semantic fallback.

The recent tail is selected by an estimated token budget, not by a fixed number
of results. Assistant Tool-use and matching user Tool-result messages are one
atomic history unit and cannot be cut apart. The latest human instruction is
retained verbatim even when a long tool run pushes it outside the suffix.
Checkpoint-boundary user messages are safely merged so provider role ordering
remains valid.

Checkpoint and real user instructions are pinned. Compact never drops old
units to recover from a summary overflow. Before the model call, the complete
summary prompt is measured against a conservative input budget. Tool Results
are normally shown to the summarizer in full. Only if the prompt does not fit
are the largest Tool Results replaced in the summary-request copy by bounded
descriptors containing Tool ID/name/input, archive ID/path, size, digest, and
head/tail preview. The original messages and recent tail are never modified.
If even that request cannot fit, the original history remains unchanged.

One compact attempt reserves and issues at most one summary model call. Model
failure, empty output, or an over-budget assembled candidate retains the
original history; there is no retry or second summary call.

Every Tool Result entering the outgoing prefix is archived exactly once under
the current Trace run's `artifacts/compacted-tool-results` directory, regardless
of size. `manifest.jsonl` maps Tool-use ID/name/input to the exact UTF-8 file,
character count, and SHA-256 digest. Checkpoints mechanically include an
`archive://<run-id>/manifest.jsonl` locator. The Lead-only
`read_archived_tool_result` tool resolves a Tool-use ID inside that directory
and cannot accept arbitrary paths. Recent-tail results are archived only when
a later Compact moves them into the prefix.

Archives inherit Trace retention as one run artifact: the active run and
pinned runs are protected, while completed unpinned runs are subject to the
existing age/count/disk cleanup. Cleanup failure remains non-fatal.

Compact trace records reason, before/after messages and token estimates,
summarized prefix size, raw-tail size, summary length, outcome, and oversized
archive/masking counts and the exact summary-call count. Successful compaction increments a small runtime generation
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
