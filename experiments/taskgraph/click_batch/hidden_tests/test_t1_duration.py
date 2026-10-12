"""Ticket 1: the ``click.Duration`` parameter type.

Each docstring quotes the sentence of request.md that the test checks.
"""
from datetime import timedelta

import click
import pytest


def convert(value, **bounds):
    return click.Duration(**bounds).convert(value, None, None)


def failure(value, **bounds):
    """The message of the usage error that converting ``value`` raises."""
    with pytest.raises(click.UsageError) as excinfo:
        convert(value, **bounds)
    return excinfo.value.format_message()


def make_command(**bounds):
    @click.command()
    @click.option("--timeout", type=click.Duration(**bounds))
    def cli(timeout):
        click.echo(f"seconds={timeout.total_seconds()}")

    return cli


def test_defined_in_types_and_exported():
    """Add a parameter type class `Duration` in `click/types.py`, exported from `click`."""
    import click.types

    assert click.types.Duration is click.Duration
    assert isinstance(click.Duration(), click.ParamType)


def test_plain_number_means_seconds():
    """A plain non-negative number means seconds: "90", "1.5"."""
    assert convert("90") == timedelta(seconds=90)
    assert convert("1.5") == timedelta(seconds=1.5)


def test_unit_parts():
    """Otherwise the value is one or more `<number><unit>` parts with units
    `d`, `h`, `m`, `s`: "45s", "2d", "1h30m", "1d2h3m4s", "1.5h"."""
    expected = {
        "45s": timedelta(seconds=45),
        "2d": timedelta(days=2),
        "1h30m": timedelta(hours=1, minutes=30),
        "1d2h3m4s": timedelta(days=1, hours=2, minutes=3, seconds=4),
        "1.5h": timedelta(hours=1.5),
    }
    for value, duration in expected.items():
        assert convert(value) == duration, value


def test_timedelta_is_returned_unchanged():
    """A `datetime.timedelta` value is returned unchanged."""
    value = timedelta(minutes=5, seconds=1)
    assert convert(value) == value


def test_other_values_fail_as_invalid():
    """units ... (lowercase), in this order, each unit at most once, without
    spaces. / Any other value fails. / The message for an invalid value
    contains `is not a valid duration`."""
    for value in ("abc", "1m1h", "1h1h", "1h 30m", "1H", "5x", "-5", ""):
        assert "is not a valid duration" in failure(value), value


def test_command_reports_invalid_value_as_usage_error(runner):
    """Failures use the parameter type's normal failure mechanism, so a
    command reports them as a usage error (exit code 2)."""
    result = runner.invoke(make_command(), ["--timeout", "soon"])
    assert result.exit_code == 2
    assert "is not a valid duration" in result.output


def test_command_receives_timedelta(runner):
    """... a parameter type class `Duration` ... that converts a duration to
    `datetime.timedelta`."""
    result = runner.invoke(make_command(), ["--timeout", "1h30m"])
    assert result.exit_code == 0, result.output
    assert result.output == "seconds=5400.0\n"


def test_bounds_are_inclusive():
    """`Duration(min=None, max=None)` takes optional `timedelta` bounds (inclusive)."""
    bounds = {"min": timedelta(minutes=1), "max": timedelta(hours=1)}
    assert convert("1m", **bounds) == timedelta(minutes=1)
    assert convert("1h", **bounds) == timedelta(hours=1)
    assert convert("90", **bounds) == timedelta(seconds=90)
    assert convert("2d", min=timedelta(minutes=1)) == timedelta(days=2)
    assert convert("0", max=timedelta(hours=1)) == timedelta(0)


def test_value_outside_bounds_fails(runner):
    """A value outside the bounds fails. / the message for a value outside the
    bounds contains `is out of range`."""
    bounds = {"min": timedelta(minutes=1), "max": timedelta(hours=1)}
    assert "is out of range" in failure("59s", **bounds)
    assert "is out of range" in failure("2h", **bounds)
    result = runner.invoke(make_command(**bounds), ["--timeout", "2h"])
    assert result.exit_code == 2
    assert "is out of range" in result.output


def test_name_metavar_and_info_dict(runner):
    """The type's `name` is "duration" and its metavar in help output is
    `DURATION`. `to_info_dict()` includes "min" and "max", each `None` or the
    bound in seconds as a float."""
    assert click.Duration().name == "duration"
    result = runner.invoke(make_command(), ["--help"])
    assert result.exit_code == 0
    assert "DURATION" in result.output
    info = click.Duration().to_info_dict()
    assert info["min"] is None and info["max"] is None
    info = click.Duration(min=timedelta(minutes=1), max=timedelta(hours=1)).to_info_dict()
    assert (info["min"], info["max"]) == (60.0, 3600.0)
    assert isinstance(info["min"], float) and isinstance(info["max"], float)
