# Configuration files for click

This repository is the click command-line library. Add support for
configuration files, so that a command line program can keep its settings in
a TOML or JSON file, read them back as parameter values, and write the
current settings to a file. Keep every existing test passing and add tests
for the new behaviour. Public names listed as "exported from `click`" must be
importable as `click.<name>`.

The value of a parameter is taken from the first of these that provides one:

1. the command line,
2. the parameter's environment variable,
3. the configuration (new),
4. `Context.default_map`,
5. the parameter's default.

## Reading a configuration file

Add `load_config(path)` in a new module `click/config.py`, exported from
`click`. `path` is a `str` or `os.PathLike`.

- The format follows the file suffix, ignoring case: `.toml` is read with
  the standard library's `tomllib`, `.json` with `json`. Files are read as
  UTF-8.
- It returns a `dict`. In every key, at every level of nesting, `-` is
  replaced by `_` (`max-workers` becomes `max_workers`). Values are not
  changed: lists stay lists, nested tables stay nested `dict`s.
- It raises `click.ConfigError` (below), with `path` set to `str(path)`,
  when:
  - the suffix is neither `.toml` nor `.json`: the message contains
    `unsupported configuration file type`;
  - the file does not exist or cannot be read: the message contains
    `cannot read`;
  - the content is not valid TOML or JSON: the message contains
    `invalid TOML` or `invalid JSON`;
  - the top level of a JSON file is not an object: the message contains
    `must contain a table`.
- On Python older than 3.11, a `.toml` file raises `ConfigError` with a
  message containing `Python 3.11`.

## Writing a configuration file

Add `dump_config(data, path)` in `click/config.py`, exported from `click`,
which writes the mapping `data` to `path` as UTF-8, in the format given by
the suffix as for `load_config` (any other suffix raises `ConfigError` with
`unsupported configuration file type`).

- Values may be `str`, `int`, `float`, `bool`, lists of such values (lists
  may be nested), and mappings with `str` keys, nested to any depth. A key
  whose value is `None` is left out. Any other value raises `ConfigError`
  with a message containing `cannot write` and the type's name.
- JSON is written with `json.dump(data, f, indent=2, ensure_ascii=False)`
  followed by a newline.
- TOML is written by click itself (the standard library cannot write TOML):
  - one `key = value` line for each key whose value is not a mapping, in
    the mapping's order, before any table;
  - then each mapping value as a table: a blank line, the header
    `[name]` (`[outer.inner]` for nested tables), and its own lines,
    followed by its own nested tables;
  - keys made only of ASCII letters, digits, `_` and `-` are written bare;
    other keys are written as quoted strings;
  - strings are written in double quotes, with `\` `"` and control
    characters escaped (`\n`, `\t`, `\r`, `\b`, `\f`, other control
    characters as `\uXXXX`); booleans as `true` / `false`; integers and
    floats as Python writes them, with `inf`, `-inf` and `nan` for the
    special floats; lists as `[a, b, c]`.
- For any data made of the allowed values whose keys contain no `-`,
  `load_config` of the written file returns data equal to the input.

## `click.ConfigError`

Add `ConfigError(message, path=None)` in `click/exceptions.py`, exported
from `click`, a subclass of `ClickException` with exit code 2.

- `path` is stored as the attribute `path` (a `str` or `None`).
- `format_message()` returns `"{path}: {message}"` when `path` is set, and
  `message` otherwise. A command that fails with it prints
  `Error: {format_message()}` like other click exceptions and exits with
  code 2.

## Configuration in the context

- `Context` takes a new keyword argument `config=None`: a mapping of
  configuration keys to values for that command. A value that is itself a
  mapping, stored under the name of a subcommand, is the configuration of
  that subcommand. It is stored as `Context.config`.
- When `config` is not given and the parent context has a configuration,
  a child context uses the parent's value under its `info_name` if that
  value is a mapping, and `None` otherwise (the same way `default_map` is
  inherited).
- `Context` takes `config_path=None`, stored as `Context.config_path`, the
  file the configuration came from. A child context without its own
  `config_path` uses its parent's.
- Because `Command.main` passes extra keyword arguments to the context,
  `cli.main(config={...})` and `CliRunner.invoke(cli, args, config={...})`
  give a command a configuration without a file.
- `Context.lookup_config(key)` returns the configured value for `key` at
  this level, or `None` when there is none. `key` may be dotted
  (`"server.port"` looks up `config["server"]["port"]`). A `null` value in
  a JSON file counts as no value.

