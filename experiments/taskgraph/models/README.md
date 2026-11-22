# Models: switching and smoke test

Two scripts to prepare multi-model experiments: one switches the model that
Aqours uses, the other runs one small request through the normal task graph
flow to see whether a model works with the harness at all. Everything here is
data and scripts; neither Aqours nor `aqours_code.taskgraph` imports it.

```text
README.md            this file
use_model.ps1        switch the model: copy .env.<name> to .env
dotenv.ps1           reads single settings of a .env (used by both scripts)
smoke/
  smoke.ps1          the smoke test (steps 1 to 4 below)
  smoke.py           its repository preparation and result check
  request.md         the request: add mul(a, b)
  template/          the repository: calc/ops.py (add) and tests/test_ops.py
```

## Model files: `.env.<name>`

Each model has its own file in the repository root, next to `.env`:
`.env.deepseek`, `.env.qwen`, `.env.sonnet`, ... A name has letters, digits,
`-` and `_`.

A model file is a complete copy of `.env` that differs from the others **only
in these four lines**:

```text
AQOURS_CODE_PROVIDER=
AQOURS_CODE_BASE_URL=
AQOURS_CODE_MODEL=
AQOURS_CODE_API_KEY=
```

Every other setting (context limits, compaction, timeouts, ...) is the same
in every file, so that switching files changes the model and nothing else. To
add a model, copy an existing model file and change the four lines. The one
exception is the output limit, below: non-DeepSeek model files also set it.

### Output limits: `AQOURS_CODE_MAX_TOKENS`

A model call asks for at most `AQOURS_CODE_MAX_TOKENS` output tokens
(default 8000); a reply cut at that limit is retried once with
`AQOURS_CODE_ESCALATED_MAX_TOKENS` (default 16000). Each must be a positive
whole number, and the escalated limit at least the initial one; otherwise
every agent stops before its first model call and says which setting is
wrong. Unset (or empty), both keep their defaults, as before.

DeepSeek's models (`deepseek-flash`, `deepseek-v4-flash`, `deepseek-v4-pro`
with provider `deepseek`) always use 128000 for both, whatever the two
variables say (`model_api.effective_initial_max_tokens`). With the defaults,
every other model is cut at 8000 or 16000: a thinking model such as MiniMax
spends that on its reasoning, and the tool call never comes.

So that every model works under the same conditions as DeepSeek, **every
non-DeepSeek model file sets**

```text
AQOURS_CODE_MAX_TOKENS=128000
AQOURS_CODE_ESCALATED_MAX_TOKENS=128000
```

(the DeepSeek files leave them out; they would change nothing there). On
OpenRouter, the models of these experiments all allow at least 128000 output
tokens:

| Model | Maximum output (tokens) |
| --- | --- |
| `minimax/minimax-m3.1-flash` | 524,288 or more |
| `qwen/qwen3.8-flash` | 131,072 |
| `anthropic/claude-sonnet-5.5` | 128,000 |

If a later model allows less than 128000, set both to its maximum output and
note that in the experiment's record, since its conditions then differ from
DeepSeek's.

The limits a run used are recorded: `max_tokens: {"initial", "escalated"}`
under `worker` (and `lead_worker`) in the run's `config.json`, in every
`worker_<n>_config.json`, per stage (`step1`, `step2`, `gate`) in a plan's
report, and per call (`step1`, `locate`) in the gate's output. The smoke
check prints them.

`.env` and every `.env.<name>` are ignored by git (`.gitignore`: `.env`,
`.env.*`, `!.env.example`); only `.env.example` is tracked.

`aqours_code.config` loads `.env` with `override=False`: an `AQOURS_CODE_*`
variable set in the shell wins over `.env`. Both scripts warn when one of the
four is set (they print its name, never its value). The same holds for the
two output limits; the smoke check shows the ones the run actually used.

## `use_model.ps1`

```powershell
experiments\taskgraph\models\use_model.ps1 qwen
```

copies `.env.qwen` over `.env` and prints three lines of the new `.env`:

```text
copied .env.qwen to C:\Users\...\Aqours-code\.env
AQOURS_CODE_PROVIDER=openai_compatible
AQOURS_CODE_BASE_URL=https://...
AQOURS_CODE_MODEL=...
```

It never prints the API key: it reads only those three lines. If
`.env.<name>` does not exist, it says so, lists the model files there are, and
exits 1 without touching `.env`; an invalid name exits 2.

## Smoke test

### What it confirms

1. **Tool calls work**: the model calls tools, and every tool input parses as
   JSON (none has `_raw`).
2. **It can edit files**: `mul` is defined in `calc/ops.py` and is not a stub,
   and a test in `tests/test_ops.py` calls it.
3. **It can run tests**: the agent ran `pytest` through `bash` at least once,
   and the final check `python -m pytest -q tests` passed in the sandbox.
4. **It finishes normally**: the agent's last attempt ended with an answer,
   not a worker error, a timeout or the model budget.

The smoke test passes when all four hold and the run status is `success`.

### The task

`template/` is a package `calc` with one function, `add(a, b)` in
`calc/ops.py`, and two tests in `tests/test_ops.py`; they pass on the host
and in the sandbox image (`aqours-code-eval:py311`) with
`python -m pytest -q tests`, without installing anything. `request.md` asks
for `mul(a, b)` in `calc/ops.py` and tests for it in `tests/test_ops.py`,
keeping the existing tests passing.

