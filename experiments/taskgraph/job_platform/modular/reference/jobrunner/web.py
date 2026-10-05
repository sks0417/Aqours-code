"""Route and page registries shared by the REST API and the dashboard.

Each module registers its own REST routes with ``@route(method, pattern)``
and its own dashboard pages with ``@page(pattern)`` when it is imported;
``jobrunner.api.handle`` and ``jobrunner.dashboard.render_page`` dispatch to
them. A pattern is a regular expression matched against the whole path
(without the query string). The function is called as
``function(runner, request, **named_groups)``; a route returns
``(status_code, dict)`` and a page ``(status_code, html)``.

Errors raised by a route or page are turned into responses here:
``ValueError`` is 400, ``NotFound`` is 404, and ``InvalidTransition`` is 409.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import InvalidTransition, NotFound

Response = tuple[int, dict[str, Any]]
Page = tuple[int, str]

ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (ValueError, 400),
    (NotFound, 404),
    (InvalidTransition, 409),
)


@dataclass
class Request:
    """One request: the method, the path without query, the query, the body."""

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass
class _Entry:
    method: str
    pattern: re.Pattern[str]
    function: Callable[..., Any]


_routes: dict[tuple[str, str], _Entry] = {}
_pages: dict[str, _Entry] = {}


def route(method: str, pattern: str) -> Callable[[Callable[..., Response]], Callable[..., Response]]:
    """Register a REST route for ``method`` and the path regex ``pattern``."""
    def register(function: Callable[..., Response]) -> Callable[..., Response]:
        _routes[(method.upper(), pattern)] = _Entry(method.upper(), re.compile(pattern), function)
        return function
    return register


def page(pattern: str) -> Callable[[Callable[..., Page]], Callable[..., Page]]:
    """Register a dashboard page for the path regex ``pattern``."""
    def register(function: Callable[..., Page]) -> Callable[..., Page]:
        _pages[pattern] = _Entry("GET", re.compile(pattern), function)
        return function
    return register


def error(status: int, message: str) -> Response:
    """An error response with the body ``{"error": message}``."""
    return status, {"error": message}


def parse(method: str, path: str, body: Any = None) -> Request:
    """Split ``path`` into path and query (the first value of each parameter)."""
    parts = urlsplit(path)
    query = {name: values[0]
             for name, values in parse_qs(parts.query, keep_blank_values=True).items()}
    return Request(method.upper(), parts.path, query, body)


def _status_for(exc: Exception) -> int | None:
    for error_type, status in ERROR_STATUS:
        if isinstance(exc, error_type):
            return status
    return None


def _find(entries: Iterable[_Entry], request: Request) -> tuple[_Entry, dict[str, str]] | None:
    for entry in entries:
        if entry.method != request.method:
            continue
        match = entry.pattern.fullmatch(request.path)
        if match:
            return entry, match.groupdict()
    return None


def dispatch(runner: Any, method: str, path: str, body: Any = None) -> Response:
    """Call the route matching the request; 404 when none matches."""
    request = parse(method, path, body)
    found = _find(_routes.values(), request)
    if found is None:
        return error(404, f"no route for {request.method} {request.path}")
    entry, groups = found
    try:
        return entry.function(runner, request, **groups)
    except Exception as exc:
        status = _status_for(exc)
        if status is None:
            raise
        return error(status, str(exc))


def render(runner: Any, path: str) -> Page:
    """Render the page matching ``path``; 404 when none matches."""
    request = parse("GET", path)
    found = _find(_pages.values(), request)
    if found is None:
        return 404, f"<p>Not found: {escape(request.path)}</p>"
    entry, groups = found
    try:
        return entry.function(runner, request, **groups)
    except Exception as exc:
        status = _status_for(exc)
        if status is None:
            raise
        message = "Not found" if status == 404 else "Error"
        return status, f"<p>{message}: {escape(str(exc))}</p>"


class Markup(str):
    """HTML that ``table`` puts in a cell without escaping."""


def fields(request: Request, *required: str) -> dict[str, Any]:
    """The request body as a dict holding every ``required`` field, else ``ValueError``."""
    if not isinstance(request.body, dict):
        raise ValueError("body must be a JSON object")
    missing = [name for name in required if name not in request.body]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
    return request.body


def number(text: str, name: str) -> float:
    """Parse a query value as an int or a float; ``ValueError`` names ``name``."""
    for parse_as in (int, float):
        try:
            return parse_as(text)
        except ValueError:
            pass
    raise ValueError(f"{name} must be a number")


def time(value: float) -> str:
    """A time or a duration with one decimal, as the dashboard shows them."""
    return f"{value:.1f}"


def heading(title: str, *tables: str) -> str:
    """A dashboard page: an ``<h1>`` title followed by tables."""
    return "\n".join((f"<h1>{escape(title)}</h1>", *tables))


def table(css_class: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """An HTML table; cells are escaped unless they are ``Markup``."""
    def cell(value: Any) -> str:
        return value if isinstance(value, Markup) else escape(str(value))

    header = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "\n".join("<tr>" + "".join(f"<td>{cell(value)}</td>" for value in row) + "</tr>"
                     for row in rows)
    return (f'<table class="{escape(css_class)}">\n<thead><tr>{header}</tr></thead>\n'
            f"<tbody>\n{body}\n</tbody>\n</table>")