## Configuration keys of parameters

- `Parameter` (and so `Option` and `Argument`) takes a new keyword argument
  `config_key=None`. A parameter's configuration key is `config_key` when
  given, and its `name` otherwise. A dotted `config_key` reads a value from
  nested tables: `config_key="server.port"` reads
  `[server] port = 8080`. In `config_key`, `-` is replaced by `_` in every
  part, as in loaded files.
- `Parameter.to_info_dict()` includes `"config_key"`, the given value, when
  `config_key` is given.

## Values from the configuration

- Add `ParameterSource.CONFIG`, documented as "The value was provided by a
  configuration file or `Context.config`". Members stay ordered from most to
  least explicit: `ENVIRONMENT < CONFIG < DEFAULT_MAP`.
- A parameter that gets no value from the command line or its environment
  variable uses the value under its configuration key, and
  `ctx.get_parameter_source(name)` returns `ParameterSource.CONFIG`.
- Configured values are converted and validated by the parameter's type
  like any other value; an invalid value is reported as a usage error
  (exit code 2), as for a bad command line value.
- For a parameter with `multiple=True` or `nargs != 1`, a list is the
  sequence of values (a list of lists for `multiple=True` with `nargs > 1`);
  a string is split as an environment variable value would be.
- Boolean flags take `true`/`false`; `count=True` options take an integer.
- An option with `prompt` set does not prompt when the configuration
  provides its value.

## Unknown keys

After a command's parameters have been processed, the command checks its
configuration. Every key must be one of:

- the configuration key of one of the command's parameters;
- the first part of a dotted configuration key of one of the parameters,
  with a mapping as value; the keys inside that mapping are checked the
  same way against the rest of the dotted keys;
- for a group, the name of one of its subcommands, with a mapping as value
  (checked when that subcommand runs).

Otherwise the command fails with `ConfigError`, with `path` set to
`ctx.config_path`. The message contains `unknown configuration key` and the
unknown keys, written as dotted paths from the command's level (for example
`server.hots`), in sorted order and separated by `, `. A context created with
`ignore_unknown_config=True` (also through `context_settings`, and inherited
by child contexts) skips this check.

## `click.config_option`

Add a decorator `config_option(*param_decls, default_paths=(), **kwargs)` in
`click/decorators.py`, exported from `click`, in the style of the existing
`version_option` and `help_option`.

- It adds an eager option, `--config` unless `param_decls` are given, with
  metavar `PATH`, help text `Read option defaults from a configuration
  file.`, and `expose_value=False`. Other keyword arguments are passed to
  the option (for example `envvar`).
- When the option is given, the file is loaded with `load_config`, the
  result becomes `ctx.config`, and the path becomes `ctx.config_path`.
- When the option is not given, the first path in `default_paths` that
  exists as a file is loaded in the same way (`~` is expanded). When none
  exists, the context's configuration is left as it is.
- A file that cannot be loaded fails the command with the `ConfigError`
  from `load_config`.
- Because the option is eager, the configuration is in place before the
  command's other parameters are processed, and before the configuration of
  its subcommands is looked up.

## `click.save_config_option`

Add a decorator `save_config_option(*param_decls, **kwargs)` in
`click/decorators.py`, exported from `click`. It adds an option,
`--save-config` unless `param_decls` are given, with metavar `PATH`, help
text `Save the current options to a configuration file and exit.`, and
`expose_value=False`.

When the option is given, after all of the command's parameters have been
processed and before the command's callback runs:

- every other parameter of the command whose value is exposed and whose
  source is `COMMANDLINE`, `ENVIRONMENT` or `CONFIG` is written under its
  configuration key (a dotted key becomes nested tables) with
  `dump_config`, to the given path;
- a value is written as the parameter's processed value, with tuples
  turned into lists; a value of any type other than `str`, `int`, `float`,
  `bool` or `list` is written as `str(value)`;
- `Saved configuration to {path}` is printed, and the program exits with
  code 0 without running the command's callback.

The options added by `config_option`, `save_config_option` and `--help`
are never written.

## Help output

In `--help`, an option that has a configured value in the context shows
that value instead of its default, as `[default: {value} (config)]`, even
when `show_default` is not set. The value is formatted the way defaults are
(lists joined with `, `). Because `--config` is eager, `--config FILE
--help` shows the values from `FILE`.
