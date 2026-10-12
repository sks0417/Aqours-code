"""Ticket 3: test helpers on ``click.testing.Result``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest


@click.command()
def hello():
    click.echo("hello")


@click.command()
@click.pass_context
def broken(ctx):
    click.echo("partial output")
    ctx.exit(3)


def test_output_lines_splits_output_into_lines(runner):
    """`Result.output_lines`: a property returning `output` split into lines,
    without line endings."""

    @click.command()
    def cli():
        click.echo("one")
        click.echo("two words")

    assert runner.invoke(cli).output_lines == ["one", "two words"]


def test_output_lines_keeps_an_empty_line(runner):
    """`Result.output_lines`: ... `output` split into lines, without line endings."""

    @click.command()
    def cli():
        click.echo("first")
        click.echo("")
        click.echo("last")

    assert runner.invoke(cli).output_lines == ["first", "", "last"]


def test_output_lines_without_final_line_ending(runner):
    """`Result.output_lines`: ... `output` split into lines, without line endings."""

    @click.command()
    def cli():
        click.echo("first\nlast", nl=False)

    assert runner.invoke(cli).output_lines == ["first", "last"]


def test_assert_success_returns_the_result(runner):
    """It returns the result itself."""
    result = runner.invoke(hello)
    assert result.assert_success() is result


def test_assert_success_raises_for_another_exit_code(runner):
    """`Result.assert_success(output=None)`: raises `AssertionError` if
    `exit_code` is not 0; the message contains the exit code and the output."""
    result = runner.invoke(broken)
    assert result.exit_code == 3
    with pytest.raises(AssertionError) as excinfo:
        result.assert_success()
    assert "3" in str(excinfo.value)
    assert "partial output" in str(excinfo.value)


def test_assert_success_accepts_equal_output(runner):
    """If `output` is given, it also raises `AssertionError` when `output` is
    not exactly equal to it. It returns the result itself."""
    result = runner.invoke(hello)
    assert result.assert_success(output="hello\n") is result


def test_assert_success_raises_for_different_output(runner):
    """If `output` is given, it also raises `AssertionError` when `output` is
    not exactly equal to it."""
    result = runner.invoke(hello)
    for expected in ("hello", "Hello\n", "hello\n\n", ""):
        with pytest.raises(AssertionError):
            result.assert_success(output=expected)


def test_assert_success_checks_exit_code_when_output_matches(runner):
    """raises `AssertionError` if `exit_code` is not 0"""
    result = runner.invoke(broken)
    with pytest.raises(AssertionError):
        result.assert_success(output=result.output)
