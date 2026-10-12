"""Ticket 8: hints on errors.

Each docstring quotes the sentence of request.md that the test checks.
"""
import io

import click


def shown(exc):
    """What ``exc.show()`` prints."""
    out = io.StringIO()
    exc.show(file=out)
    return out.getvalue()


def run_failing(runner, exc_factory):
    @click.command()
    def cli():
        raise exc_factory()

    return runner.invoke(cli)


def test_hint_attribute():
    """Give `click.ClickException` ... an optional keyword argument
    `hint=None` and an attribute `hint`."""
    assert click.ClickException("boom", hint="try again").hint == "try again"
    assert click.ClickException("boom").hint is None
    assert click.ClickException("boom", hint=None).hint is None


def test_show_prints_the_hint_as_final_line():
    """When `hint` is set, `show()` prints an extra final line `Hint: {hint}`
    after the usual error output."""
    exc = click.ClickException("boom", hint="try again")
    assert shown(exc) == "Error: boom\nHint: try again\n"


def test_show_without_hint_is_unchanged():
    """Without a hint, the output is unchanged."""
    assert shown(click.ClickException("boom", hint=None)) == "Error: boom\n"
    assert shown(click.UsageError("bad", hint=None)) == "Error: bad\n"


def test_usage_error_takes_a_hint():
    """(and so all its subclasses, including `UsageError`)"""
    exc = click.UsageError("bad", hint="see the docs")
    assert exc.hint == "see the docs"
    assert click.UsageError("bad").hint is None
    assert shown(exc) == "Error: bad\nHint: see the docs\n"


def test_bad_parameter_takes_a_hint():
    """(and so all its subclasses, including `UsageError`)"""
    exc = click.BadParameter("nope", hint="see the docs")
    assert exc.hint == "see the docs"
    assert shown(exc) == shown(click.BadParameter("nope", hint=None)) + "Hint: see the docs\n"


def test_command_shows_hint_of_click_exception(runner):
    """When `hint` is set, `show()` prints an extra final line `Hint: {hint}`
    after the usual error output."""
    result = run_failing(runner, lambda: click.ClickException("boom", hint="try again"))
    assert result.exit_code == 1
    assert result.output == "Error: boom\nHint: try again\n"


def test_command_shows_hint_after_usage_error_output(runner):
    """an extra final line `Hint: {hint}` after the usual error output."""
    plain = run_failing(runner, lambda: click.UsageError("bad", hint=None))
    hinted = run_failing(runner, lambda: click.UsageError("bad", hint="use --force"))
    assert plain.exit_code == hinted.exit_code == 2
    assert plain.output.startswith("Usage: ")
    assert plain.output.endswith("Error: bad\n")
    assert hinted.output == plain.output + "Hint: use --force\n"


def test_command_output_without_hint_is_unchanged(runner):
    """Without a hint, the output is unchanged."""
    result = run_failing(runner, lambda: click.ClickException("boom", hint=None))
    assert result.exit_code == 1
    assert result.output == "Error: boom\n"
    result = run_failing(runner, lambda: click.UsageError("bad", hint=None))
    assert result.output == "Usage: cli [OPTIONS]\nTry 'cli --help' for help.\n\nError: bad\n"
