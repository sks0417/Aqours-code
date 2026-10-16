"""Section "`click.ConfigError`".

Each docstring quotes the sentence of request.md that the test checks.
"""
import click


def test_defined_in_exceptions_and_exported():
    """Add `ConfigError(message, path=None)` in `click/exceptions.py`,
    exported from `click`, a subclass of `ClickException` with exit code 2."""
    import click.exceptions

    assert click.exceptions.ConfigError is click.ConfigError
    assert issubclass(click.ConfigError, click.ClickException)
    assert click.ConfigError("bad value").exit_code == 2
    assert click.ConfigError("bad value", "app.toml").exit_code == 2


def test_path_attribute():
    """`path` is stored as the attribute `path` (a `str` or `None`)."""
    assert click.ConfigError("bad value").path is None
    assert click.ConfigError("bad value", "app.toml").path == "app.toml"
    assert click.ConfigError("bad value", path="conf/app.json").path == "conf/app.json"


def test_format_message():
    """`format_message()` returns `"{path}: {message}"` when `path` is set,
    and `message` otherwise."""
    assert click.ConfigError("bad value", path="app.toml").format_message() == (
        "app.toml: bad value"
    )
    assert click.ConfigError("bad value").format_message() == "bad value"


def test_command_failing_with_it_prints_error_and_exits_2(runner):
    """A command that fails with it prints `Error: {format_message()}` like
    other click exceptions and exits with code 2."""

    @click.command()
    @click.option("--path")
    def cli(path):
        raise click.ConfigError("bad value", path=path)

    result = runner.invoke(cli, ["--path", "app.toml"])
    assert result.exit_code == 2
    assert result.output == "Error: app.toml: bad value\n"
    result = runner.invoke(cli, [])
    assert result.exit_code == 2
    assert result.output == "Error: bad value\n"
