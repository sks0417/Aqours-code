"""Ticket 2: ``click.select``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest

FRUITS = ["apple", "banana", "cherry"]


def run_select(runner, answers, default=None):
    """Call ``select`` in a command with ``answers`` typed on stdin."""

    @click.command()
    def cli():
        choice = click.select("Pick a fruit", FRUITS, default=default)
        click.echo(f"chosen={choice}")

    return runner.invoke(cli, input=answers)


def test_defined_in_termui_and_exported():
    """Add `select(text, options, default=None)` in `click/termui.py`, exported from `click`."""
    import click.termui

    assert click.termui.select is click.select


def test_prints_text_then_numbered_options(runner):
    """It prints `text`, then each option on its own line as `  {n}) {option}`
    with `n` counting from 1"""
    result = run_select(runner, "1\n")
    assert result.output.splitlines()[:4] == [
        "Pick a fruit", "  1) apple", "  2) banana", "  3) cherry"]


def test_prompts_for_a_number_after_the_options(runner):
    """then prompts with `click.prompt` using the prompt text `Enter a number`."""
    result = run_select(runner, "1\n")
    assert "Enter a number" in result.output
    assert result.output.index("Enter a number") > result.output.index("  3) cherry")


def test_number_selects_option(runner):
    """The answer may be a number from 1 to the number of options ...
    `select` returns the chosen option string."""
    for answer, option in (("1", "apple"), ("2", "banana"), ("3", "cherry")):
        result = run_select(runner, f"{answer}\n")
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[-1] == f"chosen={option}"


def test_exact_text_selects_option(runner):
    """The answer may be ... the exact text of an option."""
    result = run_select(runner, "cherry\n")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "chosen=cherry"


def test_default_is_the_number_of_the_default_option(runner):
    """If `default` is given, the prompt's default is the default option's number."""
    result = run_select(runner, "\n", default="banana")
    assert result.exit_code == 0, result.output
    assert "Enter a number [2]" in result.output
    assert result.output.splitlines()[-1] == "chosen=banana"


def test_other_answers_print_an_error_and_prompt_again(runner):
    """For any other answer it prints `Error: '{answer}' is not a valid
    choice.` and prompts again."""
    result = run_select(runner, "0\n4\nplum\n2\n")
    assert result.exit_code == 0, result.output
    for answer in ("0", "4", "plum"):
        assert f"Error: '{answer}' is not a valid choice." in result.output
    assert result.output.count("Enter a number") == 4
    assert result.output.splitlines()[-1] == "chosen=banana"


def test_empty_options_raise_value_error():
    """An empty `options` list ... raises `ValueError`."""
    with pytest.raises(ValueError):
        click.select("Pick a fruit", [])


def test_default_that_is_not_an_option_raises_value_error():
    """a `default` that is not one of the options, raises `ValueError`."""
    with pytest.raises(ValueError):
        click.select("Pick a fruit", FRUITS, default="plum")
