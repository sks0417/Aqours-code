"""Command-line entry point: ``python -m aqours_code.taskgraph <command>``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from .derive import derive_edges
from .repo_index import RepoIndex, build_index
from .schema import DEFAULT_SCHEMA_PATH, Graph, dump_graph, export_json_schema, load_graph
from .validate import validate

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_INPUT_ERROR = 2


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_INPUT_ERROR


def _load(path: str) -> Graph | None:
    try:
        return load_graph(path)
    except FileNotFoundError:
        _error(f"graph file not found: {path}")
    except OSError as exc:
        _error(f"cannot read graph file {path}: {exc.strerror or exc}")
    except UnicodeDecodeError:
        _error(f"graph file {path} is not valid UTF-8")
    except ValidationError as exc:
        print(f"error: {path} does not match the task graph schema:\n{exc}",
              file=sys.stderr)
    return None


def _index(repo: str, commit: str) -> RepoIndex | None:
    try:
        index = build_index(Path(repo), commit)
    except RuntimeError as exc:
        _error(f"cannot index {repo} at {commit}: {exc}")
        return None
    for warning in index.warnings:
        print(f"index warning: {warning}", file=sys.stderr)
    return index


def _cmd_validate(args: argparse.Namespace) -> int:
    graph = _load(args.graph)
    if graph is None:
        return EXIT_INPUT_ERROR
    index = None
    if args.repo:
        index = _index(args.repo, graph.base_commit)
        if index is None:
            return EXIT_INPUT_ERROR
    report = validate(graph, index)
    print(report.format())
    return EXIT_OK if report.ok else EXIT_INVALID


def _cmd_derive(args: argparse.Namespace) -> int:
    graph = _load(args.graph)
    if graph is None:
        return EXIT_INPUT_ERROR
    index = _index(args.repo, graph.base_commit)
    if index is None:
        return EXIT_INPUT_ERROR
    derived, entries = derive_edges(graph, index)
    try:
        dump_graph(derived, args.out)
    except OSError as exc:
        return _error(f"cannot write {args.out}: {exc.strerror or exc}")
    print(f"revisions ({len(entries)}):")
    for entry in entries:
        print(f"[{entry.action}] {entry.reason}")
    report = validate(derived, index)
    print(report.format())
    print(f"wrote {args.out}")
    return EXIT_OK if report.ok else EXIT_INVALID


def _cmd_index(args: argparse.Namespace) -> int:
    index = _index(args.repo, args.commit)
    if index is None:
        return EXIT_INPUT_ERROR
    for symbol in sorted(index.symbols):
        print(symbol)
    return EXIT_OK


def _cmd_export_schema(args: argparse.Namespace) -> int:
    try:
        target = export_json_schema(args.out)
    except OSError as exc:
        return _error(f"cannot write {args.out}: {exc.strerror or exc}")
    print(f"wrote {target}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(prog="python -m aqours_code.taskgraph")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_cmd = commands.add_parser("validate", help="validate a task graph")
    validate_cmd.add_argument("graph")
    validate_cmd.add_argument("--repo", help="repository indexed at the graph's base_commit")
    validate_cmd.set_defaults(func=_cmd_validate)

    derive_cmd = commands.add_parser("derive", help="add rule-derived edges")
    derive_cmd.add_argument("graph")
    derive_cmd.add_argument("--repo", required=True)
    derive_cmd.add_argument("--out", required=True)
    derive_cmd.set_defaults(func=_cmd_derive)

    index_cmd = commands.add_parser("index", help="print the symbols of a commit")
    index_cmd.add_argument("--repo", required=True)
    index_cmd.add_argument("--commit", required=True)
    index_cmd.set_defaults(func=_cmd_index)

    schema_cmd = commands.add_parser("export-schema", help="regenerate the JSON Schema")
    schema_cmd.add_argument("--out", default=str(DEFAULT_SCHEMA_PATH))
    schema_cmd.set_defaults(func=_cmd_export_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)
