"""Section "Writing a configuration file": ``click.dump_config``.

Each docstring quotes the sentence of request.md that the test checks. The
TOML that click writes is mostly checked by reading it back; only the
format the request fixes is compared line by line.
"""
import datetime
import json
import math

import click
import pytest

DATA = {
    "name": "app",
    "quote": 'say "hi" \\ back\\slash',
    "controls": "line1\nline2\ttab\rcr\bbs\fff\x00nul\x1fus",
    "unicode": "café 東京 ✓",
    "count": 42,
    "negative": -7,
    "zero": 0,
    "big": 2**63 - 1,
    "ratio": 0.5,
    "small": 1e-07,
    "large": 1.5e300,
    "neg_float": -2.25,
    "inf": float("inf"),
    "ninf": float("-inf"),
    "yes": True,
    "no": False,
    "empty_list": [],
    "tags": ["a", "b"],
    "mixed": [1, "two", 3.0, False],
    "matrix": [[1, 2], [3, [4, 5]], []],
    "with space": 1,
    "dotted.key": 2,
    "ключ": 3,
    "server": {
        "host": "localhost",
        "port": 8080,
        "tls": {"enabled": True, "ciphers": ["x", "y"], "deep": {"level": 3}},
    },
    "empty_table": {},
    "after": "a value after the tables",
}


class Point:
    pass


def toml_lines(data, tmp_path):
    path = tmp_path / "out.toml"
    click.dump_config(data, path)
    return path.read_text(encoding="utf-8").rstrip("\n").split("\n")


def test_defined_in_config_module_and_exported():
    """Add `dump_config(data, path)` in `click/config.py`, exported from
    `click`"""
    import click.config

    assert click.config.dump_config is click.dump_config


def test_unsupported_suffix(tmp_path):
    """in the format given by the suffix as for `load_config` (any other
    suffix raises `ConfigError` with `unsupported configuration file
    type`)."""
    for name in ("out.yaml", "out", "out.txt"):
        with pytest.raises(click.ConfigError) as excinfo:
            click.dump_config({"a": 1}, tmp_path / name)
        assert "unsupported configuration file type" in excinfo.value.format_message()


def test_json_is_written_with_indent_2_and_a_final_newline(tmp_path):
    """JSON is written with `json.dump(data, f, indent=2,
    ensure_ascii=False)` followed by a newline. / which writes the mapping
    `data` to `path` as UTF-8"""
    data = {"name": "café", "tags": ["a", "b"], "server": {"port": 80, "on": True}}
    path = tmp_path / "out.json"
    click.dump_config(data, path)
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert text == json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def test_written_data_reads_back_equal(tmp_path):
    """For any data made of the allowed values whose keys contain no `-`,
    `load_config` of the written file returns data equal to the input."""
    for name in ("out.toml", "out.json", "OUT.TOML"):
        path = tmp_path / name
        click.dump_config(DATA, path)
        assert click.load_config(path) == DATA


def test_none_values_are_left_out(tmp_path):
    """A key whose value is `None` is left out."""
    data = {"a": 1, "b": None, "table": {"c": None, "d": 2}}
    for name in ("out.toml", "out.json"):
        path = tmp_path / name
        click.dump_config(data, path)
        assert click.load_config(path) == {"a": 1, "table": {"d": 2}}


def test_other_values_raise_with_the_type_name(tmp_path):
    """Any other value raises `ConfigError` with a message containing
    `cannot write` and the type's name."""
    cases = (
        ({"value": {1, 2}}, "set"),
        ({"value": b"raw"}, "bytes"),
        ({"table": {"when": datetime.date(2026, 1, 1)}}, "date"),
        ({"items": [1, Point()]}, "Point"),
    )
    for name in ("out.toml", "out.json"):
        for data, type_name in cases:
            with pytest.raises(click.ConfigError) as excinfo:
                click.dump_config(data, tmp_path / name)
            message = excinfo.value.format_message()
            assert "cannot write" in message and type_name in message


def test_toml_values_before_tables_in_order(tmp_path):
    """one `key = value` line for each key whose value is not a mapping, in
    the mapping's order, before any table; then each mapping value as a
    table: a blank line, the header `[name]` ..., and its own lines"""
    data = {"name": "app", "server": {"port": 80}, "debug": True, "level": 2}
    assert toml_lines(data, tmp_path) == [
        'name = "app"',
        "debug = true",
        "level = 2",
        "",
        "[server]",
        "port = 80",
    ]


def test_toml_nested_tables_follow_their_table(tmp_path):
    """then each mapping value as a table: a blank line, the header `[name]`
    (`[outer.inner]` for nested tables), and its own lines, followed by its
    own nested tables"""
    data = {
        "a": 1,
        "outer": {"x": 1, "inner": {"y": 2, "deepest": {"z": 3}}, "w": 4},
        "second": {"v": 5},
    }
    assert toml_lines(data, tmp_path) == [
        "a = 1",
        "",
        "[outer]",
        "x = 1",
        "w = 4",
        "",
        "[outer.inner]",
        "y = 2",
        "",
        "[outer.inner.deepest]",
        "z = 3",
        "",
        "[second]",
        "v = 5",
    ]


def test_toml_keys_are_bare_or_quoted(tmp_path):
    """keys made only of ASCII letters, digits, `_` and `-` are written
    bare; other keys are written as quoted strings"""
    data = {
        "Plain_key-9": 1,
        "with space": 2,
        "dotted.key": 3,
        'quote"key': 4,
        "my table": {"k": 5},
    }
    assert toml_lines(data, tmp_path) == [
        "Plain_key-9 = 1",
        '"with space" = 2',
        '"dotted.key" = 3',
        '"quote\\"key" = 4',
        "",
        '["my table"]',
        "k = 5",
    ]


def test_toml_string_escapes(tmp_path):
    """strings are written in double quotes, with `\\` `"` and control
    characters escaped (`\\n`, `\\t`, `\\r`, `\\b`, `\\f`, other control
    characters as `\\uXXXX`)"""
    value = 'q"b\\c\nn\tt\rr\bb\ff\x01x\x1fy'
    (line,) = toml_lines({"s": value}, tmp_path)
    expected = 's = "q\\"b\\\\c\\nn\\tt\\rr\\bb\\ff\\u0001x\\u001fy"'
    # The request does not say whether the hex digits are upper or lower case.
    assert line.lower() == expected.lower()


def test_toml_booleans_numbers_and_lists(tmp_path):
    """booleans as `true` / `false`; integers and floats as Python writes
    them, with `inf`, `-inf` and `nan` for the special floats; lists as
    `[a, b, c]`."""
    data = {
        "t": True,
        "f": False,
        "i": -42,
        "x": 1.5,
        "big": 1e100,
        "pinf": float("inf"),
        "ninf": float("-inf"),
        "nan": float("nan"),
        "l": [1, "a", [True, 2.5], []],
        "e": [],
    }
    assert toml_lines(data, tmp_path) == [
        "t = true",
        "f = false",
        "i = -42",
        "x = 1.5",
        "big = 1e+100",
        "pinf = inf",
        "ninf = -inf",
        "nan = nan",
        'l = [1, "a", [true, 2.5], []]',
        "e = []",
    ]
    assert math.isnan(click.load_config(tmp_path / "out.toml")["nan"])
