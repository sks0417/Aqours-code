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
