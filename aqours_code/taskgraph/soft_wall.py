"""A recoverable, per-attempt reading nudge; not a security boundary.

This module imports no Aqours runtime. worker_entry alone registers its bound
callbacks using the public hook API. Ambiguous shell commands are allowed.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

READ_COMMANDS = {'cat', 'head', 'tail', 'less', 'more', 'sed', 'awk', 'grep',
                 'nl', 'type', 'findstr'}


def repository_path(value: str, workspace: Path) -> str | None:
    """Canonical in-workspace path, accepting host, Docker and Windows spellings."""
    root = workspace.resolve()
    value = value.replace('\\', '/')
    if value.startswith('/workspace/') and not value.startswith(str(root) + '/'):
        value = value[len('/workspace/'):]
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root):
        return None
    relative = candidate.relative_to(root).as_posix()
    if '.git' in Path(relative).parts:
        return None
    return relative


def python_read_arguments(code: str) -> list[str]:
    """Literal paths in obviously read-only python -c snippets; unknown code passes."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return []
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    reads = False
    for call in calls:
        name = (call.func.attr if isinstance(call.func, ast.Attribute)
                else call.func.id if isinstance(call.func, ast.Name) else '')
        if name not in {'open', 'Path', 'read', 'read_text', 'read_bytes',
                        'readlines', 'print', 'len', 'str', 'repr'}:
            return []  # An unknown call may write: do not impede edits.
        if name == 'open':
            modes = [*call.args[1:2], *(kw.value for kw in call.keywords if kw.arg == 'mode')]
            if any(not isinstance(m, ast.Constant) or not isinstance(m.value, str)
                   or any(c in m.value for c in 'wax+') for m in modes):
                return []
            reads = True
        if name in {'read', 'read_text', 'read_bytes', 'readlines'}:
            reads = True
    if not reads:
        return []
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
            and isinstance(n.value, str)]


def _strip_harmless_redirects(command: str) -> str:
    """Remove only known discard/merge redirects, preserving quoted arguments."""
    pattern = re.compile(
        r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
        r'|(?<![\w<>])2\s*>\s*&\s*1(?=\s|[;&|]|$)'
        r'|(?<![\w<>])2\s*>\s*(?:/dev/null|(?i:nul))(?=\s|[;&|]|$)'
        r'|(?<![0-9<>])>\s*(?:/dev/null|(?i:nul))(?=\s|[;&|]|$)',
    )
    return pattern.sub(lambda match: match[1] if match[1] is not None else ' ', command)


def bash_read_paths(command: str, workspace: Path) -> list[str]:
    """Existing files named by conservative content-reader commands/globs.

    Listings, pytest, redirects, in-place sed and write-capable Python are
    allowed. Root cd and harmless redirects are ignored; other cd targets
    and unknown shell constructs are not interpreted.
    """
    if re.match(r'^\s*(?:python\s+-m\s+pytest|pytest)(?:\s|$)', command):
        return []
    command = _strip_harmless_redirects(command)
    # A redirect or substitution may write; prefer a false negative to blocking
    # code edits. This also lets here-docs and mixed read/write pipelines pass.
    if any(marker in command for marker in ('>', '<', '`', '$(')):
        return []
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=';&|')
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = [token[1:-1] if len(token) >= 2 and token[0] == token[-1]
                  and token[0] in '\"\'' else token for token in lexer]
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {';', '&&', '||', '|', '&'}:
            segments.append([])
        else:
            segments[-1].append(token)
    arguments: list[tuple[str, bool]] = []
    for args in segments:
        if not args:
            continue
        name = args[0].lower()
        if name == 'cd':
            targets = args[2:] if len(args) > 1 and args[1].lower() == '/d' else args[1:]
            if len(targets) != 1:
                return []
            target = targets[0].replace('\\', '/')
            if target.rstrip('/') == '/workspace' or repository_path(target, workspace) == '.':
                continue
            return []
        if name in {'python', 'python3', 'python.exe'} and len(args) >= 3 and args[1] == '-c':
            paths = python_read_arguments(args[2])
            if not paths:
                return []
            arguments += [(path, False) for path in paths]
        elif name in READ_COMMANDS:
            if name == 'sed' and any(a.startswith('-i') or a == '--in-place' or
                                      a.startswith('--in-place=') for a in args[1:]):
                return []
            if name == 'sed' and any(re.search(r'(?:^|[;/\s])(?:[gpI0-9]*[weW])(?:\s|$)', a)
                                     for a in args[1:]):
                return []
            if name == 'awk' and any('|' in a or re.search(r'\b(?:system|getline)\b', a) for a in args[1:]):
                return []
            recursive = (
                name == 'grep' and any(
                    (a.startswith('-') and not a.startswith('--') and any(c in a[1:] for c in 'rR'))
                    or a in {'--recursive', '--dereference-recursive'} for a in args[1:])
                or name == 'findstr' and any(a.lower() == '/s' for a in args[1:])
            )
            arguments += [(arg, recursive) for arg in args[1:]]
        elif name not in {'ls', 'dir', 'pwd', 'pytest'}:
            return []  # A mixed command with unknown effects may be writing.
    result: set[str] = set()
    for value, recursive in arguments:
        relative = repository_path(value, workspace)
        if relative is None:
            continue
        for match in glob.glob(str(workspace.resolve() / relative), recursive=True):
            path = repository_path(match, workspace)
            if path is None:
                continue
            file = workspace / path
            candidates = file.rglob('*') if recursive and file.is_dir() else [file]
            for candidate in candidates:
                relative_file = repository_path(str(candidate), workspace)
                if relative_file is not None and candidate.is_file():
                    result.add(relative_file)
    return sorted(result)


