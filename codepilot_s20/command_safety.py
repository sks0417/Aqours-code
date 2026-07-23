from __future__ import annotations

import re
import shlex


_DELETE_EXECUTABLES = {
    "del", "erase", "rd", "remove-item", "rmdir", "rm", "unlink",
}
_COMMAND_WRAPPERS = {"builtin", "command", "env", "nohup"}
_SHELL_WRAPPERS = {"bash", "cmd", "cmd.exe", "pwsh", "pwsh.exe",
                   "powershell", "powershell.exe", "sh"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _executable_name(token: str) -> str:
    value = token.strip().strip("'\"").replace("\\", "/").rsplit("/", 1)[-1]
    lowered = value.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if lowered.endswith(suffix):
            lowered = lowered[:-len(suffix)]
            break
    return lowered


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(
        str(command), posix=True, punctuation_chars=";&|\n")
    # Preserve newlines as command separators while still removing shell
    # quoting. This prevents command-looking words inside a quoted Python
    # payload from being treated as an executable.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segment_executable(tokens: list[str]) -> tuple[str, int]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = _executable_name(token)
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if name in _COMMAND_WRAPPERS:
            index += 1
            while index < len(tokens) and (
                    tokens[index].startswith("-")
                    or _ASSIGNMENT.match(tokens[index])):
                index += 1
            continue
        return name, index
    return "", index


def _segment_looks_like_delete(tokens: list[str], depth: int) -> bool:
    executable, index = _segment_executable(tokens)
    if executable in _DELETE_EXECUTABLES:
        return True
    if depth >= 2 or executable not in _SHELL_WRAPPERS:
        return False

    arguments = tokens[index + 1:]
    command_switches = {"-c", "-command", "/c"}
    for arg_index, argument in enumerate(arguments):
        if argument.lower() in command_switches and arg_index + 1 < len(arguments):
            nested = " ".join(arguments[arg_index + 1:])
            return looks_like_delete_command(nested, _depth=depth + 1)
    return False


def looks_like_delete_command(command: str, *, _depth: int = 0) -> bool:
    """Return whether *command* invokes a known file-deletion executable.

    Detection is restricted to executable positions in shell command segments.
    Arguments and quoted program payloads are intentionally not scanned for
    substrings such as ``"rm "``.
    """
    try:
        tokens = _tokenize(command)
    except ValueError:
        # An unparsable command is not safe to execute if it starts with a
        # deletion executable. Keep this fallback narrow to avoid reverting to
        # the substring false positives this parser replaces.
        return bool(re.search(
            r"(?i)(?:^|[;&|]\s*)\s*(?:[\w.-]+=\S+\s+)*"
            r"(?:[^\s;&|]+[/\\])?"
            r"(?:rm|rmdir|rd|del|erase|remove-item|unlink)(?:\.exe)?"
            r"(?=\s|$)",
            str(command),
        ))

    segment: list[str] = []
    for token in tokens + [";"]:
        if token and all(char in ";&|\n" for char in token):
            if segment and _segment_looks_like_delete(segment, _depth):
                return True
            segment = []
        else:
            segment.append(token)
    return False
