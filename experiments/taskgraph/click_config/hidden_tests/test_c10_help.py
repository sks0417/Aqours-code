"""Section "Help output".

Each docstring quotes the sentence of request.md that the test checks.
"""
import click


def test_configured_value_replaces_the_default(runner):
    """In `--help`, an option that has a configured value in the context
    shows that value instead of its default, as `[default: {value}
    (config)]`"""

    @click.command()
    @click.option("--port", type=int, default=8000, show_default=True)
    def cli(port):
        pass

    result = runner.invoke(cli, ["--help"])
    assert "[default: 8000]" in result.output
    result = runner.invoke(cli, ["--help"], config={"port": 9000})
    assert result.exit_code == 0, result.output
    assert "[default: 9000 (config)]" in result.output
    assert "8000" not in result.output


def test_shown_even_when_show_default_is_not_set(runner):
    """shows that value instead of its default, as `[default: {value}
    (config)]`, even when `show_default` is not set."""

    @click.command()
    @click.option("--name", default="x")
    @click.option("--host", config_key="server.host")
    @click.option("--level")
    def cli(name, host, level):
        pass

    config = {"name": "app", "server": {"host": "example.org"}}
    result = runner.invoke(cli, ["--help"], config=config)
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "[default: app (config)]" in lines["--name"]
    assert "[default: example.org (config)]" in lines["--host"]
    assert "default:" not in lines["--level"]


def test_lists_are_joined_like_defaults(runner):
    """The value is formatted the way defaults are (lists joined with
    `, `)."""

    @click.command()
    @click.option("--tag", "tags", multiple=True)
    def cli(tags):
        pass

    result = runner.invoke(cli, ["--help"], config={"tags": ["a", "b", "c"]})
    assert result.exit_code == 0, result.output
    assert "[default: a, b, c (config)]" in result.output


def test_config_file_then_help_shows_the_file_values(runner, tmp_path):
    """Because `--config` is eager, `--config FILE --help` shows the values
    from `FILE`."""
    path = tmp_path / "app.toml"
    path.write_text('port = 9000\nname = "app"\n', encoding="utf-8")

    @click.command()
    @click.config_option()
    @click.option("--port", type=int, default=8000, show_default=True)
    @click.option("--name")
    def cli(port, name):
        pass

    result = runner.invoke(cli, ["--config", str(path), "--help"])
    assert result.exit_code == 0, result.output
    assert "[default: 9000 (config)]" in result.output
    assert "[default: app (config)]" in result.output
