# Batch of improvements for click

This repository is the click command-line library. Implement the eight
independent changes below. Each one is small; together they touch several
parts of the library. Keep every existing test passing and add tests for
the new behaviour. Public names listed as "exported from `click`" must be
importable as `click.<name>`.

## 1. `click.Duration` parameter type

Add a parameter type class `Duration` in `click/types.py`, exported from
`click`, that converts a duration to `datetime.timedelta`.

- A plain non-negative number means seconds: `"90"`, `"1.5"`.
- Otherwise the value is one or more `<number><unit>` parts with units
  `d`, `h`, `m`, `s` (lowercase), in this order, each unit at most once,
  without spaces: `"45s"`, `"2d"`, `"1h30m"`, `"1d2h3m4s"`, `"1.5h"`.
- A `datetime.timedelta` value is returned unchanged.
- `Duration(min=None, max=None)` takes optional `timedelta` bounds
  (inclusive). A value outside the bounds fails.
- Any other value fails. Failures use the parameter type's normal failure
  mechanism, so a command reports them as a usage error (exit code 2).
  The message for an invalid value contains `is not a valid duration`;
  the message for a value outside the bounds contains `is out of range`.
- The type's `name` is `"duration"` and its metavar in help output is
  `DURATION`. `to_info_dict()` includes `"min"` and `"max"`, each `None` or
  the bound in seconds as a float.

## 2. `click.select`

Add `select(text, options, default=None)` in `click/termui.py`, exported
from `click`, that lets the user pick one of several strings.

- It prints `text`, then each option on its own line as `  {n}) {option}`
  with `n` counting from 1, then prompts with `click.prompt` using the
  prompt text `Enter a number`. If `default` is given, the prompt's default
  is the default option's number.
- The answer may be a number from 1 to the number of options, or the exact
  text of an option. `select` returns the chosen option string.
- For any other answer it prints `Error: '{answer}' is not a valid choice.`
  and prompts again.
- An empty `options` list, or a `default` that is not one of the options,
  raises `ValueError`.

## 3. Test helpers on `click.testing.Result`

- `Result.output_lines`: a property returning `output` split into lines,
  without line endings.
- `Result.assert_success(output=None)`: raises `AssertionError` if
  `exit_code` is not 0; the message contains the exit code and the output.
  If `output` is given, it also raises `AssertionError` when `output` is
  not exactly equal to it. It returns the result itself.

## 4. Nushell completion

Support Nushell as a completion shell named `nu`, through a class
`NuComplete` in `click/shell_completion.py` registered in the same way as
the existing shells, so that `_{PROG}_COMPLETE=nu_source` prints a source
script and `_{PROG}_COMPLETE=nu_complete` prints completions.

- The source script contains the program name and the string
  `nu_complete`.
- Completion input is read like the PowerShell completer: environment
  variable `COMP_WORDS` (the command line) and `COMP_CWORD` (the index of
  the word being completed).
- Each completion is printed on its own line as `{value}\t{help}`, or just
  `{value}` when there is no help. Newlines and tabs in the help text are
  replaced by spaces.

## 5. `click.mutually_exclusive`

Add a decorator `mutually_exclusive(*names)` in `click/decorators.py`,
exported from `click`. It is applied to a command callback below the
`@click.command()` and parameter decorators:

    @click.command()
    @click.option("--json", is_flag=True)
    @click.option("--csv", is_flag=True)
    @click.mutually_exclusive("json", "csv")
    def export(json, csv): ...

When the command runs and more than one of the named parameters was given
on the command line or through an environment variable (not just left at
its default), the command fails with a usage error (exit code 2) whose
message contains every conflicting option as it appears in help (for
example `--json` and `--csv`) and the words `cannot be used together`.
Fewer than two names, or a name that is not a parameter of the command,
raises `ValueError` when the command is invoked.

## 6. `click.format_size`

Add `format_size(num_bytes, binary=False)` in `click/utils.py`, exported
from `click`, returning a human-readable size.

- With `binary=False` it uses powers of 1000 and units `B`, `kB`, `MB`,
  `GB`, `TB`, `PB`; with `binary=True` powers of 1024 and units `B`, `KiB`,
  `MiB`, `GiB`, `TiB`, `PiB`.
- Values below 1000 (or 1024) are printed as an integer: `"512 B"`.
  Larger values use the largest unit that keeps the number at least 1,
  with one decimal: `"1.5 kB"`, `"2.0 MiB"`. Beyond the last unit, the last
  unit is used.
- Negative values raise `ValueError`.

## 7. Bullet lists in `click.wrap_text`

Add a parameter `preserve_bullets=False` to `wrap_text` in
`click/formatting.py`. When it is true, a line starting with `- `, `* `,
or a number followed by `. ` (after any initial indent) starts a new
bullet item: the item is wrapped to the width on its own, and its
continuation lines are indented so that they line up with the text after
the bullet marker. Text that is not a bullet item is wrapped as before.
With the default `False`, the output is exactly as before.

## 8. Hints on errors

Give `click.ClickException` (and so all its subclasses, including
`UsageError`) an optional keyword argument `hint=None` and an attribute
`hint`. When `hint` is set, `show()` prints an extra final line
`Hint: {hint}` after the usual error output. Without a hint, the output is
unchanged.
