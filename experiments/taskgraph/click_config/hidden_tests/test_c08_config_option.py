"""Section "`click.config_option`".

Each docstring quotes the sentence of request.md that the test checks.
"""
import click


def write(path, text):
    path.write_bytes(text.encode("utf-8"))
    return path


def make_cli(**option_kwargs):
    @click.command()
    @click.config_option(**option_kwargs)
    @click.option("--port", type=int, default=1)
    @click.pass_context
    def cli(ctx, port):
        click.echo(f"port={port} config={ctx.config!r} path={ctx.config_path!r}")

    return cli


def one_line(text):
    return " ".join(text.split())


def test_defined_in_decorators_and_exported():
    """Add a decorator `config_option(*param_decls, default_paths=(),
    **kwargs)` in `click/decorators.py`, exported from `click`"""
    import click.decorators

    assert click.decorators.config_option is click.config_option


def test_adds_an_eager_config_option(runner):
    """It adds an eager option, `--config` unless `param_decls` are given,
    with metavar `PATH`, help text `Read option defaults from a
    configuration file.`, and `expose_value=False`."""
    cli = make_cli()
    (option,) = [p for p in cli.params if "--config" in getattr(p, "opts", ())]
    assert isinstance(option, click.Option)
    assert option.opts == ["--config"]
    assert option.is_eager is True
    assert option.expose_value is False
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "--config PATH" in result.output
    assert "Read option defaults from a configuration file." in one_line(result.output)
    result = runner.invoke(cli, [])
    assert result.exit_code == 0, result.output
    assert result.output == "port=1 config=None path=None\n"


def test_param_decls_and_other_keyword_arguments(runner, tmp_path):
    """`--config` unless `param_decls` are given ... Other keyword arguments
    are passed to the option (for example `envvar`)."""
    path = write(tmp_path / "app.toml", "port = 7\n")

    @click.command()
    @click.config_option("-s", "--settings", envvar="APP_SETTINGS")
    @click.option("--port", type=int, default=1)
    def cli(port):
        click.echo(f"port={port}")

    assert runner.invoke(cli, ["-s", str(path)]).output == "port=7\n"
    assert runner.invoke(cli, ["--settings", str(path)]).output == "port=7\n"
    assert runner.invoke(cli, [], env={"APP_SETTINGS": str(path)}).output == "port=7\n"
    result = runner.invoke(cli, ["--config", str(path)])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_given_file_becomes_config_and_config_path(runner, tmp_path):
    """When the option is given, the file is loaded with `load_config`, the
    result becomes `ctx.config`, and the path becomes `ctx.config_path`."""
    for name, text in (("app.toml", "port = 8\n"), ("app.json", '{"port": 9}')):
        path = str(write(tmp_path / name, text))
        result = runner.invoke(make_cli(), ["--config", path])
        assert result.exit_code == 0, result.output
        expected = click.load_config(path)
        assert result.output == f"port={expected['port']} config={expected!r} path={path!r}\n"


def test_first_existing_default_path_is_loaded(runner, tmp_path, monkeypatch):
    """When the option is not given, the first path in `default_paths` that
    exists as a file is loaded in the same way (`~` is expanded)."""
    (tmp_path / "folder.toml").mkdir()
    first = str(write(tmp_path / "first.toml", "port = 2\n"))
    write(tmp_path / "second.toml", "port = 3\n")
    paths = [str(tmp_path / "missing.toml"), str(tmp_path / "folder.toml"), first,
             str(tmp_path / "second.toml")]
    result = runner.invoke(make_cli(default_paths=paths), [])
    assert result.exit_code == 0, result.output
    assert result.output == f"port=2 config={{'port': 2}} path={first!r}\n"
    given = str(write(tmp_path / "given.toml", "port = 4\n"))
    result = runner.invoke(make_cli(default_paths=paths), ["--config", given])
    assert result.output == f"port=4 config={{'port': 4}} path={given!r}\n"

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    write(tmp_path / "home.toml", "port = 5\n")
    result = runner.invoke(make_cli(default_paths=["~/missing.toml", "~/home.toml"]), [])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("port=5 config={'port': 5} path=")


def test_no_default_path_exists(runner, tmp_path):
    """When none exists, the context's configuration is left as it is."""
    cli = make_cli(default_paths=[str(tmp_path / "missing.toml")])
    result = runner.invoke(cli, [], config={"port": 6})
    assert result.exit_code == 0, result.output
    assert result.output == "port=6 config={'port': 6} path=None\n"
    result = runner.invoke(cli, [])
    assert result.output == "port=1 config=None path=None\n"


def test_file_that_cannot_be_loaded_fails(runner, tmp_path):
    """A file that cannot be loaded fails the command with the `ConfigError`
    from `load_config`."""
    cases = (
        (str(tmp_path / "missing.toml"), "cannot read"),
        (str(write(tmp_path / "bad.json", "{port: 1}")), "invalid JSON"),
        (str(write(tmp_path / "app.yaml", "port: 1\n")), "unsupported configuration file type"),
    )
    for path, message in cases:
        result = runner.invoke(make_cli(), ["--config", path])
        assert result.exit_code == 2
        assert f"Error: {path}: " in result.output and message in result.output
    bad = str(write(tmp_path / "bad.toml", "port = \n"))
    result = runner.invoke(make_cli(default_paths=[bad]), [])
    assert result.exit_code == 2
    assert f"Error: {bad}: " in result.output and "invalid TOML" in result.output


def test_configuration_is_in_place_before_parameters_and_subcommands(runner, tmp_path):
    """Because the option is eager, the configuration is in place before the
    command's other parameters are processed, and before the configuration
    of its subcommands is looked up."""
    path = str(write(tmp_path / "app.toml",
                     'port = 8\nname = "app"\n\n[sync]\nforce = true\n'))

    def record(ctx, param, value):
        click.echo(f"seen={ctx.config!r}")
        return value

    @click.group()
    @click.option("--name", callback=record)
    @click.option("--port", type=int, default=1)
    @click.config_option()
    def cli(name, port):
        click.echo(f"name={name} port={port}")

    @cli.command()
    @click.option("--force", is_flag=True)
    @click.pass_context
    def sync(ctx, force):
        click.echo(f"force={force} path={ctx.config_path!r}")

    result = runner.invoke(cli, ["--name", "x", "--config", path, "sync"])
    assert result.exit_code == 0, result.output
    expected = click.load_config(path)
    assert result.output == (
        f"seen={expected!r}\nname=x port=8\nforce=True path={path!r}\n"
    )
