"""Ticket 5: ``click.mutually_exclusive``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest


@pytest.fixture
def export():
    @click.command()
    @click.option("--json", is_flag=True)
    @click.option("--csv", is_flag=True)
    @click.option("--format", default="text", envvar="EXPORT_FORMAT")
    @click.mutually_exclusive("json", "csv", "format")
    def export(json, csv, format):
        click.echo(f"json={json} csv={csv} format={format}")

    return export


def test_defined_in_decorators_and_exported():
    """Add a decorator `mutually_exclusive(*names)` in `click/decorators.py`,
    exported from `click`."""
    import click.decorators

    assert click.decorators.mutually_exclusive is click.mutually_exclusive


def test_one_given_parameter_runs_the_command(runner, export):
    """When the command runs and more than one of the named parameters was
    given ... the command fails (one given parameter is not more than one)."""
    result = runner.invoke(export, ["--json"])
    assert result.exit_code == 0, result.output
    assert result.output == "json=True csv=False format=text\n"


def test_defaults_do_not_count(runner, export):
    """given on the command line or through an environment variable (not just
    left at its default)"""
    result = runner.invoke(export, [])
    assert result.exit_code == 0, result.output
    assert result.output == "json=False csv=False format=text\n"
    result = runner.invoke(export, ["--csv"])
    assert result.exit_code == 0, result.output


def test_two_given_parameters_fail_with_usage_error(runner, export):
    """the command fails with a usage error (exit code 2) whose message
    contains every conflicting option as it appears in help (for example
    `--json` and `--csv`) and the words `cannot be used together`."""
    result = runner.invoke(export, ["--json", "--csv"])
    assert result.exit_code == 2
    assert "--json" in result.output and "--csv" in result.output
    assert "cannot be used together" in result.output
    assert "json=" not in result.output


def test_value_given_on_the_command_line_counts_even_if_equal_to_default(runner, export):
    """more than one of the named parameters was given on the command line
    ... (not just left at its default)"""
    result = runner.invoke(export, ["--csv", "--format", "text"])
    assert result.exit_code == 2
    assert "--csv" in result.output and "--format" in result.output
    assert "cannot be used together" in result.output


def test_environment_variable_counts(runner, export):
    """given on the command line or through an environment variable"""
    result = runner.invoke(export, ["--json"], env={"EXPORT_FORMAT": "xml"})
    assert result.exit_code == 2
    assert "--json" in result.output and "--format" in result.output
    assert "cannot be used together" in result.output


def test_message_names_every_conflicting_option(runner, export):
    """whose message contains every conflicting option as it appears in help"""
    result = runner.invoke(export, ["--json", "--csv", "--format", "xml"])
    assert result.exit_code == 2
    for option in ("--json", "--csv", "--format"):
        assert option in result.output


def test_fewer_than_two_names_raise_value_error_on_invoke(runner):
    """Fewer than two names ... raises `ValueError` when the command is invoked."""

    @click.command()
    @click.option("--json", is_flag=True)
    @click.mutually_exclusive("json")
    def cli(json):
        click.echo("ran")

    result = runner.invoke(cli, [])
    assert isinstance(result.exception, ValueError)
    assert "ran" not in result.output


def test_unknown_name_raises_value_error_on_invoke(runner):
    """a name that is not a parameter of the command, raises `ValueError`
    when the command is invoked."""

    @click.command()
    @click.option("--json", is_flag=True)
    @click.option("--csv", is_flag=True)
    @click.mutually_exclusive("json", "xml")
    def cli(json, csv):
        click.echo("ran")

    result = runner.invoke(cli, [])
    assert isinstance(result.exception, ValueError)
    assert "ran" not in result.output
