"""Subprocess helpers: run a command with a timeout and kill its whole tree."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessResult:
    """Outcome of one command."""

    command: str
    exit_code: int | None
    output: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        """True when the command exited with code 0 before its timeout."""
        return self.exit_code == 0 and not self.timed_out


def _group_kwargs() -> dict:
    """Start the child in its own process group so the tree can be killed."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and every process it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_process(args: str | Sequence[str], *, cwd: Path, timeout: float,
                env: Mapping[str, str] | None = None,
                shell: bool = False) -> ProcessResult:
    """Run ``args`` in ``cwd``; on timeout kill the process tree.

    stdout and stderr are merged and decoded as UTF-8 with replacement.
    """
    command = args if isinstance(args, str) else " ".join(map(str, args))
    started = time.monotonic()
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=dict(env) if env is not None else None, shell=shell,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        **_group_kwargs(),
    )
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raw = b""
    return ProcessResult(
        command=command,
        exit_code=None if timed_out else proc.returncode,
        output=(raw or b"").decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )


def run_shell(command: str, *, cwd: Path, timeout: float,
              env: Mapping[str, str] | None = None) -> ProcessResult:
    """Run one check or worker command through the system shell."""
    return run_process(command, cwd=cwd, timeout=timeout, env=env, shell=True)
