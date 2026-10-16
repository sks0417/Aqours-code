"""Section "Configuration keys of parameters": ``config_key``.

Each docstring quotes the sentence of request.md that the test checks. That
the info dict of a parameter without ``config_key`` is unchanged is checked
by ``regression/test_info_dict.py``.
"""
import click


def test_config_key_when_given(runner):
    """`Parameter` (and so `Option` and `Argument`) takes a new keyword
    argument `config_key=None`. A parameter's configuration key is
    `config_key` when given"""

    @click.command()
    @click.option("--port", type=int, default=1, config_key="listen")
    def cli(port):
        click.echo(f"port={port}")

    result = runner.invoke(cli, [], config={"listen": 9000})
    assert result.exit_code == 0, result.output
    assert result.output == "port=9000\n"


def test_name_otherwise(runner):
    """A parameter's configuration key is `config_key` when given, and its
    `name` otherwise."""

    @click.command()
    @click.option("--max-workers", type=int, default=1)
    @click.option("-r", "--retries", "attempts", type=int, default=1)
    def cli(max_workers, attempts):
        click.echo(f"workers={max_workers} attempts={attempts}")

    result = runner.invoke(cli, [], config={"max_workers": 4, "attempts": 3})
    assert result.exit_code == 0, result.output
    assert result.output == "workers=4 attempts=3\n"


def test_dotted_config_key_reads_nested_tables(runner, tmp_path):
    """A dotted `config_key` reads a value from nested tables:
    `config_key="server.port"` reads `[server] port = 8080`."""
    path = tmp_path / "app.toml"
    path.write_text("[server]\nport = 8080\n\n[server.tls]\ncert = \"a.pem\"\n",
                    encoding="utf-8")

    @click.command()
    @click.option("--port", type=int, default=1, config_key="server.port")
    @click.option("--cert", config_key="server.tls.cert")
    def cli(port, cert):
        click.echo(f"port={port} cert={cert}")

    result = runner.invoke(cli, [], config=click.load_config(path))
    assert result.exit_code == 0, result.output
    assert result.output == "port=8080 cert=a.pem\n"


def test_dashes_in_config_key_become_underscores(runner, tmp_path):
    """In `config_key`, `-` is replaced by `_` in every part, as in loaded
    files."""
    path = tmp_path / "app.toml"
    path.write_text("[http-server]\nmax-conn = 5\n", encoding="utf-8")

    @click.command()
    @click.option("--conn", type=int, default=1, config_key="http-server.max-conn")
    def cli(conn):
        click.echo(f"conn={conn}")

    result = runner.invoke(cli, [], config=click.load_config(path))
    assert result.exit_code == 0, result.output
    assert result.output == "conn=5\n"
    result = runner.invoke(cli, [], config={"http_server": {"max_conn": 6}})
    assert result.exit_code == 0, result.output
    assert result.output == "conn=6\n"


def test_argument_takes_config_key(runner):
    """`Parameter` (and so `Option` and `Argument`) takes a new keyword
    argument `config_key=None`."""

    @click.command()
    @click.argument("src", config_key="paths.source")
    def cli(src):
        click.echo(f"src={src}")

    result = runner.invoke(cli, [], config={"paths": {"source": "in.txt"}})
    assert result.exit_code == 0, result.output
    assert result.output == "src=in.txt\n"
    result = runner.invoke(cli, ["other.txt"], config={"paths": {"source": "in.txt"}})
    assert result.output == "src=other.txt\n"


def test_info_dict_includes_the_given_config_key():
    """`Parameter.to_info_dict()` includes `"config_key"`, the given value,
    when `config_key` is given."""
    option = click.Option(["--port"], config_key="server.port")
    assert option.to_info_dict()["config_key"] == "server.port"
    argument = click.Argument(["src"], config_key="http-server.source")
    assert argument.to_info_dict()["config_key"] == "http-server.source"

    @click.command()
    @click.option("--name", config_key="app.name")
    def cli(name):
        pass

    with click.Context(cli) as ctx:
        param = cli.to_info_dict(ctx)["params"][0]
    assert param["name"] == "name" and param["config_key"] == "app.name"
