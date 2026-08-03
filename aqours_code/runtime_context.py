from __future__ import annotations

import os
import platform
from pathlib import Path


def _detect_shell(system_name: str) -> str:
    shell = os.getenv("SHELL")
    if shell:
        return Path(shell).name
    if system_name == "Windows":
        comspec = os.getenv("COMSPEC")
        if comspec:
            return Path(comspec).name
        return "cmd.exe"
    return "unknown"


def _command_hints(system_name: str) -> list[str]:
    if system_name == "Windows":
        return [
            "The bash tool runs commands through cmd.exe on Windows.",
            "Use cmd-compatible commands, not PowerShell-only syntax.",
            "Use dir instead of ls/Get-ChildItem.",
            "Use type instead of cat/Get-Content for file previews.",
            "Use findstr instead of grep/Select-String.",
            "Use cd instead of pwd/Get-Location.",
            "Quote paths that contain spaces.",
        ]
    if system_name == "Darwin":
        return [
            "The bash tool runs commands through the host Unix shell.",
            "Use macOS-compatible shell commands.",
            "BSD tools may differ from GNU Linux tools.",
            "Quote paths that contain spaces.",
        ]
    return [
        "The bash tool runs commands through the host Unix shell.",
        "Use Linux-compatible shell commands.",
        "Quote paths that contain spaces.",
    ]


def detect_runtime_context(workdir: str | Path | None = None) -> dict:
    system_name = platform.system() or os.name
    resolved_workdir = Path(workdir or os.getcwd()).resolve()
    return {
        "os": system_name,
        "platform": platform.platform(),
        "shell": _detect_shell(system_name),
        "path_separator": os.sep,
        "workdir": str(resolved_workdir),
        "command_hints": _command_hints(system_name),
    }


def resolve_prompt_runtime_context(
    policy: dict | None = None,
    workdir: str | Path | None = None,
) -> dict:
    """Return the runtime environment visible to tools for prompt display."""
    configured = policy.get("prompt_runtime") if isinstance(policy, dict) else None
    if not isinstance(configured, dict):
        return detect_runtime_context(workdir)
    return {
        "os": configured.get("os", "unknown"),
        "platform": configured.get("platform", configured.get("os", "unknown")),
        "shell": configured.get("shell", "unknown"),
        "path_separator": configured.get("path_separator", "/"),
        "workdir": configured.get("workdir", ""),
        "command_hints": list(configured.get("command_hints", [])),
    }


def format_runtime_context_for_prompt(context: dict | None = None) -> str:
    data = context or detect_runtime_context()
    hints = "\n".join(f"- {hint}" for hint in data.get("command_hints", []))
    return (
        "Runtime environment:\n"
        f"- OS: {data.get('os', 'unknown')}\n"
        f"- Platform: {data.get('platform', 'unknown')}\n"
        f"- Shell: {data.get('shell', 'unknown')}\n"
        f"- Path separator: {data.get('path_separator', '')}\n"
        f"- Working directory: {data.get('workdir', '')}\n"
        "Command guidance:\n"
        f"{hints}"
    )
