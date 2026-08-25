# Aqours_code Evaluation Report

**Status:** v0.1 research snapshot  
**Last updated:** August 2026

## Abstract

Aqours_code is a local coding-agent harness that combines model inference, repository tools, context compaction, permission checks, structured tracing, and isolated Docker evaluation. This report documents the evaluation methodology used during its development and summarizes selected real-model experiments on long-horizon repository repair tasks.

The main result is not a claim that one model or one run defines agent quality. It is an engineering result: a clean-room evaluation loop made failures observable, separated implementation correctness from operational cost, and supported repeated changes to the harness. On the harder `stress_atomic_cache_recovery` task, early successful runs consumed between 2.23M and 2.53M provider tokens. Later successful checkpoints consumed between 0.54M and 1.04M tokens while continuing to pass the trusted grader. The latest documented run used 959,125 tokens, 33 model requests, and 47 tool calls.

Because the harness changed between many of these runs and model behavior is stochastic, the historical sequence is an observational engineering record rather than a controlled ablation study. Results from different commits are not combined into a single pass-rate estimate.

## Evaluation Questions

The evaluation work was organized around four questions:

1. **Correctness:** Can the agent produce a repository change that satisfies public behavior, hidden contracts, regression tests, and API compatibility?
2. **Isolation:** Can the agent run freely without receiving trusted grader files or exposing host credentials to the container?
3. **Observability:** When a run fails, can the failure be attributed to the model, the harness, the task, or the external provider?
4. **Efficiency:** Can context handling and tool-use policy reduce model requests, repeated reading, and token consumption without lowering correctness?

## Evaluation Architecture

The Docker path separates the agent and grader:

```text
Host
  ├─ Model broker (holds provider credentials and usage counters)
  ├─ One-shot agent container
  │    ├─ network disabled
  │    ├─ disposable copy of the task workspace
  │    └─ public task description and tests
  └─ Separate grader container
       ├─ trusted grader code and hidden tests
       ├─ protected-file and change-scope checks
       └─ correctness, quality, runtime, and token scoring
```

The agent container does not receive API credentials. Model requests are sent through the host broker, which enforces the case call limit and records provider-reported usage. The trusted grader is copied separately and is not available during the agent's implementation phase.

Every run produces machine-readable and human-readable artifacts, including:

- `trace.jsonl`: model, tool, permission, context, compact, and recovery events;
- `timeline.jsonl` and `timeline.md`: a reduced execution timeline;
- `metrics.json`: normalized correctness and operational metrics;
- `change_manifest.json`: changed paths, hashes, and scope violations;
- `grader_stdout.txt`: trusted grader output;
- `final.md`: the agent's final response; and
- a repository-level `summary.json` for the evaluation invocation.

The implementation is in [`evals/run_eval.py`](../evals/run_eval.py), [`evals/metrics.py`](../evals/metrics.py), and [`evals/scoring.py`](../evals/scoring.py).

## Cases

The repository currently contains 24 cases spanning basic tool behavior, permissions, constrained edits, multi-file repair, security fixes, context stress, recovery, scheduling, and collaboration experiments. This report focuses on two difficulty-5 cases because they generated the most useful real-model evidence.

### `stress_worker_lease_recovery`

This case tests a persistent worker queue with interacting state-machine requirements:

- request idempotency and payload conflicts;
- exact lease-token fencing and expiry;
- retry and terminal-failure idempotency;
- cancellation state transitions;
- restart recovery; and
- public API and regression compatibility.

The agent may modify only the permitted implementation files. Public tests are available in the workspace; trusted tests verify additional boundary behavior.

### `stress_atomic_cache_recovery`

This case tests a filesystem-backed artifact cache across six implementation modules. Its contract includes:

- canonical cache-key generation;
- per-key writer fencing and expiry;
- atomic publication and crash boundaries;
- artifact and manifest integrity;
- schema compatibility;
- recovery and cleanup; and
- idempotent success and failure paths.

The case protects the README, project configuration, public models, and tests. Its trusted grader evaluates seven functional outcome groups, regression behavior, public API compatibility, change scope, syntax, architecture, and unsafe test coupling.

## Scoring

Pass/fail is determined by correctness and required process checks. The continuous score then distinguishes correct but expensive runs from correct and efficient runs:

| Dimension | Weight |
| --- | ---: |
| Functional correctness | 50 |
| Code quality and constraints | 20 |
| Runtime efficiency and reliability | 15 |
| Provider token cost | 15 |

