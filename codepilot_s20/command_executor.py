from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path
from .docker_utils import host_container_user, normalize_bind_source


class CommandExecutorError(RuntimeError):
    """Base error raised by a command execution backend."""


class SandboxError(CommandExecutorError):
    """Docker could not create or operate the requested sandbox."""


class CaseTimeoutError(CommandExecutorError):
    """The enclosing eval case exhausted its wall-clock budget."""


def _decode_timeout_stream(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


class LocalCommandExecutor:
    backend_name = "local"

    def __init__(self):
        self.command_execution_count = 0
        self.sandbox_error = ""

    def start(self):
        return None

    def execute(self, command: str, cwd: str | Path, timeout: float) -> dict:
        started = time.perf_counter()
        self.command_execution_count += 1
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=Path(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "timed_out": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "command": command,
                "exit_code": None,
                "stdout": _decode_timeout_stream(exc.stdout),
                "stderr": _decode_timeout_stream(exc.stderr),
                "timed_out": True,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

    def stop(self):
        return None

    def execution_metadata(self) -> dict:
        return {
            "execution_backend": self.backend_name,
            "docker_image": None,
            "container_started": False,
            "container_exit_code": None,
            "container_timed_out": False,
            "container_cleanup_succeeded": True,
            "command_execution_count": self.command_execution_count,
            "resource_limits": {},
        }


class DockerCommandExecutor:
    backend_name = "docker"

    def __init__(
        self,
        *,
        workspace: str | Path,
        image: str,
        case_name: str,
        memory: str = "1g",
        cpus: str | float = "1",
        pids_limit: int = 128,
        command_timeout: float = 120,
        overall_timeout: float | None = None,
        docker_timeout: float = 30,
        container_name: str | None = None,
        container_user: str | None = None,
        verify_workspace_write: bool = True,
        runner=subprocess.run,
    ):
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.case_name = self._safe_name(case_name)
        self.memory = str(memory)
        self.cpus = str(cpus)
        self.pids_limit = int(pids_limit)
        self.command_timeout = float(command_timeout)
        self.overall_timeout = float(overall_timeout) if overall_timeout is not None else None
        self.docker_timeout = float(docker_timeout)
        self.runner = runner
        self.container_name = container_name or f"codepilot-agent-{self.case_name}-{uuid.uuid4().hex[:10]}"
        self.container_user = container_user or host_container_user()
        self.verify_workspace_write = verify_workspace_write
        self.container_started = False
        self.container_exit_code = None
        self.container_timed_out = False
        self.command_timed_out = False
        self.overall_timed_out = False
        self.container_cleanup_succeeded = False
        self.command_execution_count = 0
        self.sandbox_error = ""
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._overall_timer = None

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value))
        return cleaned.strip("-")[:40] or "case"

    @property
    def resource_limits(self) -> dict:
        return {
            "memory": self.memory,
            "memory_swap": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "nofile": "256:256",
            "tmpfs": "/tmp:rw,noexec,nosuid,size=64m",
            "overall_timeout_seconds": self.overall_timeout,
        }

    def docker_run_args(self) -> list[str]:
        mount = f"type=bind,source={normalize_bind_source(self.workspace)},target=/workspace"
        return [
            "docker", "run", "--detach",
            "--name", self.container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self.container_user,
            "--memory", self.memory,
            "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--ulimit", "nofile=256:256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", mount,
            "--workdir", "/workspace",
            "--env", "HOME=/tmp/home",
            self.image,
            "sleep", "infinity",
        ]

    def start(self):
        if self.container_started:
            return
        if not self.workspace.is_dir():
            raise SandboxError(f"agent workspace does not exist: {self.workspace}")
        try:
            proc = self.runner(
                self.docker_run_args(),
                capture_output=True,
                text=True,
                timeout=self.docker_timeout,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self.sandbox_error = f"{type(exc).__name__}: {exc}"
            self.stop()
            raise SandboxError(f"Docker sandbox failed to start: {type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            self.stop()
            detail = (proc.stderr or proc.stdout or "docker run failed").strip()
            self.sandbox_error = detail
            raise SandboxError(f"Docker sandbox failed to start: {detail}")
        self.container_started = True
        if self.verify_workspace_write:
            try:
                probe = self.runner(
                    ["docker", "exec", "--workdir", "/workspace",
                     self.container_name, "/bin/sh", "-lc",
                     "umask 077; p=.codepilot-write-probe-$$; : > \"$p\" && rm -f \"$p\""],
                    capture_output=True,
                    text=True,
                    timeout=min(self.docker_timeout, 10),
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                self.sandbox_error = f"workspace write probe failed: {type(exc).__name__}: {exc}"
                self.stop()
                raise SandboxError(self.sandbox_error) from exc
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout or "workspace is not writable").strip()
                self.sandbox_error = detail
                self.stop()
                raise SandboxError(
                    f"non-root sandbox user {self.container_user} cannot write /workspace: {detail}")
        if self.overall_timeout is not None:
            self._overall_timer = threading.Timer(
                self.overall_timeout,
                self.terminate_for_overall_timeout,
            )
            self._overall_timer.daemon = True
            self._overall_timer.start()

    def _container_cwd(self, cwd: str | Path) -> str:
        host_cwd = Path(cwd).resolve()
        try:
            relative = host_cwd.relative_to(self.workspace)
        except ValueError as exc:
            raise SandboxError(f"command cwd escapes agent workspace: {host_cwd}") from exc
        if not relative.parts:
            return "/workspace"
        return "/workspace/" + relative.as_posix()

    def execute(self, command: str, cwd: str | Path, timeout: float | None = None) -> dict:
        if not self.container_started or self._stopped:
            raise SandboxError("Docker sandbox is not running")
        started = time.perf_counter()
        self.command_execution_count += 1
        effective_timeout = self.command_timeout if timeout is None else min(float(timeout), self.command_timeout)
        args = [
            "docker", "exec",
            "--workdir", self._container_cwd(cwd),
            self.container_name,
            "/bin/sh", "-lc", command,
        ]
        try:
            proc = self.runner(
                args,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
            detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
            if proc.returncode in {125, 126} or any(token in detail for token in (
                "cannot connect to the docker daemon",
                "error during connect",
                "no such container",
                "is the docker daemon running",
            )):
                self.sandbox_error = (proc.stderr or proc.stdout or "docker exec failed").strip()
                self.stop()
                raise SandboxError(f"docker exec failed: {self.sandbox_error}")
            return {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "timed_out": False,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except subprocess.TimeoutExpired as exc:
            self.container_timed_out = True
            self.command_timed_out = True
            self.stop()
            return {
                "command": command,
                "exit_code": None,
                "stdout": _decode_timeout_stream(exc.stdout),
                "stderr": _decode_timeout_stream(exc.stderr),
                "timed_out": True,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        except (FileNotFoundError, OSError) as exc:
            self.sandbox_error = f"{type(exc).__name__}: {exc}"
            self.stop()
            raise SandboxError(f"docker exec failed: {type(exc).__name__}: {exc}") from exc

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            if self._overall_timer is not None:
                self._overall_timer.cancel()
            running = None
            explicitly_missing = False
            try:
                inspect = self.runner(
                    ["docker", "inspect", "--format",
                     "{{.State.Running}} {{.State.ExitCode}}", self.container_name],
                    capture_output=True, text=True, timeout=self.docker_timeout)
                detail = ((inspect.stderr or "") + (inspect.stdout or "")).lower()
                if inspect.returncode != 0 and "no such" in detail:
                    explicitly_missing = True
                elif inspect.returncode == 0:
                    self.container_started = True
                    parts = (inspect.stdout or "").strip().split()
                    if parts:
                        running = parts[0].lower() == "true"
                    if len(parts) > 1 and not running:
                        try:
                            self.container_exit_code = int(parts[1])
                        except ValueError:
                            pass
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

            if explicitly_missing:
                self.container_cleanup_succeeded = True
                return

            if running:
                try:
                    stopped = self.runner(
                        ["docker", "stop", "--time", "1", self.container_name],
                        capture_output=True, text=True, timeout=self.docker_timeout)
                    if stopped.returncode == 0:
                        inspect = self.runner(
                            ["docker", "inspect", "--format", "{{.State.ExitCode}}",
                             self.container_name],
                            capture_output=True, text=True, timeout=self.docker_timeout)
                        if inspect.returncode == 0:
                            self.container_exit_code = int((inspect.stdout or "").strip())
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
                    pass

            try:
                proc = self.runner(
                    ["docker", "rm", "-f", self.container_name],
                    capture_output=True, text=True, timeout=self.docker_timeout)
                detail = ((proc.stderr or "") + (proc.stdout or "")).lower()
                self.container_cleanup_succeeded = (
                    proc.returncode == 0 or "no such container" in detail)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                self.container_cleanup_succeeded = False

    def terminate_for_overall_timeout(self):
        self.container_timed_out = True
        self.overall_timed_out = True
        self.stop()

    def execution_metadata(self) -> dict:
        return {
            "execution_backend": self.backend_name,
            "docker_image": self.image,
            "container_started": self.container_started,
            "container_exit_code": self.container_exit_code,
            "container_timed_out": self.container_timed_out,
            "container_cleanup_succeeded": self.container_cleanup_succeeded,
            "command_execution_count": self.command_execution_count,
            "resource_limits": self.resource_limits,
            "sandbox_error": self.sandbox_error,
            "command_timed_out": self.command_timed_out,
            "overall_timed_out": self.overall_timed_out,
        }
