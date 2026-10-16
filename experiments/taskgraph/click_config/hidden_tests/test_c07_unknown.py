"""Section "Unknown keys".

Each docstring quotes the sentence of request.md that the test checks.
"""
import click
import pytest


@pytest.fixture
def server():
    @click.command()
    @click.option("--port", type=int)
    @click.option("--host", config_key="server.host")
    @click.option("--cert", config_key="server.tls.cert")
    @click.argument("src", required=False)
    def cli(port, host, cert, src):
        click.echo("ran")

    return cli


@pytest.fixture
def app():
    @click.group()
    @click.option("--debug", is_flag=True)
    def cli(debug):
        click.echo("group ran")

    @cli.command()
    @click.option("--force", is_flag=True)
    def sync(force):
        click.echo(f"sync force={force}")

    @cli.command()
    def clean():
        click.echo("clean ran")

    return cli


def test_keys_of_parameters_are_known(runner, server):
    """Every key must be one of: the configuration key of one of the
    command's parameters; the first part of a dotted configuration key of
    one of the parameters, with a mapping as value; the keys inside that
    mapping are checked the same way against the rest of the dotted keys"""
    config = {"port": 1, "src": "a.txt", "server": {"host": "h", "tls": {"cert": "c.pem"}}}
    result = runner.invoke(server, [], config=config)
    assert result.exit_code == 0, result.output
    assert result.output == "ran\n"
    result = runner.invoke(server, ["--port", "2"], config={"port": 1, "server": {}})
    assert result.exit_code == 0, result.output


def test_unknown_key_fails_with_config_error(runner, server):
    """Otherwise the command fails with `ConfigError` ... The message
    contains `unknown configuration key` and the unknown keys"""
    result = runner.invoke(server, [], config={"port": 1, "porrt": 2})
    assert result.exit_code == 2
    assert "Error: " in result.output
    assert "unknown configuration key" in result.output and "porrt" in result.output


def test_error_path_is_the_config_path(server):
    """Otherwise the command fails with `ConfigError`, with `path` set to
    `ctx.config_path`."""
    with pytest.raises(click.ConfigError) as excinfo:
        server.main([], prog_name="cli", standalone_mode=False,
                    config={"porrt": 2}, config_path="settings.toml")
    assert excinfo.value.path == "settings.toml"
    assert "unknown configuration key" in excinfo.value.format_message()
    with pytest.raises(click.ConfigError) as excinfo:
        server.main([], prog_name="cli", standalone_mode=False, config={"porrt": 2})
    assert excinfo.value.path is None


def test_unknown_keys_are_dotted_sorted_and_comma_separated(runner, server):
    """The message contains `unknown configuration key` and the unknown
    keys, written as dotted paths from the command's level (for example
    `server.hots`), in sorted order and separated by `, `."""
    config = {
        "zeta": 1,
        "port": 1,
        "server": {"host": "h", "hots": "x", "tls": {"cert": "c", "kye": "k"}},
        "alpha": 2,
    }
    result = runner.invoke(server, [], config=config)
    assert result.exit_code == 2
    assert "alpha, server.hots, server.tls.kye, zeta" in result.output


def test_first_part_of_a_dotted_key_needs_a_mapping(runner, server):
    """the first part of a dotted configuration key of one of the
    parameters, with a mapping as value"""
    result = runner.invoke(server, [], config={"port": 1, "server": "h"})
    assert result.exit_code == 2
    assert "unknown configuration key" in result.output
    assert "server" in result.output


def test_group_takes_subcommand_tables_checked_when_they_run(runner, app):
    """for a group, the name of one of its subcommands, with a mapping as
    value (checked when that subcommand runs). / written as dotted paths
    from the command's level"""
    config = {"debug": True, "sync": {"force": True}, "clean": {"anything": 1}}
    result = runner.invoke(app, ["sync"], config=config)
    assert result.exit_code == 0, result.output
    assert result.output == "group ran\nsync force=True\n"
    result = runner.invoke(app, ["sync"], config={"sync": {"force": True, "forse": 1}})
    assert result.exit_code == 2
    assert "unknown configuration key" in result.output and "forse" in result.output
    assert "sync.forse" not in result.output
    result = runner.invoke(app, ["sync"], config={"sync": 5})
    assert result.exit_code == 2
    assert "unknown configuration key" in result.output and "sync" in result.output


def test_ignore_unknown_config_skips_the_check(runner, server, app):
    """A context created with `ignore_unknown_config=True` (also through
    `context_settings`, and inherited by child contexts) skips this check."""
    result = runner.invoke(server, [], config={"porrt": 2}, ignore_unknown_config=True)
    assert result.exit_code == 0, result.output

    @click.command(context_settings={"ignore_unknown_config": True})
    @click.option("--port", type=int)
    def lenient(port):
        click.echo(f"port={port}")

    result = runner.invoke(lenient, [], config={"port": 1, "porrt": 2})
    assert result.exit_code == 0, result.output
    assert result.output == "port=1\n"
    config = {"bad": 1, "sync": {"forse": 1}}
    result = runner.invoke(app, ["sync"], config=config, ignore_unknown_config=True)
    assert result.exit_code == 0, result.output
    assert result.output == "group ran\nsync force=False\n"
