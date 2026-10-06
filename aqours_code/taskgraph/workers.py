"""Workers that complete one node attempt in a worktree.

``AqoursWorker`` runs the Aqours single-agent path in a child process through
``worker_entry``; this module itself never imports the Aqours runtime.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Protocol

from .process import run_process, run_shell
from .sandbox import NO_SANDBOX, SandboxConfig, check_docker, container_name, remove_containers

WORKER_ENTRY_MODULE = "aqours_code.taskgraph.worker_entry"
DEFAULT_WORKER_TIMEOUT_S = 1800.0
# Extra time the parent waits beyond the worker's own deadline before killing
# the process tree, so the worker can stop and write its result itself.
DEFAULT_KILL_GRACE_S = 30.0


@dataclass
class WorkerRequest:
    """One attempt of one node."""

    node_id: str
    attempt: int
    prompt: str
    workspace: Path
    log_dir: Path
    timeout_s: float


@dataclass
class WorkerResult:
    """What a worker reports back."""

    ok: bool
    reason: str = ""  # "", "worker_error", "worker_timeout"
    exit_code: int | None = None
    duration_s: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    final_answer: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> "WorkerResult":
        """Build a result from a dict, ignoring unknown keys."""
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class Worker(Protocol):
    """Completes one node attempt in ``request.workspace``."""

    def run(self, request: WorkerRequest) -> WorkerResult: ...

    def describe(self) -> dict: ...

    def preflight(self) -> None: ...


class CommandWorker:
    """Run a fixed shell command per node; for tests and debugging only.

    Its commands always run on the host (sandbox ``none``).
    """

    sandbox = NO_SANDBOX

    def __init__(self, commands: Mapping[str, str]):
        self.commands = dict(commands)

    def describe(self) -> dict:
        """Return a description for config.json."""
        return {"worker": "command"}

    def preflight(self) -> None:
        """Nothing to check."""

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Run the node's command in its workspace."""
        command = self.commands.get(request.node_id)
        if command is None:
            return WorkerResult(ok=False, reason="worker_error",
                                error=f"no command for node {request.node_id}")
        env = {**os.environ, "TG_NODE_ID": request.node_id,
               "TG_ATTEMPT": str(request.attempt)}
        result = run_shell(command, cwd=request.workspace,
                           timeout=request.timeout_s, env=env)
        if result.timed_out:
            reason = "worker_timeout"
        else:
            reason = "" if result.ok else "worker_error"
        return WorkerResult(ok=result.ok, reason=reason, exit_code=result.exit_code,
                            duration_s=result.duration_s,
                            final_answer=result.output[-4000:],
                            error="" if result.ok else result.output[-4000:])


def write_json_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as JSON through a temporary file and rename."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class AqoursWorker:
    """Run the Aqours single-agent path in a child process.

    With a Docker ``sandbox``, the child runs the agent's ``bash`` commands in
    a container that sees only the node's worktree; the parent removes the
    container after the child exits, whatever happened to it.
    """

    def __init__(self, *, entry_command: Sequence[str] | None = None,
                 kill_grace_s: float = DEFAULT_KILL_GRACE_S,
                 sandbox: SandboxConfig = NO_SANDBOX):
        self.entry_command = list(entry_command or
                                  [sys.executable, "-m", WORKER_ENTRY_MODULE])
        self.kill_grace_s = kill_grace_s
        self.sandbox = sandbox

    def preflight(self) -> None:
        """Raise ``SandboxUnavailable`` if the Docker sandbox cannot run."""
        if self.sandbox.kind == "docker":
            check_docker(self.sandbox.image)

    def describe(self) -> dict:
        """Ask the worker entry which provider and model it would use."""
        info = {"worker": "aqours"}
        result = run_process([*self.entry_command, "--describe"], cwd=Path.cwd(),
                             timeout=60)
        try:
            info.update(json.loads(result.output.strip().splitlines()[-1]))
        except (IndexError, json.JSONDecodeError):
            info["describe_error"] = result.output[-2000:]
        return info

    def config_for(self, request: WorkerRequest) -> dict:
        """Return the worker_entry configuration for one attempt."""
        log_dir = request.log_dir.resolve()
        n = request.attempt
        return {
            "node_id": request.node_id,
            "attempt": n,
            "task": request.prompt,
            "workspace": str(request.workspace.resolve()),
            "log_dir": str(log_dir),
            "trace_path": str(log_dir / f"trace_{n}.jsonl"),
            "result_path": str(log_dir / f"worker_{n}.json"),
            "trace_storage_root": str(log_dir / f"aqours_{n}" / "trace"),
            "runtime_root": str(log_dir / f"aqours_{n}" / "state"),
            "timeout_s": request.timeout_s,
            "sandbox": self._sandbox_config(request),
        }

    def _sandbox_config(self, request: WorkerRequest) -> dict:
        if self.sandbox.kind != "docker":
            return {"kind": "none"}
        return {"kind": "docker", "image": self.sandbox.image,
                "container": container_name(request.node_id, request.attempt,
                                            secrets.token_hex(4))}

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Run one attempt in a child process and read its result file."""
        config = self.config_for(request)
        log_dir = Path(config["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        config_path = log_dir / f"worker_{request.attempt}_config.json"
        write_json_atomic(config_path, config)
        result_path = Path(config["result_path"])
        result_path.unlink(missing_ok=True)
        env = {**os.environ, "AQOURS_CODE_WORKDIR": config["workspace"]}
        started = time.monotonic()
        try:
            proc = run_process([*self.entry_command, "--config", str(config_path)],
                               cwd=log_dir, env=env,
                               timeout=request.timeout_s + self.kill_grace_s)
        finally:
            if config["sandbox"]["kind"] == "docker":
                remove_containers(config["sandbox"]["container"])
        duration = time.monotonic() - started
        (log_dir / f"worker_{request.attempt}_stdout.txt").write_text(
            proc.output, encoding="utf-8")
        if proc.timed_out:
            return WorkerResult(ok=False, reason="worker_timeout", duration_s=duration,
                                error="worker process killed after timeout")
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return WorkerResult(ok=False, reason="worker_error", exit_code=proc.exit_code,
                                duration_s=duration,
                                error=f"no worker result: {exc}; {proc.output[-2000:]}")
        result = WorkerResult.from_dict(data)
        result.exit_code = proc.exit_code
        result.duration_s = duration
        if proc.exit_code != 0 and result.ok:
            result.ok, result.reason = False, "worker_error"
        return result
