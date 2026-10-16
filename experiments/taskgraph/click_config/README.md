# Click config task

One feature, spread over several files whose parts depend on each other, in
a large real repository. One request ([`request.md`](request.md)) adds
configuration files to the [click](https://github.com/pallets/click)
command-line library: read a TOML or JSON file, use it as parameter values
(after the command line and the environment, before `default_map` and the
defaults), reject unknown keys, and add `--config` and `--save-config`
options.

Everything here is data: neither Aqours nor `aqours_code.taskgraph` imports it.
`click_batch/` is unchanged; this task reuses its base repository.

## Why this task

The two earlier tasks sit at the two ends:

| Task | Repository | Change | Coupling |
| --- | --- | --- | --- |
| [job platform](../job_platform/README.md) | small | large: six features | strong: every feature goes through the same core |
| [click batch](../click_batch/README.md) | large (about 30,000 lines) | eight small tickets | none: one module each, nothing shared but `__init__.py` |
| **click config** | large, the same as click batch | **one feature, about 700 changed lines in five files** | **real dependencies between the parts** |

This task is in between. The repository is the same as click batch, so an
agent still has to find its way in about 13,000 lines of source and 16,000
lines of tests, but the change is one feature whose parts build on each
other:

```text
exceptions.py   ConfigError ─────────────────────────────┐
     ▲                                                   │
config.py       load_config, dump_config (TOML writer)   │
     ▲                                                   ▼
     │          core.py   ParameterSource.CONFIG, Context.config / config_path /
     │                    lookup_config, Parameter(config_key), value resolution,
     │                    unknown-key check, help output
     │                         ▲
decorators.py   config_option (load_config → ctx.config, eager)
                save_config_option (sources, config keys, processed values → dump_config)
     ▲
__init__.py     the five exports
```

`config_option` cannot work before `load_config` and `Context.config` exist,
`save_config_option` needs the configuration keys, the new parameter source
and `dump_config`, and the help output needs the value lookup. A task graph
has to choose an order and a contract here; it cannot split the work into
independent tickets as in click batch. A single agent, on the other hand,
has to keep the whole feature in mind across five files of a large
repository.

```text
README.md           this file
request.md          the request
make_repo.py        builds the base repository (calls ../click_batch/make_repo.py)
reference/
  config.patch      reference solution: one patch, src/click/ only (never shown to workers)
hidden_tests/       hidden tests (never shown to workers)
  test_c01_load.py ... test_c10_help.py   one file per section of the request
  regression/       click's own tests at the snapshot commit (copy of click_batch's)
  conftest.py       the same as click_batch's
  sandbox_skips.txt the same as click_batch's
tools/
  verify.py         checks the hidden tests against the base and the reference
```

## The base repository

The base repository is **the click batch base repository, unchanged**: the
same vendor snapshot (`../click_batch/vendor/click/`, click at commit
`2247b35e`, BSD-3-Clause), the same added files (root `conftest.py`,
`src/click.egg-info/PKG-INFO`, `sandbox_skips.txt`), the same commit and the
same Docker image (`aqours-code-eval:py311`). See
[click batch: the base repository](../click_batch/README.md#the-base-repository)
and [Tests skipped in the sandbox](../click_batch/README.md#tests-skipped-in-the-sandbox).

```bash
python experiments/taskgraph/click_config/make_repo.py <dest> [--with-reference]
```

does not copy the snapshot itself: it calls `make_repo()` of
`../click_batch/make_repo.py` and prints the commit, which is
`bd8caad9078c90c90f38d096fb63650adf1213fd` on every platform. With
`--with-reference` it then applies `reference/config.patch` (instead of the
click batch patches) to the working tree, without committing it.

For runs there is no need to build a new repository: `C:\tg\click-batch2`
(the click batch repository, at `bd8caad9`) is used as it is.

## The request

[`request.md`](request.md) has ten sections. It asks for tests of the new
behaviour too, and for every existing test to keep passing.

| Section | What it adds | Main file |
| --- | --- | --- |
| Reading a configuration file | `load_config(path)`: suffix decides TOML or JSON, `-` → `_` in keys, `ConfigError` for each failure | `config.py` (new) |
| Writing a configuration file | `dump_config(data, path)`: JSON, and a TOML writer of click's own | `config.py` |
| `click.ConfigError` | `ConfigError(message, path=None)`, exit code 2 | `exceptions.py` |
| Configuration in the context | `Context(config=, config_path=)`, inheritance by subcommands, `lookup_config` | `core.py` |
| Configuration keys of parameters | `Parameter(config_key=)`, dotted keys | `core.py` |
| Values from the configuration | `ParameterSource.CONFIG`, the order of the sources, type conversion, lists and strings, flags, prompts | `core.py` |
| Unknown keys | the check after the parameters, `ignore_unknown_config` | `core.py` |
| `click.config_option` | the eager `--config` option, `default_paths` | `decorators.py` |
| `click.save_config_option` | `--save-config`, which writes the given values and exits | `decorators.py` |
| Help output | `[default: {value} (config)]` in `--help` | `core.py` |

## Reference solution

`reference/config.patch` is one patch that changes only `src/click/` and adds
no test. `make_repo.py <dest> --with-reference` applies it. It exists to
check the hidden tests; no agent sees it.

Lines changed (`git diff --numstat`, added / deleted):

| File | Added / deleted | What it changes |
| --- | --- | --- |
| `src/click/config.py` (new) | 253 / 0 | `load_config`: format from the suffix (case ignored), UTF-8, `tomllib` / `json`, `-` → `_` in every key, a `ConfigError` with `path=str(path)` for each failure. `dump_config`: checks the values and drops `None`, writes JSON with `json`, and TOML itself (bare or quoted keys, string escapes, values before tables, `[outer.inner]` headers). `json` and `tomllib` are imported inside the functions, because click's `tests/test_imports.py` limits the modules that `import click` may load |
| `src/click/exceptions.py` | 25 / 0 | `ConfigError(message, path=None)`: `exit_code = 2`, `path`, `format_message()` |
| `src/click/core.py` | 249 / 21 | `ParameterSource.CONFIG` between `ENVIRONMENT` and `DEFAULT_MAP`. `Context`: `config`, `config_path`, `ignore_unknown_config` (each inherited like `default_map`), `lookup_config()`. `Parameter`: `config_key`, `get_config_key()`, `value_from_config()` (a string is split like an environment variable), the configuration step in `consume_value()`, `"config_key"` in `to_info_dict()` (only when given, see [Problems in the request](#problems-in-the-request)). `Command.parse_args()`: the unknown-key check (`Group` also accepts subcommand tables) and the callbacks that run once all parameters are processed (used by `save_config_option`). `Option.get_help_extra()`: the configured value as `[default: … (config)]`; the formatting of defaults moves into `_format_help_default()`, which accounts for the 21 deleted lines |
| `src/click/decorators.py` | 145 / 0 | `config_option` (eager `--config`, `default_paths`, `~` expanded) and `save_config_option` (writes the exposed values whose source is the command line, the environment or the configuration, tuples as lists, other types as `str`, then prints `Saved configuration to {path}` and exits with 0) |
| `src/click/__init__.py` | 5 / 0 | exports `load_config`, `dump_config`, `ConfigError`, `config_option`, `save_config_option` |
| **total, 5 files** | **677 / 21 = 698** | the patch file has 962 lines |

The code follows click's style: type annotations, docstrings with
`versionadded` / `versionchanged` 8.6, and `_()` around text shown to users.
With the reference, click's own tests pass (`python -m pytest -q tests`:
2,184 passed, 102 skipped, 1 xfailed on a Windows host; 2,235 passed, 51
skipped, 1 xfailed in the sandbox), the same counts as on the base.

## Hidden tests

`hidden_tests/` is used like click batch's: the Coordinator copies it into
the repository as `_hidden_tests/` and runs `python -m pytest -q
_hidden_tests` (`--hidden-tests`). Its `conftest.py` (the same file as click
batch's) puts `src` on `sys.path` and `PYTHONPATH` and skips the 27 tests of
`sandbox_skips.txt` in `regression/`.

- `regression/` is click's own test suite at the snapshot commit, a byte for
  byte copy of `click_batch/hidden_tests/regression/` (`tools/verify.py`
  compares the SHA-256 of every file). An agent that edits a public test to
  make it pass is still measured against the original.
- `test_c01_load.py` ... `test_c10_help.py` are the functional tests: 73
  tests, one file per section of the request.

| File | Section of the request | Tests |
| --- | --- | --- |
| `test_c01_load.py` | Reading a configuration file | 10 |
| `test_c02_dump.py` | Writing a configuration file | 11 |
| `test_c03_error.py` | `click.ConfigError` | 4 |
| `test_c04_context.py` | Configuration in the context | 7 |
| `test_c05_config_key.py` | Configuration keys of parameters | 5 |
| `test_c06_values.py` | Values from the configuration (and the order of the sources) | 10 |
| `test_c07_unknown.py` | Unknown keys | 7 |
| `test_c08_config_option.py` | `click.config_option` | 8 |
| `test_c09_save_config.py` | `click.save_config_option` | 7 |
| `test_c10_help.py` | Help output | 4 |
| **total** | | **73** |

The rules are those of click batch:

- A test checks **only behaviour that `request.md` states**, through the
  public interface the request names. The docstring of every test quotes the
  sentences it checks.
- Every test uses `tmp_path` for files, does not depend on the order of the
  tests, does not use the network, and gives the same result every time.
- The TOML that `dump_config` writes is mostly checked by reading it back
  with `load_config` (the request promises equality). Lines are compared
  exactly only for what the request fixes: `key = value` lines, values
  before tables, a blank line and a `[name]` / `[outer.inner]` header per
  table, bare or quoted keys, string escapes, and how booleans, numbers and
  lists are written. Whether the hex digits of `\uXXXX` are upper or lower
  case is left open, so that test compares case-insensitively.
- Where the request is silent or unclear, there is no test. Known gaps,
  deliberately untested:
  - `Parameter.to_info_dict()`: the sentence contradicts "keep every existing
    test passing" (see [Problems in the request](#problems-in-the-request));
  - `load_config` on Python older than 3.11 (the host and the image run 3.11,
    and the public interface gives no way to pretend otherwise); a file that
    is not valid UTF-8; arrays of tables (whether keys of tables inside a
    list count as "nested");
  - `ParameterSource.CONFIG`'s documentation: the docstring of an enum member
    is not available at runtime;
  - subcommands whose names contain `-` (see
    [Problems in the request](#problems-in-the-request));
  - TOML output: a leading blank line when the top-level mapping has only
    tables, the final newline, DEL and C1 characters, non-ASCII characters in
    strings (only read back), mappings or `None` inside lists, keys that are
    not strings; a file that cannot be written;
  - `lookup_config` with `-` in the key, and keys with `-` in a mapping given
    as `config=`;
  - `config_option`: `ctx.config_path` for a `~` default path (as written or
    expanded), `--help --config FILE` in that order;
  - the options of `config_option`, `save_config_option` and `--help`
    reading their own value from the configuration (the general rule says
    every parameter does, so a configuration with `save_config = "…"` would
    save);
  - unknown keys: a key whose value is JSON `null`, whether the check comes
    before the command's callback, its order relative to other errors;
  - `save_config_option`: a processed value of `None`, parameters that share
    a name, a configuration key that is both a value and a table;
  - help output: a configured value together with `show_default=False` or a
    string `show_default`, a configured `false` for a boolean flag without a
    secondary name.

On the base repository no functional test passes (65 fail, 8 error in
fixtures that use the new interface) and `regression/` passes as click's own
tests do; with the reference every hidden test passes, on the host and in
the sandbox. [`tools/verify.py`](tools/verify.py) checks this.

## Problems in the request

Found while writing the tests, on 2026-10-15, before any run. `request.md` is
kept as written; these are for a decision, and each would change
`request.md`, the reference and possibly a test.

1. **`Parameter.to_info_dict()` contradicts "Keep every existing test
   passing".** The request says the info dict includes `"config_key"`: the
   given value, or `None`. Click's `tests/test_info_dict.py` (and so
   `hidden_tests/regression/test_info_dict.py`) compares whole info dicts with
   `==`, so a new key in every parameter's dict fails 7 of its tests
   (`test_parameter[Option]`, `[Flag Option]`, `[Argument]`,
   `test_command[Command]`, `[Group]`, `[Nested Group]`, `test_context`). An
   agent that follows the sentence loses 7 regression tests; an agent that
   ignores it loses nothing. The reference adds `"config_key"` only when it
   was given, which keeps the existing tests passing, and no hidden test
   checks the sentence. A possible rewording: "`Parameter.to_info_dict()`
   includes `"config_key"` when `config_key` is given." (or leave the
   sentence out).
2. **Subcommands whose names contain `-`.** Click names a subcommand
   `show-all` for a function `show_all`, and `load_config` turns a table
   `[show-all]` into `show_all`. A child context looks up "the parent's value
   under its `info_name`" (`show-all`), so it finds nothing, and the group's
   unknown-key check reports `show_all`, since it is not "the name of one of
   its subcommands". A file can therefore never configure such a subcommand.
   The reference follows the request literally; the tests use subcommand
   names without `-`. A possible addition: "A subcommand's table is looked up
   under its name with `-` replaced by `_` as well."

## Verification

```bash
python experiments/taskgraph/click_config/tools/verify.py [--no-docker] [--json OUT]
```

builds the repositories in a temporary directory with `make_repo.py`, calls
no model, and runs the hidden tests through the Coordinator's own
`checks.run_hidden_tests` (host: `HostChecks`, sandbox: `SandboxChecks`).
Results on 2026-10-15 (Windows host, Python 3.11.7; image
`aqours-code-eval:py311`):

| # | Check | Result |
| --- | --- | --- |
| 1 | base: functional tests that pass | **0 of 73** (65 failed, 8 errors); `regression/`: 2,184 passed, 103 skipped |
| 2 | base + reference: hidden tests, host | 2,257 passed, 0 failed, 103 skipped (8.9 s) |
| 2 | base + reference: hidden tests, sandbox | 2,308 passed, 0 failed, 52 skipped |
| 3 | base + reference: `tests/`, host / sandbox | 2,184 passed, 102 skipped, 1 xfailed / 2,235 passed, 51 skipped, 1 xfailed |
| 4 | hidden tests in the sandbox | **11.8 s** (`duration_s` of `run_hidden_tests`; 13.9 s wall time), below 60 s |
| 5 | reference without `save_config_option` | only `test_c09` fails (0/7); `test_c01` 10/10, `test_c02` 11/11, every other section passes |
| 5 | reference without the help output | only `test_c10` fails (0/4); every other section passes |
| 6 | `regression/`, `conftest.py`, `sandbox_skips.txt` against click batch | 61 files, all SHA-256 equal |

(The skipped counts include the one xfailed test, which JUnit reports as
skipped.)

## Running

On the host, from the root of this repository, with the click batch
repository `C:\tg\click-batch2` (at `bd8caad9`):

```bash
# planner (v3), then the planned graph
python -m aqours_code.taskgraph plan experiments\taskgraph\click_config\request.md --repo C:\tg\click-batch2 --out C:\tg\solve\click-config\graph.json --final-check "python -m pytest -q tests"
python -m aqours_code.taskgraph run C:\tg\solve\click-config\graph.json --repo C:\tg\click-batch2 --workers 8 --worker-timeout 3600 --hidden-tests experiments\taskgraph\click_config\hidden_tests --out C:\tg\runs
# single agent
python -m aqours_code.taskgraph single experiments\taskgraph\click_config\request.md --repo C:\tg\click-batch2 --out C:\tg\solve\click-config-single\graph.json --final-check "python -m pytest -q tests"
python -m aqours_code.taskgraph run C:\tg\solve\click-config-single\graph.json --repo C:\tg\click-batch2 --workers 1 --worker-timeout 3600 --hidden-tests experiments\taskgraph\click_config\hidden_tests --out C:\tg\runs
# team (a lead plans, teammates work in parallel)
python -m aqours_code.taskgraph team experiments\taskgraph\click_config\request.md --repo C:\tg\click-batch2 --final-check "python -m pytest -q tests" --hidden-tests experiments\taskgraph\click_config\hidden_tests --out C:\tg\runs
```

To check the material itself (no model is called):

```bash
python experiments/taskgraph/click_config/tools/verify.py
```
