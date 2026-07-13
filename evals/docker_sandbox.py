from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from codepilot_s20.command_executor import SandboxError, _decode_timeout_stream


def bind_mount(source: str | Path, target: str, *, readonly: bool = False) -> str:
    """Build one --mount value without colon-delimited Windows path parsing."""
    value = f"type=bind,source={Path(source).resolve()},target={target}"
    if readonly:
        value += ",readonly"
    return value


def build_eval_image(
    *,
    project_root: Path,
    image: str,
    timeout: float = 600,
    runner=subprocess.run,
) -> subprocess.CompletedProcess:
    docker_dir = project_root / "evals" / "docker"
    args = [
        "docker", "build",
        "--file", str(docker_dir / "Dockerfile"),
        "--tag", image,
        str(docker_dir),
    ]
    try:
        proc = runner(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise SandboxError(f"Docker image build failed: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "docker build failed").strip()
        raise SandboxError(f"Docker image build failed: {detail}")
    return proc


class DockerGraderRunner:
    backend_name = "docker"

    def __init__(
        self,
        *,
        image: str,
        case_name: str,
        memory: str = "1g",
        cpus: str | float = "1",
        pids_limit: int = 128,
        timeout: float = 120,
        docker_timeout: float = 30,
        runner=subprocess.run,
    ):
        self.image = image
        self.case_name = case_name
        self.memory = str(memory)
        self.cpus = str(cpus)
        self.pids_limit = int(pids_limit)
        self.timeout = float(timeout)
        self.docker_timeout = float(docker_timeout)
        self.runner = runner
        safe_case = "".join(ch.lower() if ch.isalnum() else "-" for ch in case_name).strip("-")[:40]
        self.container_name = f"codepilot-grader-{safe_case or 'case'}-{uuid.uuid4().hex[:10]}"
        self.timed_out = False
        self.cleanup_succeeded = False

    @property
    def resource_limits(self) -> dict:
        return {
            "memory": self.memory,
            "memory_swap": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "nofile": "256:256",
            "tmpfs": "/tmp:rw,noexec,nosuid,size=64m",
        }

    def docker_run_args(
        self,
        *,
        trusted_eval_root: Path,
        grading_workspace: Path,
        trace_path: Path,
        final_path: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> list[str]:
        grader = f"/trusted_eval/cases/{self.case_name}/grader.py"
        mounts = [
            bind_mount(trusted_eval_root, "/trusted_eval", readonly=True),
            bind_mount(grading_workspace, "/grading_workspace", readonly=True),
            bind_mount(trace_path, "/inputs/trace.jsonl", readonly=True),
            bind_mount(final_path, "/inputs/final.md", readonly=True),
            bind_mount(stdout_path, "/inputs/stdout.txt", readonly=True),
            bind_mount(stderr_path, "/inputs/stderr.txt", readonly=True),
        ]
        args = [
            "docker", "run", "--name", self.container_name,
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "10001:10001",
            "--memory", self.memory,
            "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--ulimit", "nofile=256:256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir", f"/trusted_eval/cases/{self.case_name}",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--env", "PYTHONNOUSERSITE=1",
            "--env", "PYTHONPATH=",
            "--env", "HOME=/tmp/home",
        ]
        for mount in mounts:
            args.extend(["--mount", mount])
        args.extend([
            self.image,
            "python", grader,
            "--workspace", "/grading_workspace",
            "--trace", "/inputs/trace.jsonl",
            "--final", "/inputs/final.md",
            "--stdout", "/inputs/stdout.txt",
            "--stderr", "/inputs/stderr.txt",
        ])
        return args

    def _cleanup(self):
        try:
            proc = self.runner(
                ["docker", "rm", "-f", self.container_name],
                capture_output=True,
                text=True,
                timeout=self.docker_timeout,
            )
            # A completed `docker run` leaves the named stopped container. A
            # daemon "not found" response is also a successful no-residue state.
            detail = ((proc.stderr or "") + (proc.stdout or "")).lower()
            self.cleanup_succeeded = proc.returncode == 0 or "no such container" in detail
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self.cleanup_succeeded = False

    def run(self, **paths) -> tuple[subprocess.CompletedProcess, dict]:
        args = self.docker_run_args(**paths)
        started = time.perf_counter()
        try:
            proc = self.runner(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.timed_out = True
            proc = subprocess.CompletedProcess(
                args,
                124,
                _decode_timeout_stream(exc.stdout),
                _decode_timeout_stream(exc.stderr),
            )
        except (FileNotFoundError, OSError) as exc:
            raise SandboxError(f"Docker grader failed to start: {type(exc).__name__}: {exc}") from exc
        finally:
            self._cleanup()
        if proc.returncode == 125:
            detail = (proc.stderr or proc.stdout or "docker run failed").strip()
            raise SandboxError(f"Docker grader failed to start: {detail}")
        return proc, {
            "timed_out": self.timed_out,
            "container_exit_code": proc.returncode,
            "cleanup_succeeded": self.cleanup_succeeded,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "resource_limits": self.resource_limits,
        }