`smoke.py prepare` copies the template with LF line endings and commits once
with a fixed author, date and message. The commit is
`836c69491eedaa3dd7f5afba0c708fe0bc50bfce` on every platform.

Nothing in the smoke test is specific to a model: it is the normal flow,
`taskgraph single` then `taskgraph run`, with the same flags as the click
experiment except a shorter worker timeout for a much smaller task.

### Running

Docker must be running, with the image `aqours-code-eval:py311`.

```powershell
experiments\taskgraph\models\use_model.ps1 qwen
```

```powershell
experiments\taskgraph\models\smoke\smoke.ps1
```

| Step | What | Command |
| --- | --- | --- |
| 0 preflight | `.env` exists and sets `AQOURS_CODE_MODEL` | |
| 1 prepare | `repo\` from `template\` | `smoke.py prepare` |
| 2 single | `graph.json`, one node | `taskgraph single request.md --final-check "python -m pytest -q tests"` |
| 3 run | `runs\<run_id>\` | `taskgraph run --workers 1 --worker-timeout 600` (default Docker sandbox) |
| 4 check | the report below | `smoke.py check` |

Each run gets its own directory:

```text
C:\tg\smoke\<yyyyMMdd-HHmmss>\
  repo\            the repository (step 1)
  graph.json       the graph (step 2)
  runs\<run_id>\   the taskgraph run directory (step 3)
  prepare.log  single.log  run.log  check.log   the output of each step
  check.json       the check as JSON, to compare models later
```

If a step fails, the script prints `SMOKE FAILED at step <n> (<step>):` with
the reason, the smoke directory, the run directory (or that the run did not
start) and the step's log, and exits 1. When `taskgraph run` finishes without
success, the check still runs first, so the report shows why. On success it
prints `SMOKE PASSED` and exits 0.

Options:

- `-Root <dir>`: put the smoke directory under `<dir>` instead of `C:\tg\smoke`;
- `-PrepareOnly`: stop after step 1;
- `-CheckRun <run_dir>`: only step 4, on an existing run directory;
- `-EnvFile <file>`: the `.env` whose `AQOURS_CODE_MODEL` is reported
  (default: the repository's `.env`; it does not change what the run uses).

### The report

```text
Smoke check of C:\tg\smoke\...\runs\<run_id>
  model in .env      : <AQOURS_CODE_MODEL of .env>
  model of the run   : <provider> / <model>      (config.json of the run)
  output limits      : initial 128000, escalated 128000
  run status         : success  [solve: merged, attempts 1]
  model calls        : ...
  tokens             : ... in / ... out
  final check        : passed
      ok   python -m pytest -q tests  (exit 0)
  mul implemented    : yes  (def mul in calc/ops.py)
  mul tested         : yes  (test_mul)
  files changed      : calc/ops.py, tests/test_ops.py
Logs (1 trace file(s), 1 stdout file(s)):
  HTTP errors on model requests         : 0
  replies cut at max_tokens             : 0
  tool inputs that did not parse (_raw) : 0
  escape attempts                       : 0
Smoke result: PASS
  [ok  ] tool calls work  : ...
  [ok  ] edits files      : ...
  [ok  ] runs tests       : ...
  [ok  ] finishes normally: ...
```

The output limits are `worker.max_tokens` of the run's `config.json` (what the
worker's child process reported before the run). The model calls and tokens
are the run's totals (`summary.json`). `mul` is
read from the integration commit in `runs\<run_id>\repo` and parsed, not run.
The log counts come from every attempt of every node under `nodes\`, up to
three examples each (anything that looks like a key is replaced by
`<redacted>`):

- **HTTP errors on model requests**: `error` events of `trace_<n>.jsonl`
  whose message has an HTTP status (`Model request failed: HTTP 400: ...`,
  `Error code: 529 - ...`; an error recorded again as it propagates counts
  once), and retries in `worker_<n>_stdout.txt` (`[429] retry ...`,
  `[provider_http_503] retry ...`). Timeouts and connection errors are not
  HTTP errors. Retries made inside the `anthropic` SDK itself are not logged,
  so they are not counted.
- **Replies cut at max_tokens**: `llm_response` events with `stop_reason`
  `max_tokens`.
- **Tool inputs that did not parse**: `tool_use` events whose input has
  `_raw`, which `model_api` uses when a tool call's arguments are not JSON.
- **Escape attempts**: `aqours_code.taskgraph.escapes.scan_run`, the same
  count as `escape_attempts_total` in `summary.json`.

Only `_raw` makes the smoke test fail by itself (tool calls work); the other
three are reported and fail it only through what they cause (a failed run, no
answer). If the run used a different model from `.env`, the report says so.

`smoke.py check <run_dir> [--env-file <file>] [--json <file>]` prints the same
report for any taskgraph run directory; it exits 0 if the smoke test passes,
1 if not, and 2 if the directory does not exist.

## Tests

No model is called:

```powershell
.venv\Scripts\python.exe -m pytest -q tests\taskgraph\test_models_smoke.py
```

```powershell
.venv\Scripts\python.exe -m pytest -q -m docker tests\taskgraph\test_models_smoke.py
```

The first tests the repository preparation and the check (on fake run
directories), `smoke.ps1 -PrepareOnly`, `smoke.ps1 -CheckRun`, the preflight,
`use_model.ps1` on a temporary root, and `.gitignore`. The second runs the
template's tests in the sandbox image.