def successful_output(output: object) -> bool:
    """Existing Aqours file tools report failures as prefixed text."""
    text = str(output).lstrip()
    return not text.startswith(('Error:', '[Error]', 'Tool not run:', 'Permission denied'))


@dataclass(frozen=True)
class ReadDecision:
    decision: str  # blocked or allowed (confirmed)
    paths: tuple[str, ...]


class SoftWall:
    """Attempt-local decisions and callbacks; deactivate when run_agent_task exits."""

    def __init__(self, workspace: Path, own_files: list[str], full_files: list[str],
                 log_path: Path):
        self.workspace = workspace.resolve()
        self.allowed = {path for value in [*own_files, *full_files]
                        if (path := repository_path(value, self.workspace)) is not None}
        self.seen: set[tuple[str, str]] = set()
        self.pending: dict[str, ReadDecision] = {}
        self.before_bash: dict[str, dict] = {}
        self.log_path = log_path
        self.active = True
        self.bash_results: dict[str, bool] = {}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()

    def decide(self, tool: str, data: dict) -> ReadDecision | None:
        """None allows freely; blocked once per canonical file or exact command."""
        if tool == 'read_file':
            path = repository_path(str(data.get('path', '')), self.workspace)
            paths = [path] if path and (self.workspace / path).is_file() else []
            key = (tool, path or '')
        elif tool == 'bash':
            command = str(data.get('command', ''))
            paths = bash_read_paths(command, self.workspace)
            key = (tool, command)
        else:
            return None
        outside = tuple(path for path in paths if path not in self.allowed)
        if not outside:
            return None
        decision = 'allowed' if key in self.seen else 'blocked'
        self.seen.add(key)
        return ReadDecision(decision, outside)

    def _record(self, block, decision: ReadDecision) -> None:
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'tool': block.name,
                 'tool_use_id': block.id, 'paths': list(decision.paths),
                 'decision': decision.decision}
        key = 'command' if block.name == 'bash' else 'path'
        entry[key] = block.input.get(key, '')
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def pre_tool(self, block, reject):
        if not self.active:
            return None
        decision = self.decide(block.name, block.input)
        if decision and decision.decision == 'blocked':
            self._record(block, decision)
            detail = ', '.join(decision.paths)
            retry = ('call read_file again with the same path' if block.name == 'read_file'
                     else 'repeat exactly the same command; prefer read_file for a specific file')
            return reject(
                f'Tool not run: {detail} is not part of your sub-task. '
                'Use the interfaces (signatures and docstrings) already in your context. '
                f'If your sub-task really cannot be done without the full file, {retry}, '
                'and say why in your final answer.')
        if decision:
            self.pending[block.id] = decision
        if block.name == 'bash':
            self.before_bash[block.id] = self._file_stamps()
        return None

    def post_tool(self, block, output):
        if not self.active:
            return None
        if block.name == 'bash':
            before = self.before_bash.pop(block.id, {})
            after = self._file_stamps()
            self.allowed.update(path for path, stamp in after.items() if before.get(path) != stamp)
        if block.name in {'write_file', 'edit_file'} and successful_output(output):
            path = repository_path(str(block.input.get('path', '')), self.workspace)
            if path and (self.workspace / path).is_file():
                self.allowed.add(path)
        decision = self.pending.pop(block.id, None)
        success = (self.bash_results.pop(block.input.get('command', ''), False)
                   if block.name == 'bash' else successful_output(output))
        if decision and success:
            self._record(block, decision)
        return None

    def _file_stamps(self) -> dict:
        """Metadata only: recognize files a shell tool created/changed, without reading."""
        stamps = {}
        for directory, dirs, files in os.walk(self.workspace):
            dirs[:] = [name for name in dirs if name not in {'.git', '.aqours_code'}]
            for name in files:
                file = Path(directory) / name
                try:
                    stat = file.stat()
                    path = repository_path(str(file), self.workspace)
                    if path is not None:
                        stamps[path] = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
                except OSError:
                    continue
        return stamps


def read_wall_log(path: Path) -> list[dict]:
    """Read complete events, tolerating an interrupted final line."""
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('decision') in {'blocked', 'allowed'}:
            events.append(event)
    return events
