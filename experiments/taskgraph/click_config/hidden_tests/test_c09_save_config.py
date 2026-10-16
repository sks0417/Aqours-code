"""Section "`click.save_config_option`".

Each docstring quotes the sentence of request.md that the test checks. The
saved file is read back with ``click.load_config``.
"""
import datetime
import pathlib

import click
import pytest


def one_line(text):
    return " ".join(text.split())


@pytest.fixture
def server():
    @click.command()
    @click.save_config_option()
    @click.option("--name", default="app")
    @click.option("--port", type=int, default=80, envvar="APP_PORT")
    @click.option("--workers", type=int, default=1)
    @click.option("--level", default="info")
    @click.option("--mode", default="fast")
    def cli(name, port, workers, level, mode):
        click.echo("callback ran")

    return cli


def test_defined_in_decorators_and_exported():
    """Add a decorator `save_config_option(*param_decls, **kwargs)` in
    `click/decorators.py`, exported from `click`."""
    import click.decorators

    assert click.decorators.save_config_option is click.save_config_option


def test_adds_a_save_config_option(runner, tmp_path):
    """It adds an option, `--save-config` unless `param_decls` are given,
    with metavar `PATH`, help text `Save the current options to a
    configuration file and exit.`, and `expose_value=False`."""

    @click.command()
    @click.save_config_option()
    def cli():
        click.echo("callback ran")

    (option,) = [p for p in cli.params if "--save-config" in getattr(p, "opts", ())]
    assert option.opts == ["--save-config"]
    assert option.expose_value is False
    result = runner.invoke(cli, ["--help"])
    assert "--save-config PATH" in result.output
    assert "Save the current options to a configuration file and exit." in one_line(
        result.output)
    assert runner.invoke(cli, []).output == "callback ran\n"

    @click.command()
    @click.save_config_option("-w", "--write-config")
    @click.option("--name")
    def other(name):
        click.echo("callback ran")

    path = tmp_path / "out.toml"
    result = runner.invoke(other, ["-w", str(path), "--name", "x"])
    assert result.exit_code == 0, result.output
    assert click.load_config(path) == {"name": "x"}
    assert runner.invoke(other, ["--save-config", str(path)]).exit_code == 2


def test_writes_values_from_command_line_environment_and_config(runner, server, tmp_path):
    """every other parameter of the command whose value is exposed and whose
    source is `COMMANDLINE`, `ENVIRONMENT` or `CONFIG` is written under its
    configuration key ... with `dump_config`, to the given path / When the
    option is given, after all of the command's parameters have been
    processed"""
    path = tmp_path / "out.toml"
    result = runner.invoke(
        server, ["--save-config", str(path), "--name", "web"],
        env={"APP_PORT": "8080"}, config={"workers": 4}, default_map={"level": "debug"},
    )
    assert result.exit_code == 0, result.output
    assert click.load_config(path) == {"name": "web", "port": 8080, "workers": 4}


def test_dotted_keys_become_nested_tables(runner, tmp_path):
    """written under its configuration key (a dotted key becomes nested
    tables)"""

    @click.command()
    @click.save_config_option()
    @click.option("--host", config_key="server.host")
    @click.option("--port", type=int, config_key="server.port")
    @click.option("--cert", config_key="server.tls.cert")
    @click.option("--debug", is_flag=True)
    def cli(host, port, cert, debug):
        pass

    args = ["--host", "h", "--port", "81", "--cert", "c.pem", "--debug"]
    for name in ("out.toml", "out.json"):
        path = tmp_path / name
        result = runner.invoke(cli, [*args, "--save-config", str(path)])
        assert result.exit_code == 0, result.output
        assert click.load_config(path) == {
            "server": {"host": "h", "port": 81, "tls": {"cert": "c.pem"}},
            "debug": True,
        }


def test_processed_values_lists_and_strings(runner, tmp_path):
    """a value is written as the parameter's processed value, with tuples
    turned into lists; a value of any type other than `str`, `int`,
    `float`, `bool` or `list` is written as `str(value)`"""

    @click.command()
    @click.save_config_option()
    @click.option("--name", callback=lambda ctx, param, value: value.upper())
    @click.option("--ratio", type=float)
    @click.option("--tag", "tags", multiple=True)
    @click.option("--size", nargs=2, type=int)
    @click.option("--point", "points", nargs=2, type=int, multiple=True)
    @click.option("--dir", "directory", type=click.Path(path_type=pathlib.Path))
    @click.option("--when", type=click.DateTime(["%Y-%m-%d"]))
    def cli(**values):
        pass

    path = tmp_path / "out.toml"
    args = ["--name", "web", "--ratio", "2", "--tag", "a", "--tag", "b", "--size", "3", "4",
            "--point", "1", "2", "--point", "5", "6", "--dir", "data/files",
            "--when", "2026-01-02", "--save-config", str(path)]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert click.load_config(path) == {
        "name": "WEB",
        "ratio": 2.0,
        "tags": ["a", "b"],
        "size": [3, 4],
        "points": [[1, 2], [5, 6]],
        "directory": str(pathlib.Path("data/files")),
        "when": str(datetime.datetime(2026, 1, 2)),
    }


def test_config_save_and_help_options_are_never_written(runner, tmp_path):
    """The options added by `config_option`, `save_config_option` and
    `--help` are never written."""

    @click.command()
    @click.config_option()
    @click.save_config_option()
    @click.help_option("-h", "--help")
    @click.option("--name")
    def cli(name):
        pass

    source = tmp_path / "in.toml"
    source.write_text('name = "app"\n', encoding="utf-8")
    path = tmp_path / "out.json"
    result = runner.invoke(cli, ["--config", str(source), "--save-config", str(path)])
    assert result.exit_code == 0, result.output
    assert click.load_config(path) == {"name": "app"}


def test_prints_a_message_and_exits_without_the_callback(runner, server, tmp_path):
    """`Saved configuration to {path}` is printed, and the program exits
    with code 0 without running the command's callback. / When the option
    is given"""
    path = tmp_path / "out.toml"
    result = runner.invoke(server, ["--name", "web", "--save-config", str(path)])
    assert result.exit_code == 0, result.output
    assert f"Saved configuration to {path}" in result.output
    assert "callback ran" not in result.output
    result = runner.invoke(server, ["--name", "web"])
    assert result.exit_code == 0, result.output
    assert result.output == "callback ran\n"
