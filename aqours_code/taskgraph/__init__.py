"""Experimental task graph: schema, repository index, validator, edge derivation, flattening.

Nothing outside this subpackage imports it; use it through its own API or
``python -m aqours_code.taskgraph``.
"""
from .derive import derive_edges
from .flatten import flatten_graph
from .repo_index import RepoIndex, build_index
from .schema import (
    Check,
    Edge,
    EditSet,
    Generator,
    Graph,
    Node,
    RevisionEntry,
    SymbolRef,
    dump_graph,
    load_graph,
    parse_symbol,
)
from .validate import Issue, ValidationReport, validate

__all__ = [
    "Check", "Edge", "EditSet", "Generator", "Graph", "Issue", "Node",
    "RepoIndex", "RevisionEntry", "SymbolRef", "ValidationReport",
    "build_index", "derive_edges", "dump_graph", "flatten_graph", "load_graph",
    "parse_symbol",
    "validate",
]
