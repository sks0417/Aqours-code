"""Section "Reading a configuration file": ``click.load_config``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import json
import tomllib

import click
import pytest

TOML_TEXT = """\
name = "app"
port = 8080
ratio = 0.5
debug = true
tags = ["a", "b"]
when = 1979-05-27T07:32:00Z

[server]
host = "localhost"

[server.tls]
enabled = false
"""
JSON_TEXT = """\
{"name": "app", "port": 8080, "ratio": 0.5, "debug": true, "tags": ["a", "b"],
 "missing": null, "server": {"host": "localhost", "tls": {"enabled": false}}}
"""


def write(path, text):
    path.write_bytes(text.encode("utf-8"))
    return path


def test_defined_in_config_module_and_exported():
    """Add `load_config(path)` in a new module `click/config.py`, exported
    from `click`."""
    import click.config

    assert click.config.load_config is click.load_config


def test_reads_toml_and_json_by_suffix(tmp_path):
    """The format follows the file suffix ...: `.toml` is read with the
    standard library's `tomllib`, `.json` with `json`. / It returns a `dict`.
    / `path` is a `str` or `os.PathLike`."""
    toml_path = write(tmp_path / "app.toml", TOML_TEXT)
    json_path = write(tmp_path / "app.json", JSON_TEXT)
    for path, expected in ((toml_path, tomllib.loads(TOML_TEXT)),
                           (json_path, json.loads(JSON_TEXT))):
        for given in (path, str(path)):
            data = click.load_config(given)
            assert type(data) is dict
            assert data == expected


def test_suffix_ignores_case(tmp_path):
    """The format follows the file suffix, ignoring case"""
    toml_path = write(tmp_path / "APP.TOML", TOML_TEXT)
    json_path = write(tmp_path / "App.Json", JSON_TEXT)
    assert click.load_config(toml_path) == tomllib.loads(TOML_TEXT)
    assert click.load_config(json_path) == json.loads(JSON_TEXT)


def test_files_are_read_as_utf8(tmp_path):
    """Files are read as UTF-8."""
    text = "café 東京 ✓"
    toml_path = write(tmp_path / "app.toml", f'title = "{text}"\n')
    json_path = write(tmp_path / "app.json", json.dumps({"title": text}, ensure_ascii=False))
    assert click.load_config(toml_path) == {"title": text}
    assert click.load_config(json_path) == {"title": text}


def test_dashes_in_keys_become_underscores_at_every_level(tmp_path):
    """In every key, at every level of nesting, `-` is replaced by `_`
    (`max-workers` becomes `max_workers`). Values are not changed"""
    toml_path = write(tmp_path / "app.toml", """\
max-workers = 4
mode = "fast-path"

[log-settings]
file-name = "out-1.log"

[log-settings.rotate-by]
max-size = 10
""")
    assert click.load_config(toml_path) == {
        "max_workers": 4,
        "mode": "fast-path",
        "log_settings": {"file_name": "out-1.log", "rotate_by": {"max_size": 10}},
    }
    json_path = write(tmp_path / "app.json", json.dumps(
        {"max-workers": 4, "outer-table": {"inner-key": "a-b", "deep-er": {"x-y": [1]}}}))
    assert click.load_config(json_path) == {
        "max_workers": 4,
        "outer_table": {"inner_key": "a-b", "deep_er": {"x_y": [1]}},
    }


def test_values_are_not_changed(tmp_path):
    """Values are not changed: lists stay lists, nested tables stay nested
    `dict`s."""
    data = click.load_config(write(tmp_path / "app.toml", TOML_TEXT))
    assert data["tags"] == ["a", "b"] and type(data["tags"]) is list
    assert type(data["server"]) is dict and type(data["server"]["tls"]) is dict
    assert data["server"]["tls"]["enabled"] is False
    assert data["when"] == tomllib.loads(TOML_TEXT)["when"]
    data = click.load_config(write(tmp_path / "app.json", JSON_TEXT))
    assert type(data["tags"]) is list and type(data["server"]) is dict
    assert data["missing"] is None
    assert data["ratio"] == 0.5 and data["port"] == 8080 and data["debug"] is True


def test_unsupported_suffix(tmp_path):
    """It raises `click.ConfigError` (below), with `path` set to `str(path)`,
    when: the suffix is neither `.toml` nor `.json`: the message contains
    `unsupported configuration file type`"""
    for name in ("app.yaml", "app.ini", "app", "app.toml.bak"):
        path = write(tmp_path / name, "port = 1\n")
        with pytest.raises(click.ConfigError) as excinfo:
            click.load_config(path)
        assert excinfo.value.path == str(path)
        assert "unsupported configuration file type" in excinfo.value.format_message()


def test_missing_or_unreadable_file(tmp_path):
    """It raises `click.ConfigError` ..., with `path` set to `str(path)`,
    when: ... the file does not exist or cannot be read: the message
    contains `cannot read`"""
    (tmp_path / "folder.toml").mkdir()
    for path in (tmp_path / "missing.toml", tmp_path / "missing.json",
                 tmp_path / "folder.toml"):
        with pytest.raises(click.ConfigError) as excinfo:
            click.load_config(path)
        assert excinfo.value.path == str(path)
        assert "cannot read" in excinfo.value.format_message()


def test_invalid_content(tmp_path):
    """It raises `click.ConfigError` ..., with `path` set to `str(path)`,
    when: ... the content is not valid TOML or JSON: the message contains
    `invalid TOML` or `invalid JSON`"""
    cases = (
        ("bad.toml", "port = \n", "invalid TOML"),
        ("dup.toml", "port = 1\nport = 2\n", "invalid TOML"),
        ("bad.json", "{port: 1}", "invalid JSON"),
        ("cut.json", '{"port": 1', "invalid JSON"),
    )
    for name, text, expected in cases:
        path = write(tmp_path / name, text)
        with pytest.raises(click.ConfigError) as excinfo:
            click.load_config(path)
        assert excinfo.value.path == str(path)
        assert expected in excinfo.value.format_message()


def test_json_top_level_must_be_an_object(tmp_path):
    """It raises `click.ConfigError` ..., with `path` set to `str(path)`,
    when: ... the top level of a JSON file is not an object: the message
    contains `must contain a table`."""
    for index, text in enumerate(("[1, 2]", '"text"', "3", "null")):
        path = write(tmp_path / f"top{index}.json", text)
        with pytest.raises(click.ConfigError) as excinfo:
            click.load_config(path)
        assert excinfo.value.path == str(path)
        assert "must contain a table" in excinfo.value.format_message()
