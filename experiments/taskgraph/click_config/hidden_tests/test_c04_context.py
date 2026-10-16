"""Section "Configuration in the context": ``Context.config``,
``Context.config_path`` and ``Context.lookup_config``.

Each docstring quotes the sentence of request.md that the test checks.
"""
import json

import click


def show_config():
    @click.command()
    @click.option("--port", type=int)
    @click.pass_context
    def cli(ctx, port):
        click.echo(f"config={ctx.config!r} path={ctx.config_path!r}")

    return cli


def test_context_stores_config_and_config_path():
    """`Context` takes a new keyword argument `config=None` ... It is stored
    as `Context.config`. / `Context` takes `config_path=None`, stored as
    `Context.config_path`"""
    command = click.Command("cli")
    ctx = click.Context(command, config={"port": 8080}, config_path="app.toml")
    assert ctx.config == {"port": 8080}
    assert ctx.config_path == "app.toml"
    ctx = click.Context(command)
    assert ctx.config is None
    assert ctx.config_path is None


def test_child_context_uses_the_parent_value_under_its_info_name():
    """When `config` is not given and the parent context has a
    configuration, a child context uses the parent's value under its
    `info_name` if that value is a mapping, and `None` otherwise"""
    group = click.Group("cli")
    sub = click.Command("sync")
    parent = click.Context(group, config={"sync": {"force": True}, "level": 3})
    assert click.Context(sub, parent=parent, info_name="sync").config == {"force": True}
    assert click.Context(sub, parent=parent, info_name="level").config is None
    assert click.Context(sub, parent=parent, info_name="other").config is None
    own = click.Context(sub, parent=parent, info_name="sync", config={"force": False})
    assert own.config == {"force": False}
    assert click.Context(sub, parent=click.Context(group), info_name="sync").config is None


def test_child_context_uses_the_parent_config_path():
    """A child context without its own `config_path` uses its parent's."""
    group = click.Group("cli")
    sub = click.Command("sync")
    parent = click.Context(group, config={"sync": {}}, config_path="app.toml")
    assert click.Context(sub, parent=parent, info_name="sync").config_path == "app.toml"
    own = click.Context(sub, parent=parent, info_name="sync", config_path="sync.toml")
    assert own.config_path == "sync.toml"


def test_main_gives_a_configuration_without_a_file():
    """Because `Command.main` passes extra keyword arguments to the context,
    `cli.main(config={...})` ... give[s] a command a configuration without a
    file."""
    seen = {}

    @click.command()
    @click.option("--port", type=int)
    @click.pass_context
    def cli(ctx, port):
        seen["config"], seen["path"] = ctx.config, ctx.config_path

    cli.main([], prog_name="cli", standalone_mode=False, config={"port": 8080})
    assert seen == {"config": {"port": 8080}, "path": None}


def test_cli_runner_gives_a_configuration_without_a_file(runner):
    """`CliRunner.invoke(cli, args, config={...})` give[s] a command a
    configuration without a file."""
    result = runner.invoke(show_config(), [], config={"port": 8080})
    assert result.exit_code == 0, result.output
    assert result.output == "config={'port': 8080} path=None\n"


def test_lookup_config_plain_and_dotted_keys():
    """`Context.lookup_config(key)` returns the configured value for `key`
    at this level, or `None` when there is none. `key` may be dotted
    (`"server.port"` looks up `config["server"]["port"]`)."""
    config = {"port": 8080, "server": {"port": 9000, "tls": {"enabled": False}}}
    ctx = click.Context(click.Command("cli"), config=config)
    assert ctx.lookup_config("port") == 8080
    assert ctx.lookup_config("server.port") == 9000
    assert ctx.lookup_config("server.tls.enabled") is False
    assert ctx.lookup_config("server") == config["server"]
    assert ctx.lookup_config("host") is None
    assert ctx.lookup_config("server.host") is None
    assert ctx.lookup_config("missing.port") is None
    assert click.Context(click.Command("cli")).lookup_config("port") is None


def test_lookup_config_treats_json_null_as_no_value(tmp_path):
    """A `null` value in a JSON file counts as no value."""
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"port": None, "server": {"host": None}}), encoding="utf-8")
    ctx = click.Context(click.Command("cli"), config=click.load_config(path))
    assert ctx.lookup_config("port") is None
    assert ctx.lookup_config("server.host") is None
