"""Section "Values from the configuration", and the order of the sources at
the top of request.md.

Each docstring quotes the sentence of request.md that the test checks.
"""
import json
import os

import click
import pytest
from click import ParameterSource


def report(ctx, **values):
    sources = {name: ctx.get_parameter_source(name).name for name in values}
    click.echo(" ".join(f"{name}={value!r}/{sources[name]}" for name, value in values.items()))


@pytest.fixture
def server():
    @click.command()
    @click.option("--port", type=int, default=3, envvar="APP_PORT")
    @click.option("--host", default="default-host")
    @click.pass_context
    def cli(ctx, port, host):
        report(ctx, port=port, host=host)

    return cli


def test_config_source_is_between_environment_and_default_map():
    """Add `ParameterSource.CONFIG` ... Members stay ordered from most to
    least explicit: `ENVIRONMENT < CONFIG < DEFAULT_MAP`."""
    assert ParameterSource.ENVIRONMENT < ParameterSource.CONFIG < ParameterSource.DEFAULT_MAP
    assert ParameterSource.COMMANDLINE < ParameterSource.CONFIG < ParameterSource.DEFAULT


def test_configured_value_is_used_with_source_config(runner, server):
    """A parameter that gets no value from the command line or its
    environment variable uses the value under its configuration key, and
    `ctx.get_parameter_source(name)` returns `ParameterSource.CONFIG`."""
    result = runner.invoke(server, [], config={"port": 9000, "host": "example.org"})
    assert result.exit_code == 0, result.output
    assert result.output == "port=9000/CONFIG host='example.org'/CONFIG\n"


def test_command_line_and_environment_come_first(runner, server):
    """The value of a parameter is taken from the first of these that
    provides one: 1. the command line, 2. the parameter's environment
    variable, 3. the configuration (new)"""
    config = {"port": 9000, "host": "example.org"}
    result = runner.invoke(server, ["--port", "1"], config=config, env={"APP_PORT": "2"})
    assert result.exit_code == 0, result.output
    assert result.output == "port=1/COMMANDLINE host='example.org'/CONFIG\n"
    result = runner.invoke(server, [], config=config, env={"APP_PORT": "2"})
    assert result.exit_code == 0, result.output
    assert result.output == "port=2/ENVIRONMENT host='example.org'/CONFIG\n"


def test_configuration_comes_before_default_map_and_default(runner, server, tmp_path):
    """3. the configuration (new), 4. `Context.default_map`, 5. the
    parameter's default. / A `null` value in a JSON file counts as no
    value."""
    default_map = {"port": 4, "host": "map-host"}
    result = runner.invoke(server, [], config={"port": 9000}, default_map=default_map)
    assert result.exit_code == 0, result.output
    assert result.output == "port=9000/CONFIG host='map-host'/DEFAULT_MAP\n"
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"port": None, "host": None}), encoding="utf-8")
    result = runner.invoke(server, [], config=click.load_config(path), default_map=default_map)
    assert result.exit_code == 0, result.output
    assert result.output == "port=4/DEFAULT_MAP host='map-host'/DEFAULT_MAP\n"
    result = runner.invoke(server, [], config=click.load_config(path))
    assert result.exit_code == 0, result.output
    assert result.output == "port=3/DEFAULT host='default-host'/DEFAULT\n"


def test_values_are_converted_by_the_type(runner):
    """Configured values are converted and validated by the parameter's type
    like any other value"""

    @click.command()
    @click.option("--port", type=int)
    @click.option("--ratio", type=float)
    @click.option("--mode", type=click.Choice(["fast", "safe"]))
    @click.option("--on", type=bool)
    def cli(port, ratio, mode, on):
        click.echo(f"{port!r} {ratio!r} {mode!r} {on!r}")

    config = {"port": "8080", "ratio": 2, "mode": "safe", "on": "yes"}
    result = runner.invoke(cli, [], config=config)
    assert result.exit_code == 0, result.output
    assert result.output == "8080 2.0 'safe' True\n"


