"""Ticket 6: ``click.format_size``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest


def test_defined_in_utils_and_exported():
    """Add `format_size(num_bytes, binary=False)` in `click/utils.py`,
    exported from `click`, returning a human-readable size."""
    import click.utils

    assert click.utils.format_size is click.format_size
    assert isinstance(click.format_size(1), str)


def test_values_below_1000_are_integer_bytes():
    """Values below 1000 (or 1024) are printed as an integer: "512 B"."""
    for num_bytes in (0, 1, 512, 999):
        assert click.format_size(num_bytes) == f"{num_bytes} B"


def test_binary_values_below_1024_are_integer_bytes():
    """Values below 1000 (or 1024) are printed as an integer"""
    for num_bytes in (0, 512, 1000, 1023):
        assert click.format_size(num_bytes, binary=True) == f"{num_bytes} B"


def test_decimal_units():
    """With `binary=False` it uses powers of 1000 and units `B`, `kB`, `MB`,
    `GB`, `TB`, `PB`"""
    expected = {
        1000: "1.0 kB",
        2 * 1000**2: "2.0 MB",
        3 * 1000**3: "3.0 GB",
        4 * 1000**4: "4.0 TB",
        5 * 1000**5: "5.0 PB",
    }
    for num_bytes, text in expected.items():
        assert click.format_size(num_bytes, binary=False) == text


def test_binary_units():
    """with `binary=True` powers of 1024 and units `B`, `KiB`, `MiB`, `GiB`,
    `TiB`, `PiB`."""
    expected = {
        1024: "1.0 KiB",
        2 * 1024**2: "2.0 MiB",
        3 * 1024**3: "3.0 GiB",
        4 * 1024**4: "4.0 TiB",
        5 * 1024**5: "5.0 PiB",
    }
    for num_bytes, text in expected.items():
        assert click.format_size(num_bytes, binary=True) == text


def test_binary_defaults_to_false():
    """`format_size(num_bytes, binary=False)`"""
    assert click.format_size(1024) == click.format_size(1024, binary=False) == "1.0 kB"


def test_largest_unit_that_keeps_the_number_at_least_one():
    """Larger values use the largest unit that keeps the number at least 1,
    with one decimal: "1.5 kB", "2.0 MiB"."""
    assert click.format_size(1500) == "1.5 kB"
    assert click.format_size(999_000) == "999.0 kB"
    assert click.format_size(1_000_000) == "1.0 MB"
    assert click.format_size(2_500_000_000) == "2.5 GB"
    assert click.format_size(1536, binary=True) == "1.5 KiB"
    assert click.format_size(1023 * 1024, binary=True) == "1023.0 KiB"
    assert click.format_size(1024**2, binary=True) == "1.0 MiB"


def test_beyond_the_last_unit_the_last_unit_is_used():
    """Beyond the last unit, the last unit is used."""
    assert click.format_size(3000 * 1000**5) == "3000.0 PB"
    assert click.format_size(2048 * 1024**5, binary=True) == "2048.0 PiB"


def test_negative_values_raise_value_error():
    """Negative values raise `ValueError`."""
    with pytest.raises(ValueError):
        click.format_size(-1)
    with pytest.raises(ValueError):
        click.format_size(-1024, binary=True)
