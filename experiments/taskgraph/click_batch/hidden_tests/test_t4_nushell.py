"""Ticket 4: Nushell completion.

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest
from click.shell_completion import CompletionItem


def _things(ctx, param, incomplete):
    return [CompletionItem("x1", help="first\nsecond\tthird"), CompletionItem("y2")]


@pytest.fixture
def cli():
    @click.group()
    def cli():
        pass

    @cli.command()
    def a():
        pass

    @cli.command(help="bee")
    @click.option("--name", help="Name.")
    def b(name):
        pass

    @cli.command()
    @click.argument("thing", shell_complete=_things)
    def c(thing):
        pass

    @cli.command()
    @click.argument("path", type=click.Path())
    def d(path):
        pass

    return cli


def complete(runner, cli, words, cword):
    """Lines printed for ``_MYTOOL_COMPLETE=nu_complete``."""
    result = runner.invoke(cli, prog_name="mytool", env={
        "_MYTOOL_COMPLETE": "nu_complete", "COMP_WORDS": words, "COMP_CWORD": str(cword)})
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_class_is_registered_as_nu():
    """Support Nushell as a completion shell named `nu`, through a class
    `NuComplete` in `click/shell_completion.py` registered in the same way as
    the existing shells"""
    from click.shell_completion import NuComplete, get_completion_class

    assert get_completion_class("nu") is NuComplete


def test_source_prints_a_script(runner, cli):
    """so that `_{PROG}_COMPLETE=nu_source` prints a source script / The
    source script contains the program name and the string `nu_complete`."""
    result = runner.invoke(cli, prog_name="mytool", env={"_MYTOOL_COMPLETE": "nu_source"})
    assert result.exit_code == 0, result.output
    assert "mytool" in result.output
    assert "nu_complete" in result.output


def test_complete_prints_value_and_help(runner, cli):
    """`_{PROG}_COMPLETE=nu_complete` prints completions. / Each completion is
    printed on its own line as `{value}\\t{help}`, or just `{value}` when there
    is no help."""
    assert complete(runner, cli, "mytool ", 1) == ["a", "b\tbee", "c", "d"]


def test_complete_uses_the_word_at_comp_cword(runner, cli):
    """Completion input is read like the PowerShell completer: environment
    variable `COMP_WORDS` (the command line) and `COMP_CWORD` (the index of
    the word being completed)."""
    assert complete(runner, cli, "mytool b", 1) == ["b\tbee"]
    assert complete(runner, cli, "mytool a", 1) == ["a"]
    assert complete(runner, cli, "mytool b extra", 1) == ["b\tbee"]


def test_complete_inside_a_subcommand(runner, cli):
    """`COMP_WORDS` (the command line) and `COMP_CWORD` (the index of the word
    being completed)."""
    assert complete(runner, cli, "mytool b --na", 2) == ["--name\tName."]


def test_newlines_and_tabs_in_help_become_spaces(runner, cli):
    """Newlines and tabs in the help text are replaced by spaces."""
    assert complete(runner, cli, "mytool c ", 2) == ["x1\tfirst second third", "y2"]


def test_only_value_and_help_are_printed(runner, cli):
    """Each completion is printed on its own line as `{value}\\t{help}`, or
    just `{value}` when there is no help."""
    assert complete(runner, cli, "mytool d ab", 2) == ["ab"]