Efficiency points do not turn an incorrect result into a pass. They are gated by functional correctness, and provider usage must be present for token points to be awarded. Case-specific targets and hard limits are declared in each case's `metadata.yaml`.

## Representative Results

All results below used Docker execution with `deepseek-v4-flash`. Thinking was enabled for the lead agent. Provider-reported tokens include both input and output usage.

### Worker lease recovery

| Date | Result | Score | Runtime | Tokens | Model requests | Tool calls |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-08-04 | PASS | 97.822 | 266.2 s | 465,755 | 20 | 38 |

The trusted grader passed every functional group, the public suite, API compatibility, regression checks, and protected-path checks. The remaining 2.178 points were lost only to runtime, model-call, and token targets.

### Atomic cache: repeated pre-optimization campaign

Three successful runs from the same evaluation campaign illustrate the original cost and variance of the hard case:

| Run | Result | Provider tokens | Model requests | Tool calls |
| --- | --- | ---: | ---: | ---: |
| `20260815-140016` | PASS | 2,526,052 | 62 | 85 |
| `20260815-140024` | PASS | 2,230,856 | 60 | 76 |
| `20260815-140032` | PASS | 2,488,236 | 63 | 71 |

All three passed, but the median run consumed 2,488,236 tokens and 62 model requests. Trace analysis showed that much of the cost occurred after the public test suite had already passed, during repeated repository reads, large inline diagnostic scripts, open-ended hidden-boundary exploration, and repeated transmission of long tool-call arguments.

### Atomic cache: selected optimization checkpoints

The following table tracks later successful checkpoints. Each row reflects the harness state at that time; it should not be read as a controlled comparison in which only one variable changed.

| Run | Score | Runtime | Provider tokens | Model requests | Tool calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| `20260816-101627` | 79.735 | 1,363.5 s | 1,358,073 | 43 | 59 |
| `20260816-114308` | 80.775 | 968.6 s | 1,319,721 | 44 | 66 |
| `20260817-132243` | 83.781 | 908.9 s | 1,289,171 | 36 | 48 |
| `20260818-092250` | 97.424 | 599.8 s | 540,178 | 20 | 33 |
| `20260818-130241` | 87.274 | 861.3 s | 1,043,080 | 37 | 50 |
| `20260818-142819` | 87.556 | 1,048.6 s | 959,125 | 33 | 47 |

Compared with the pre-optimization median, the latest documented run used approximately 61% fewer tokens, 47% fewer model requests, and 38% fewer tool calls. The best run used approximately 78% fewer tokens. The difference between the 540K and 959K successful runs also shows that route selection by the model remains a major source of variance; the harness reduced waste but did not make the model deterministic.

## What Changed

Trace review led to the following harness changes.

### 1. Preserve new tool results before compaction

The original context path masked every tool result above an estimated 8K tokens before the model could consume it. A large README could therefore be read successfully by the tool and replaced with an omission marker before the next model request, causing repeated reads and lost evidence.

The current path instead:

- delivers normal results inline up to an estimated 24K tokens;
- compacts older history first when a new inline result would cross the trigger;
- protects the newest unconsumed `tool_use`/`tool_result` batch;
- externalizes only an individually oversized result; and
- keeps a bounded head-and-tail preview plus a recoverable file path.

This separates the complete trace from the context view sent to the model without destroying the newest observation.

### 2. Make compaction cheaper and earlier

The active agent context now has a 200K-token hard limit and starts automatic compaction at an estimated 80K tokens. Compact-summary requests may read up to 256K tokens but produce at most 6K tokens. DeepSeek Thinking is disabled only for summary generation because compaction is an information-preservation task rather than an agent-planning task.

Compaction preserves the root user task, atomic tool-call/result pairing, the newest unconsumed tool exchange, and recent exchanges when budget permits. If proactive compaction fails, the runtime may continue below the hard limit, but hard-limit recovery is allowed to retry rather than being blocked by a normal cooldown.

The 200K hard limit is primarily a reliability margin. It should not be interpreted as a direct token-saving mechanism.

### 3. Remove automatic backgrounding of tests

Earlier versions classified commands such as `pytest` as slow and automatically started them in the background. Sub-second test suites then required an additional model turn before their result became visible. Bash now runs in the foreground unless the model explicitly requests `run_in_background=true`.

### 4. Avoid repeated post-test exploration

The lead prompt now favors batched edits and focused verification. After the public suite passes, the agent should test one concrete uncovered risk at a time and stop when no specific risk remains. This replaced case-shaped advice and open-ended instructions that encouraged broad, expensive auditing.

