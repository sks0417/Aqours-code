# Click batch task

A batch of eight unrelated small tickets on a large, real repository. One
request ([`request.md`](request.md)) asks for eight independent changes to the
[click](https://github.com/pallets/click) command-line library, each in a
different module, to compare a single agent with a hand-written task graph
(one thin contract and eight parallel nodes).

Everything here is data: neither Aqours nor `aqours_code.taskgraph` imports it.

## Why this task

The [job platform task](../job_platform/README.md) is "small repository, large
change": a single agent reads the repository once, while every node of a task
graph reads its context again, so the graph pays for its structure. This task
is the opposite: the repository is large (about 12,900 lines of source and
16,300 lines of tests), the change is small (eight tickets, about 780 changed
lines in the reference, tests included), and the tickets are spread over
eight modules. It asks whether a single agent struggles as its context grows,
and whether a graph whose nodes each see one module does better.

```text
README.md           this file
request.md          the request: eight tickets
make_repo.py        builds the deterministic base repository
vendor/click/       snapshot of click (source, tests, pyproject.toml, README.md, LICENSE.txt)
hidden_tests/       hidden tests (never shown to workers)
  test_t1_duration.py ... test_t8_hint.py   one file per ticket
  regression/       click's own tests at the snapshot commit, unchanged
  conftest.py
reference/          reference solution: one patch per ticket (never shown to workers)
graphs/
  single.json       one-node graph (single-agent baseline)
  handwritten.json  hand-written task graph
```

## Source and licence

`vendor/click/` is a snapshot of **click**, <https://github.com/pallets/click>,
at commit `2247b35ea1c47c727d7a06e51fa280e12a863ff6` (2026-10-04, version
`8.6.0.dev`). Click is distributed under the **BSD-3-Clause** licence; the
licence text is kept unchanged in
[`vendor/click/LICENSE.txt`](vendor/click/LICENSE.txt) (Copyright 2014
Pallets). The snapshot contains only `src/click/`, `tests/`, `pyproject.toml`,
`README.md` and `LICENSE.txt`, all unmodified; `docs/`, `examples/`,
`uv.lock`, `.github/` and `CHANGES.md` are left out. Click is pure Python with
no runtime dependencies, and its tests need only pytest.

| | Files | Lines |
| --- | --- | --- |
| `src/click/*.py` | 17 | 12,930 |
| `tests/**/*.py` (43 test modules, 12 typing examples, helpers) | 59 | 16,294 |

`hidden_tests/regression/` is a second, unchanged copy of the snapshot's
`tests/` (BSD-3-Clause as well), and `reference/*.patch` contain context
lines of click's source.

## The base repository

```bash
python experiments/taskgraph/click_batch/make_repo.py <dest> [--with-reference]
```

copies the snapshot to `<dest>` with LF line endings, adds the two files
below, and commits once with a fixed author, date and message. The commit is
`a9af6116c1c33ee893651f3523605a6e3dfaa643` on every platform.

The package is not installed in the repository (nor in the Docker image the
workers run in), so `make_repo.py` adds what an installation would provide:

- `conftest.py` in the repository root puts `src` on `sys.path`, and on
  `PYTHONPATH` for the two click tests that import click in a Python
  subprocess (`tests/test_imports.py`, `tests/test_types/test_Path.py`);
- `src/click.egg-info/PKG-INFO` (name and version only), because
  `tests/test_deprecations.py` compares `click.__version__` with
  `importlib.metadata.version("click")`.

There is no `SPEC.md`: the tickets are the specification. With
`python -m pytest -q tests`, click's own tests pass on the base: 2,184 passed,
102 skipped, 1 xfailed on Windows with Python 3.11 (the skips are tests for
other platforms; click's `pyproject.toml` deselects its 31,000 stress tests
and turns warnings into errors). The run takes about 5 seconds.

## The request

Eight tickets, each in one module. Four add a name that is exported from
`click`, which is the only thing the tickets share (`src/click/__init__.py`).

| # | Ticket | Module | Exported from `click` |
| --- | --- | --- | --- |
| 1 | `click.Duration` parameter type | `types.py` | `Duration` |
| 2 | `click.select` | `termui.py` | `select` |
| 3 | `Result.output_lines`, `Result.assert_success` | `testing.py` | |
| 4 | Nushell completion (`NuComplete`) | `shell_completion.py` | |
| 5 | `click.mutually_exclusive` | `decorators.py` | `mutually_exclusive` |
| 6 | `click.format_size` | `utils.py` | `format_size` |
| 7 | `wrap_text(preserve_bullets=...)` | `formatting.py` | |
| 8 | `hint=` on `ClickException` and its subclasses | `exceptions.py` | |

## Reference solution

`reference/t1_duration.patch` ... `reference/t8_hint.patch`, one per ticket,
applied in name order by `make_repo.py <dest> --with-reference` (each patch
applies on top of the previous ones; the working tree is left uncommitted).
Each patch changes the ticket's module, adds a public test file, and, for
tickets 1, 2, 5 and 6, adds one export line to `src/click/__init__.py`. The
reference exists to check the hidden tests; no agent sees it.

Lines changed (`git diff --numstat`, added / deleted):

| Ticket | Source | Public tests (new file) | Total |
| --- | --- | --- | --- |
| 1 Duration | 102 / 0 | 58 (`tests/test_duration.py`) | 160 |
| 2 select | 45 / 0 | 41 (`tests/test_select.py`) | 86 |
| 3 Result helpers | 29 / 0 | 33 (`tests/test_result_helpers.py`) | 62 |
| 4 Nushell | 49 / 0 | 43 (`tests/test_nushell_completion.py`) | 92 |
| 5 mutually_exclusive | 54 / 0 | 47 (`tests/test_mutually_exclusive.py`) | 101 |
| 6 format_size | 36 / 0 | 29 (`tests/test_format_size.py`) | 65 |
| 7 bullets | 71 / 1 | 41 (`tests/test_wrap_bullets.py`) | 113 |
| 8 hints | 41 / 10 | 51 (`tests/test_error_hints.py`) | 102 |
| **total** | **427 / 11 = 438** | **343** | **781** |

With the reference, `python -m pytest -q tests` passes (2,251 passed, 102
skipped, 1 xfailed on Windows) and so do all hidden tests.

The Nushell source script of the reference has not been run in Nushell; the
request only fixes what the script contains and what `nu_complete` prints.

## Hidden tests

`hidden_tests/` is copied to `_hidden_tests/` in the repository and run with
`python -m pytest -q _hidden_tests` (the Coordinator does this with
`--hidden-tests`). Its `conftest.py` puts `src` on `sys.path` and `PYTHONPATH`
itself, so it does not depend on the repository's `conftest.py`.

- `regression/` is click's own test suite as it was at the snapshot commit.
  An agent that edits a public test to make it pass is still measured against
  the original.
- `test_t<N>_*.py` are the tests of ticket N: 70 tests, 7 to 10 per ticket.

The job platform task had three hidden tests that checked behaviour its
specification did not state. The rules here:

- A test checks **only behaviour that `request.md` states**, through click's
  public interface. The docstring of every test quotes the sentence it
  checks; the tables below list them.
- No test looks at private names, at the implementation, or at an order or
  format the request leaves open. Where the request is silent, there is no
  test. Known gaps, deliberately untested:
  - ticket 1: numbers that are not strings (`90`), a `timedelta` outside the
    bounds, forms such as `".5"` or `"1e3"`;
  - ticket 2: surrounding whitespace in the answer, an option whose text is
    itself a number, how the default is displayed beyond `[2]`;
  - ticket 3: the wording of the assertion messages, `output_lines` of an
    empty output;
  - ticket 4: the content of the source script beyond the two required
    strings, carriage returns in help text;
  - ticket 5: options with several names (`-j/--json`), arguments, how the
    options are joined in the message, whether options that were not given
    are absent from it;
  - ticket 6: rounding of values that are not exact to one decimal, float
    input;
  - ticket 7: what separates one item from the next or from the surrounding
    text (the tests compare non-empty lines), indented bullet lines, the
    combination with `initial_indent` / `subsequent_indent`, text after the
    last item in the same paragraph;
  - ticket 8: `FileError`, whose existing positional argument is also named
    `hint`, and subclasses other than `UsageError` and `BadParameter`.

On the base repository every ticket test fails (70 of 70) and `regression/`
gives the same result as the public tests; on base + reference all hidden
tests pass. `tests/taskgraph/test_click_batch_task.py` checks this.

**`test_t1_duration.py`** (10 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_defined_in_types_and_exported` | Add a parameter type class `Duration` in `click/types.py`, exported from `click`. |
| `test_plain_number_means_seconds` | A plain non-negative number means seconds: "90", "1.5". |
| `test_unit_parts` | Otherwise the value is one or more `<number><unit>` parts with units `d`, `h`, `m`, `s`: "45s", "2d", "1h30m", "1d2h3m4s", "1.5h". |
| `test_timedelta_is_returned_unchanged` | A `datetime.timedelta` value is returned unchanged. |
| `test_other_values_fail_as_invalid` | units ... (lowercase), in this order, each unit at most once, without spaces. / Any other value fails. / The message for an invalid value contains `is not a valid duration`. |
| `test_command_reports_invalid_value_as_usage_error` | Failures use the parameter type's normal failure mechanism, so a command reports them as a usage error (exit code 2). |
| `test_command_receives_timedelta` | ... a parameter type class `Duration` ... that converts a duration to `datetime.timedelta`. |
| `test_bounds_are_inclusive` | `Duration(min=None, max=None)` takes optional `timedelta` bounds (inclusive). |
| `test_value_outside_bounds_fails` | A value outside the bounds fails. / the message for a value outside the bounds contains `is out of range`. |
| `test_name_metavar_and_info_dict` | The type's `name` is "duration" and its metavar in help output is `DURATION`. `to_info_dict()` includes "min" and "max", each `None` or the bound in seconds as a float. |

**`test_t2_select.py`** (9 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_defined_in_termui_and_exported` | Add `select(text, options, default=None)` in `click/termui.py`, exported from `click`. |
| `test_prints_text_then_numbered_options` | It prints `text`, then each option on its own line as `␣␣{n}) {option}` (two spaces) with `n` counting from 1 |
| `test_prompts_for_a_number_after_the_options` | then prompts with `click.prompt` using the prompt text `Enter a number`. |
| `test_number_selects_option` | The answer may be a number from 1 to the number of options ... `select` returns the chosen option string. |
| `test_exact_text_selects_option` | The answer may be ... the exact text of an option. |
| `test_default_is_the_number_of_the_default_option` | If `default` is given, the prompt's default is the default option's number. |
| `test_other_answers_print_an_error_and_prompt_again` | For any other answer it prints `Error: '{answer}' is not a valid choice.` and prompts again. |
| `test_empty_options_raise_value_error` | An empty `options` list ... raises `ValueError`. |
| `test_default_that_is_not_an_option_raises_value_error` | a `default` that is not one of the options, raises `ValueError`. |

**`test_t3_result.py`** (8 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_output_lines_splits_output_into_lines` | `Result.output_lines`: a property returning `output` split into lines, without line endings. |
| `test_output_lines_keeps_an_empty_line` | `Result.output_lines`: ... `output` split into lines, without line endings. |
| `test_output_lines_without_final_line_ending` | `Result.output_lines`: ... `output` split into lines, without line endings. |
| `test_assert_success_returns_the_result` | It returns the result itself. |
| `test_assert_success_raises_for_another_exit_code` | `Result.assert_success(output=None)`: raises `AssertionError` if `exit_code` is not 0; the message contains the exit code and the output. |
| `test_assert_success_accepts_equal_output` | If `output` is given, it also raises `AssertionError` when `output` is not exactly equal to it. It returns the result itself. |
| `test_assert_success_raises_for_different_output` | If `output` is given, it also raises `AssertionError` when `output` is not exactly equal to it. |
| `test_assert_success_checks_exit_code_when_output_matches` | raises `AssertionError` if `exit_code` is not 0 |

**`test_t4_nushell.py`** (7 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_class_is_registered_as_nu` | Support Nushell as a completion shell named `nu`, through a class `NuComplete` in `click/shell_completion.py` registered in the same way as the existing shells |
| `test_source_prints_a_script` | so that `_{PROG}_COMPLETE=nu_source` prints a source script / The source script contains the program name and the string `nu_complete`. |
| `test_complete_prints_value_and_help` | `_{PROG}_COMPLETE=nu_complete` prints completions. / Each completion is printed on its own line as `{value}\t{help}`, or just `{value}` when there is no help. |
| `test_complete_uses_the_word_at_comp_cword` | Completion input is read like the PowerShell completer: environment variable `COMP_WORDS` (the command line) and `COMP_CWORD` (the index of the word being completed). |
| `test_complete_inside_a_subcommand` | `COMP_WORDS` (the command line) and `COMP_CWORD` (the index of the word being completed). |
| `test_newlines_and_tabs_in_help_become_spaces` | Newlines and tabs in the help text are replaced by spaces. |
| `test_only_value_and_help_are_printed` | Each completion is printed on its own line as `{value}\t{help}`, or just `{value}` when there is no help. |

**`test_t5_exclusive.py`** (9 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_defined_in_decorators_and_exported` | Add a decorator `mutually_exclusive(*names)` in `click/decorators.py`, exported from `click`. |
| `test_one_given_parameter_runs_the_command` | When the command runs and more than one of the named parameters was given ... the command fails (one given parameter is not more than one). |
| `test_defaults_do_not_count` | given on the command line or through an environment variable (not just left at its default) |
| `test_two_given_parameters_fail_with_usage_error` | the command fails with a usage error (exit code 2) whose message contains every conflicting option as it appears in help (for example `--json` and `--csv`) and the words `cannot be used together`. |
| `test_value_given_on_the_command_line_counts_even_if_equal_to_default` | more than one of the named parameters was given on the command line ... (not just left at its default) |
| `test_environment_variable_counts` | given on the command line or through an environment variable |
| `test_message_names_every_conflicting_option` | whose message contains every conflicting option as it appears in help |
| `test_fewer_than_two_names_raise_value_error_on_invoke` | Fewer than two names ... raises `ValueError` when the command is invoked. |
| `test_unknown_name_raises_value_error_on_invoke` | a name that is not a parameter of the command, raises `ValueError` when the command is invoked. |

**`test_t6_size.py`** (9 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_defined_in_utils_and_exported` | Add `format_size(num_bytes, binary=False)` in `click/utils.py`, exported from `click`, returning a human-readable size. |
| `test_values_below_1000_are_integer_bytes` | Values below 1000 (or 1024) are printed as an integer: "512 B". |
| `test_binary_values_below_1024_are_integer_bytes` | Values below 1000 (or 1024) are printed as an integer |
| `test_decimal_units` | With `binary=False` it uses powers of 1000 and units `B`, `kB`, `MB`, `GB`, `TB`, `PB` |
| `test_binary_units` | with `binary=True` powers of 1024 and units `B`, `KiB`, `MiB`, `GiB`, `TiB`, `PiB`. |
| `test_binary_defaults_to_false` | `format_size(num_bytes, binary=False)` |
| `test_largest_unit_that_keeps_the_number_at_least_one` | Larger values use the largest unit that keeps the number at least 1, with one decimal: "1.5 kB", "2.0 MiB". |
| `test_beyond_the_last_unit_the_last_unit_is_used` | Beyond the last unit, the last unit is used. |
| `test_negative_values_raise_value_error` | Negative values raise `ValueError`. |

**`test_t7_bullets.py`** (10 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_default_output_is_exactly_as_before` | With the default `False`, the output is exactly as before. |
| `test_dash_items_are_wrapped_on_their_own` | a line starting with `- ` ... starts a new bullet item: the item is wrapped to the width on its own, and its continuation lines are indented so that they line up with the text after the bullet marker. |
| `test_star_items` | a line starting with `- `, `* `, ... starts a new bullet item |
| `test_numbered_items_line_up_after_the_number` | or a number followed by `. ` ... its continuation lines are indented so that they line up with the text after the bullet marker. |
| `test_every_item_starts_its_own_line_when_it_fits` | a line starting with `- ` ... starts a new bullet item |
| `test_item_runs_until_the_next_item` | a line starting with `- ` ... starts a new bullet item: the item is wrapped to the width on its own |
| `test_no_line_is_wider_than_the_width` | the item is wrapped to the width on its own |
| `test_text_without_items_is_wrapped_as_before` | Text that is not a bullet item is wrapped as before. |
| `test_text_before_items_is_wrapped_as_before` | Text that is not a bullet item is wrapped as before. |
| `test_items_between_paragraphs` | Text that is not a bullet item is wrapped as before. (with `preserve_paragraphs`, the paragraphs around a list) |

**`test_t8_hint.py`** (8 tests)

| Test | Sentence of the request |
| --- | --- |
| `test_hint_attribute` | Give `click.ClickException` ... an optional keyword argument `hint=None` and an attribute `hint`. |
| `test_show_prints_the_hint_as_final_line` | When `hint` is set, `show()` prints an extra final line `Hint: {hint}` after the usual error output. |
| `test_show_without_hint_is_unchanged` | Without a hint, the output is unchanged. |
| `test_usage_error_takes_a_hint` | (and so all its subclasses, including `UsageError`) |
| `test_bad_parameter_takes_a_hint` | (and so all its subclasses, including `UsageError`) |
| `test_command_shows_hint_of_click_exception` | When `hint` is set, `show()` prints an extra final line `Hint: {hint}` after the usual error output. |
| `test_command_shows_hint_after_usage_error_output` | an extra final line `Hint: {hint}` after the usual error output. |
| `test_command_output_without_hint_is_unchanged` | Without a hint, the output is unchanged. |

## Graphs

### single

`graphs/single.json` is generated by `python -m aqours_code.taskgraph single
request.md --repo <repo> --final-check "python -m pytest -q tests"`: one
unrestricted node whose goal is the request.

### handwritten

```text
  A ──────┬──► T1 types.py              T3 testing.py
contract  ├──► T2 termui.py             T4 shell_completion.py
(exports) ├──► T5 decorators.py         T7 formatting.py
          └──► T6 utils.py              T8 exceptions.py
```

| Node | Kind | Edits | What it does |
| --- | --- | --- | --- |
| A | contract | `__init__.py`; stubs in `types.py`, `termui.py`, `decorators.py`, `utils.py` | exports `Duration`, `select`, `mutually_exclusive` and `format_size` from `click`. So that `import click` keeps working, it adds each name to its module as a stub (signature, docstring, no behaviour). Writes no test |
| T1 | implement | `types.py`, `tests/test_duration.py` | ticket 1; replaces the stub |
| T2 | implement | `termui.py`, `tests/test_select.py` | ticket 2; replaces the stub |
| T3 | implement | `testing.py`, `tests/test_result_helpers.py` | ticket 3 |
| T4 | implement | `shell_completion.py`, `tests/test_nushell_completion.py` | ticket 4 |
| T5 | implement | `decorators.py`, `tests/test_mutually_exclusive.py` | ticket 5; replaces the stub |
| T6 | implement | `utils.py`, `tests/test_format_size.py` | ticket 6; replaces the stub |
| T7 | implement | `formatting.py`, `tests/test_wrap_bullets.py` | ticket 7 |
| T8 | implement | `exceptions.py`, `tests/test_error_hints.py` | ticket 8 |

Edges: `A→T1`, `A→T2`, `A→T5`, `A→T6` (interface). T3, T4, T7 and T8 export
nothing, share no file with A, and so do not wait for it: all nine nodes are
ready at the start or right after A. No two tickets share a file, there is no
integration node, and the critical path is two nodes. `final_checks` runs the
public tests on the merged result.

The contract is responsible only for the exports in `click/__init__.py`. It
still has to edit four more modules, because an export of a name that does
not exist yet breaks `import click` for every node that starts after it; the
stubs are the smallest change that keeps the package importable. This has a
cost that the run will show: A's own files (given to it in full) are about
128 KB, more than any ticket node's.

Size of what a node is given in full (its own module), for comparison with
the whole repository (about 440 KB of source and 510 KB of tests):

| A | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 128 KB | 46 KB | 35 KB | 27 KB | 26 KB | 21 KB | 21 KB | 10 KB | 12 KB |

Every node's prompt also contains the whole request (about 6 KB), and its
goal tells it which of the eight changes is its own.

### Validation

Both graphs pass `validate()` on the base repository with no errors, and
`derive_edges()` adds no edge to the hand-written graph. The single graph has
no warnings; the hand-written graph has only W2 warnings (a symbol that no
other node requires): nothing in the graph consumes what the tickets add,
`final_checks` and the hidden tests do.

## Expectations

Written on 2026-10-11, before any run of this task. This section is not
edited afterwards; results go in the next section.

- **Single agent**: it has to find eight unrelated places in a repository of
  about 30,000 lines, and everything it reads stays in its conversation.
  Expected: **clearly more input tokens** than the task graph, whose nodes
  each look at their own part only.
- **Task graph**: one thin contract that only handles the exports, then eight
  parallel nodes. Expected: **shorter execution time** than the single agent.
- **Planning** itself takes about 3 to 5 minutes and may cancel out the
  graph's advantage in execution time. **Cost and quality** are the main
  things this task compares.
- If the results do not match these expectations, they are recorded as they
  are and the task is not changed.

## Results

Not run yet.

## Running

```bash
python experiments/taskgraph/click_batch/make_repo.py /tmp/click-batch
G=experiments/taskgraph/click_batch/graphs
H=experiments/taskgraph/click_batch/hidden_tests
# single agent
python -m aqours_code.taskgraph run $G/single.json --repo /tmp/click-batch --workers 1 --worker-timeout 3600 --hidden-tests $H --out /tmp/cb-runs
# hand-written graph, sequential and parallel
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/click-batch --workers 1 --worker-timeout 3600 --hidden-tests $H --out /tmp/cb-runs
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/click-batch --workers 8 --worker-timeout 3600 --hidden-tests $H --out /tmp/cb-runs
# planner, compared with the hand-written graph
python experiments/taskgraph/planner_eval/run_eval.py --out /tmp/cb-planner --case click-batch=/tmp/click-batch
```

To check the material itself (no model is called):

```bash
python -m pytest -q tests/taskgraph/test_click_batch_task.py
```
