"""Docker sandbox for worker shell commands.

With ``--sandbox docker`` (the default of ``run``), a worker's ``bash`` tool
runs in a container that sees only the node's worktree, mounted at
``/workspace``, with no network and a read-only root file system. It uses
the existing ``DockerCommandExecutor`` and the eval image. The worker's file
tools already stay inside the worktree. Checks, final checks and hidden tests
still run on the host, by the Coordinator.

This module never imports the Aqours runtime at import time.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IMAGE = "aqours-code-eval:py311"
BUILD_COMMAND = "docker build -f evals/docker/Dockerfile -t aqours-code-eval:py311 ."
SANDBOX_KINDS = ("docker", "none")
# Container limits for one worker. A worker runs the repository's tests, so it
# gets more than an eval case (1 CPU, 1 GiB, 128 processes, 120 s).
CONTAINER_MEMORY = "2g"
CONTAINER_CPUS = "2"
CONTAINER_PIDS = 256
COMMAND_TIMEOUT_S = 600.0
DOCKER_TIMEOUT_S = 60.0
CONTAINER_PREFIX = "aqours-tg"


class SandboxUnavailable(RuntimeError):
    """Docker or the sandbox image is missing; the run must not start."""


@dataclass(frozen=True)
class SandboxConfig:
    """Where a worker's shell commands run."""

    kind: str = "docker"
    image: str = DEFAULT_IMAGE

    def __post_init__(self) -> None:
        if self.kind not in SANDBOX_KINDS:
            raise ValueError(f"sandbox must be one of {', '.join(SANDBOX_KINDS)}")

    @property
    def image_name(self) -> str | None:
        """The image, or None without a sandbox."""
        return self.image if self.kind == "docker" else None

    def to_dict(self) -> dict:
        """``{"sandbox": ..., "sandbox_image": ...}`` for config.json and summary.json."""
        return {"sandbox": self.kind, "sandbox_image": self.image_name}


NO_SANDBOX = SandboxConfig(kind="none")


def check_docker(image: str = DEFAULT_IMAGE, runner=subprocess.run) -> None:
    """Raise ``SandboxUnavailable`` unless Docker runs and ``image`` exists."""
    hint = f"Build the image with: {BUILD_COMMAND}"
    try:
        info = runner(["docker", "info", "--format", "{{.ServerVersion}}"],
                      capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except FileNotFoundError:
        raise SandboxUnavailable(
            "--sandbox docker needs Docker, but the docker command was not found. "
            "Install and start Docker, then " + hint[0].lower() + hint[1:]) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxUnavailable(f"cannot run docker: {exc}") from None
    if info.returncode != 0:
        detail = (info.stderr or info.stdout or "").strip().splitlines()
        raise SandboxUnavailable(
            "--sandbox docker needs a running Docker daemon"
            + (f" ({detail[-1]})" if detail else "") + ". Start Docker and try again.")
    try:
        found = runner(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                       capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxUnavailable(f"cannot inspect image {image}: {exc}") from None
    if found.returncode != 0:
        raise SandboxUnavailable(f"Docker image {image} not found. {hint}")


def container_name(node_id: str, attempt: int, token: str) -> str:
    """A Docker container name for one worker attempt."""
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in node_id).strip("-")
    return f"{CONTAINER_PREFIX}-{safe[:30] or 'node'}-{attempt}-{token}"


def remove_containers(name: str, runner=subprocess.run) -> list[str]:
    """Remove the container ``name`` and its restarts (``name-r<n>``); never raises.

    Returns the names that were still there, so a caller can tell whether the
    worker had cleaned up after itself.
    """
    try:
        listed = runner(["docker", "ps", "--all", "--filter", f"name={name}",
                         "--format", "{{.Names}}"],
                        capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = [line.strip() for line in (listed.stdout or "").splitlines()
             if line.strip() == name or line.strip().startswith(f"{name}-r")]
    for leftover in names:
        try:
            runner(["docker", "rm", "-f", leftover], capture_output=True, text=True,
                   timeout=DOCKER_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return names


class RestartingDockerExecutor:
    """A ``DockerCommandExecutor`` that gets a fresh container after a timeout.

    ``DockerCommandExecutor`` removes its container when a command times out,
    after which every command fails. A worker that runs one slow command
    should keep its shell, so the next command starts a new container on the
    same workspace (the workspace is the only state, and it is on the host).
    """

    backend_name = "docker"

    def __init__(self, factory, *, deadline: float | None = None):
        self._factory = factory
        self._deadline = deadline
        self._index = 0
        self.inner = factory(0)
        self.restarts = 0

    def start(self):
        """Start the first container."""
        return self.inner.start()

    def execute(self, command: str, cwd: str | Path, timeout: float | None = None) -> dict:
        """Run ``command``; replace the container if it timed out."""
        result = self.inner.execute(command, cwd, timeout)
        if result.get("timed_out") and (self._deadline is None
                                        or time.monotonic() < self._deadline):
            self.inner.stop()
            self._index += 1
            self.restarts += 1
            self.inner = self._factory(self._index)
            self.inner.start()
        return result

    def stop(self, deadline: float | None = None):
        """Stop and remove the current container."""
        return self.inner.stop(deadline) if deadline is not None else self.inner.stop()

    def execution_metadata(self) -> dict:
        """The current container's metadata plus the restart count."""
        return {**self.inner.execution_metadata(), "container_restarts": self.restarts}


def docker_executor(workspace: str | Path, image: str, name: str, *,
                    deadline: float | None = None) -> RestartingDockerExecutor:
    """The executor a sandboxed worker uses for ``bash``."""
    from aqours_code.command_executor import DockerCommandExecutor  # noqa: PLC0415

    def factory(index: int):
        container = name if index == 0 else f"{name}-r{index}"
        return DockerCommandExecutor(
            workspace=workspace, image=image, case_name=name, container_name=container,
            memory=CONTAINER_MEMORY, cpus=CONTAINER_CPUS, pids_limit=CONTAINER_PIDS,
            command_timeout=COMMAND_TIMEOUT_S, docker_timeout=DOCKER_TIMEOUT_S,
            operation_deadline=deadline)

    return RestartingDockerExecutor(factory, deadline=deadline)