### 5. Avoid an 8K probe for DeepSeek Thinking

Early traces showed repeated `stop_reason=max_tokens` responses with empty visible content: the model spent the entire 8K, 16K, or 32K response allowance in reasoning without producing a tool call. Replaying the same request at a larger limit duplicated cost. DeepSeek Thinking calls now begin with the configured high allowance. This is a ceiling, not a reservation; normal tool-call responses can still stop after a small output.

## Negative Experiment: Automatic Verifier

An automatic verifier subagent was tested before the leader's final response. The intended flow was simple: inspect the leader's changes and tests, identify missed contracts, and return actionable findings. In practice, successive implementations accumulated separate call limits, output limits, JSON synthesis, finding-state transitions, and recovery behavior.

Observed failures included:

- the verifier repeating repository exploration instead of performing a focused review;
- result generation consuming its output allowance in reasoning and returning no parseable JSON;
- tool or global token budgets ending verification before a finding was delivered;
- findings failing to produce a reliable leader-fix-verification loop; and
- substantial extra runtime and token cost without a stable correctness gain.

The automatic verifier was removed from the default path. This experiment changed the project direction: explicit task contracts, trusted external grading, and inspectable traces were more reliable than adding another model-controlled protocol layer. Subagent primitives remain experimental, but the stable single-agent path does not require a verifier.

## Failure Taxonomy

Failures are classified before interpreting pass rate or optimization results:

| Class | Examples | Treatment |
| --- | --- | --- |
| Agent/model | Missed failure-path idempotency, public API regression, incomplete implementation | Counts as a task failure |
| Harness | Unconsumed tool result masked, compaction hard-limit dead end, verifier protocol failure | Fix the harness and rerun; do not attribute to task capability alone |
| Provider/infrastructure | TLS EOF, HTTP 402 insufficient balance, Docker startup failure | Report separately; do not treat as evidence of coding correctness |
| Case/specification | A required hidden behavior not stated by the public contract | Clarify the contract before using the case to compare agents |

This distinction was important during development. For example, a transient TLS failure produced no edits or test runs and was followed by a clean successful rerun. An HTTP 402 failure similarly ended an otherwise valid experiment because the provider account lacked balance.

## Interpretation

The experiments support four conclusions:

1. **Correct task contracts matter more than elaborate reviewer orchestration.** Deep failure semantics should be explicit enough to derive from the public specification while still requiring implementation work.
2. **Input-token repetition dominates long coding runs.** Large histories, rereads, diagnostic scripts, and repeated test interactions were more important than final-answer length.
3. **Harness changes can remove systematic waste, but model variance remains.** A correct run ranged from 540K to more than 2M tokens depending on the harness revision and the model's chosen path.
4. **Traceable negative results are useful.** Reasoning-only truncation, compaction failures, and the unsuccessful verifier experiment directly informed simpler designs.

## Limitations

- Most historical rows use different harness revisions. They establish an optimization trajectory, not the isolated causal effect of each change.
- Temperature, provider implementation details, caching, and model updates may affect reproducibility.
- The report focuses on two custom stress cases and does not claim comparability with SWE-bench Verified, Terminal-Bench, or production coding-agent workloads.
- One best run should not be treated as expected performance. Multiple independent trials are required for a stable estimate.
- The case score targets are engineering thresholds and remain provisional until more fixed-revision trials are collected.
- Raw traces are intentionally not committed wholesale because they are large and may contain complete task workspaces or model-generated code. Sanitized metrics and timelines are safer publication artifacts.

## Reproduction

Install the project with Python 3.11 and configure a provider in `.env`. A deterministic Docker smoke test is:

```bash
python evals/run_eval.py --scripted --execution docker --case read_file_basic
```

The two real-model stress commands used during development were equivalent to:

```bash
python evals/run_eval.py \
  --case stress_worker_lease_recovery \
  --execution docker \
  --request-timeout 180 \
  --docker-timeout 900

python evals/run_eval.py \
  --case stress_atomic_cache_recovery \
  --execution docker \
  --docker-build \
  --request-timeout 720 \
  --docker-timeout 1500
```

For a publishable comparison, pin one Git commit, provider model version, case revision, Docker image, and configuration. Run at least five independent trials and report all of the following:

- pass count and pass@k;
- median and range for tokens, runtime, model requests, and tool calls;
- input and output tokens by request purpose;
- provider and infrastructure incidents; and
- links to sanitized `metrics.json`, `timeline.md`, and grader output for every trial.

This protocol is the next step from the historical engineering record toward a controlled agent-systems experiment.