def test_invalid_value_is_a_usage_error(runner):
    """an invalid value is reported as a usage error (exit code 2), as for a
    bad command line value."""

    @click.command()
    @click.option("--port", type=int)
    @click.option("--workers", type=click.IntRange(1, 8))
    def cli(port, workers):
        click.echo("callback ran")

    for config in ({"port": "abc"}, {"workers": 20}):
        result = runner.invoke(cli, [], config=config)
        assert result.exit_code == 2, result.output
        assert "Usage:" in result.output and "Error: Invalid value" in result.output
        assert "callback ran" not in result.output
    result = runner.invoke(cli, [], config={"port": "abc"})
    assert "'abc' is not a valid integer" in result.output


def test_lists_for_multiple_and_nargs(runner):
    """For a parameter with `multiple=True` or `nargs != 1`, a list is the
    sequence of values (a list of lists for `multiple=True` with
    `nargs > 1`)"""

    @click.command()
    @click.option("--tag", "tags", multiple=True)
    @click.option("--size", nargs=2, type=int)
    @click.option("--point", "points", nargs=2, type=int, multiple=True)
    @click.argument("files", nargs=-1)
    def cli(tags, size, points, files):
        click.echo(f"{tags!r} {size!r} {points!r} {files!r}")

    config = {"tags": ["a", "b"], "size": [3, 4], "points": [[1, 2], [5, 6]],
              "files": ["x.txt", "y.txt"]}
    result = runner.invoke(cli, [], config=config)
    assert result.exit_code == 0, result.output
    assert result.output == "('a', 'b') (3, 4) ((1, 2), (5, 6)) ('x.txt', 'y.txt')\n"


def test_strings_are_split_like_environment_variables(runner):
    """For a parameter with `multiple=True` or `nargs != 1`, ... a string is
    split as an environment variable value would be."""

    @click.command()
    @click.option("--tag", "tags", multiple=True, envvar="T_TAGS")
    @click.option("--size", nargs=2, type=int, envvar="T_SIZE")
    @click.option("--point", "points", nargs=2, type=int, multiple=True, envvar="T_POINTS")
    @click.option("--path", "paths", type=click.Path(), multiple=True, envvar="T_PATHS")
    def cli(tags, size, points, paths):
        click.echo(f"{tags!r} {size!r} {points!r} {paths!r}")

    values = {"tags": "a b  c", "size": "3 4", "points": "1 2 5 6",
              "paths": os.pathsep.join(["one", "two"])}
    from_env = runner.invoke(cli, [], env={f"T_{k.upper()}": v for k, v in values.items()})
    from_config = runner.invoke(cli, [], config=values)
    assert from_env.exit_code == 0, from_env.output
    assert from_config.exit_code == 0, from_config.output
    assert from_config.output == from_env.output
    assert from_config.output == "('a', 'b', 'c') (3, 4) ((1, 2), (5, 6)) ('one', 'two')\n"


def test_boolean_flags_and_count_options(runner):
    """Boolean flags take `true`/`false`; `count=True` options take an
    integer."""

    @click.command()
    @click.option("--shout/--no-shout", default=False)
    @click.option("--debug", is_flag=True)
    @click.option("--color", is_flag=True, default=True)
    @click.option("-v", "--verbose", count=True)
    def cli(shout, debug, color, verbose):
        click.echo(f"{shout} {debug} {color} {verbose}")

    config = {"shout": True, "debug": True, "color": False, "verbose": 3}
    result = runner.invoke(cli, [], config=config)
    assert result.exit_code == 0, result.output
    assert result.output == "True True False 3\n"


def test_prompt_is_skipped_when_configured(runner):
    """An option with `prompt` set does not prompt when the configuration
    provides its value."""

    @click.command()
    @click.option("--name", prompt="Your name")
    @click.option("--city", prompt="Your city")
    def cli(name, city):
        click.echo(f"name={name} city={city}")

    result = runner.invoke(cli, [], config={"name": "Ada"}, input="Paris\n")
    assert result.exit_code == 0, result.output
    assert "Your name" not in result.output
    assert "Your city" in result.output
    assert result.output.endswith("name=Ada city=Paris\n")
