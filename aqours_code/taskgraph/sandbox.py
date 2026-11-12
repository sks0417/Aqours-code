"""Docker sandbox for worker shell commands and for the Coordinator's checks.

With ``--sandbox docker`` (the default of ``run``), a worker's ``bash`` tool
runs in a container that sees only the node's worktree, mounted at
``/workspace``, with no network and a read-only root file system. It uses
the existing ``DockerCommandExecutor`` and the eval image. The worker's file
tools already stay inside the worktree. Node checks, post-merge checks, final
checks and hidden tests run in the same kind of container (``checks.py``), so
code a worker wrote never runs on the host.

This module never imports the Aqours runtime at import time.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
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
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NOFILE_ARG = "nofile=256:256"  # what DockerCommandExecutor gives every container
MAX_NOFILE = 1_048_576
MAX_PIDS = 65_536
GIT_VIEW_TARGET = "/aqours/git"  # where a worktree's git view is mounted


class SandboxUnavailable(RuntimeError):
    """Docker or the sandbox image is missing; the run must not start."""


@dataclass(frozen=True)
class SandboxConfig:
    """Where a worker's shell commands run.

    ``env`` holds environment variables set in every container of the run
    (``docker run --env``), as ``(name, value)`` pairs. It is empty for the
    default eval image; a task whose image keeps its tools somewhere else
    (a SWE-bench image's conda environment) sets it in its ``sandbox.json``.
    ``nofile`` replaces the containers' limit of open files (256) for a task
    whose test suite needs more; ``None`` keeps it. ``pids`` does the same for
    the limit of processes and threads (256). ``git_view`` lets git
    work in a container on a worktree (see :func:`docker_executor`), for a
    task whose code runs git at import time.
    """

    kind: str = "docker"
    image: str = DEFAULT_IMAGE
    env: tuple[tuple[str, str], ...] = ()
    nofile: int | None = None
    pids: int | None = None
    git_view: bool = False

    def __post_init__(self) -> None:
        if self.kind not in SANDBOX_KINDS:
            raise ValueError(f"sandbox must be one of {', '.join(SANDBOX_KINDS)}")
        env = tuple((str(name), str(value)) for name, value in
                    (self.env.items() if isinstance(self.env, dict) else self.env))
        for name, _ in env:
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
        object.__setattr__(self, "env", env)
        if self.nofile is not None and (isinstance(self.nofile, bool)
                                        or not isinstance(self.nofile, int)
                                        or not 1 <= self.nofile <= MAX_NOFILE):
            raise ValueError(f"nofile must be an integer from 1 to {MAX_NOFILE}")
        if self.pids is not None and (isinstance(self.pids, bool)
                                      or not isinstance(self.pids, int)
                                      or not 1 <= self.pids <= MAX_PIDS):
            raise ValueError(f"pids must be an integer from 1 to {MAX_PIDS}")
        if not isinstance(self.git_view, bool):
            raise ValueError("git_view must be true or false")

    @property
    def image_name(self) -> str | None:
        """The image, or None without a sandbox."""
        return self.image if self.kind == "docker" else None

    @property
    def env_dict(self) -> dict[str, str]:
        """The environment variables, or ``{}`` without a sandbox."""
        return dict(self.env) if self.kind == "docker" else {}

    def to_dict(self) -> dict:
        """``sandbox``, ``sandbox_image`` and the task's container options.

        For config.json and summary.json: ``sandbox_env``, ``sandbox_nofile``,
        ``sandbox_pids`` (None when the containers keep the default limit) and
        ``sandbox_git_view``.
        """
        docker = self.kind == "docker"
        return {"sandbox": self.kind, "sandbox_image": self.image_name,
                "sandbox_env": self.env_dict,
                "sandbox_nofile": self.nofile if docker else None,
                "sandbox_pids": self.pids if docker else None,
                "sandbox_git_view": self.git_view and docker}


NO_SANDBOX = SandboxConfig(kind="none")


def load_sandbox_config(path: Path) -> SandboxConfig:
    """A Docker sandbox from a task's ``sandbox.json``.

    ``{"image": ..., "env": {"NAME": "value", ...}, "nofile": 4096,
    "pids": 4096, "git_view": true}``; all but ``image`` are optional, and
    ``comment`` is ignored.

    Raises ``OSError`` when the file cannot be read and ``ValueError`` when
    it is not such an object (unknown keys included).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    unknown = sorted(set(data) - {"image", "env", "nofile", "pids", "git_view", "comment"})
    if unknown:
        raise ValueError(f"{path}: unknown keys {', '.join(unknown)}")
    image, env = data.get("image"), data.get("env", {})
    if not isinstance(image, str) or not image.strip():
        raise ValueError(f"{path} needs a non-empty \"image\"")
    if not isinstance(env, dict) or not all(isinstance(value, str) for value in env.values()):
        raise ValueError(f"{path}: \"env\" must map names to strings")
    return SandboxConfig(kind="docker", image=image, env=tuple(env.items()),
                         nofile=data.get("nofile"), pids=data.get("pids"),
                         git_view=data.get("git_view", False))


def utf8_run(args, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` whose text output is UTF-8 with undecodable bytes replaced.

    Without this, ``text=True`` decodes with the platform's default encoding
    and raises on a byte it cannot decode: one odd byte in a command's output
    would end a worker or a check.
    """
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(args, **kwargs)


def check_docker(image: str = DEFAULT_IMAGE, runner=utf8_run) -> None:
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


def check_container_name(label: str, token: str) -> str:
    """A Docker container name for one group of check commands."""
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in label).strip("-")
    return f"{CONTAINER_PREFIX}-{safe[:40] or 'check'}-{token}"


def remove_containers(name: str, runner=utf8_run) -> list[str]:
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

    def __init__(self, factory, *, deadline: float | None = None, cleanup=()):
        self._factory = factory
        self._deadline = deadline
        self._cleanup = [Path(path) for path in cleanup]  # removed by stop()
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
        """Stop and remove the current container, then the executor's temporary files."""
        try:
            return self.inner.stop(deadline) if deadline is not None else self.inner.stop()
        finally:
            for path in self._cleanup:
                _remove_quietly(path)

    def execution_metadata(self) -> dict:
        """The current container's metadata plus the restart count."""
        return {**self.inner.execution_metadata(), "container_restarts": self.restarts}


def _remove_quietly(path: Path) -> None:
    """Delete a temporary directory, read-only git files included; never raises."""
    from .cleanup import remove_tree  # noqa: PLC0415

    try:
        remove_tree(Path(path))
    except OSError:
        pass


def task_run_args(run_args: list[str], image: str, env=(), nofile: int | None = None,
                  mounts=()) -> list[str]:
    """``docker run`` arguments with a task's options.

    ``--env NAME=VALUE`` for each pair of ``env`` and ``--mount`` for each of
    ``mounts`` go before the image (the arguments end with the image and its
    command, ``sleep infinity``), and ``nofile`` replaces the open-files limit.
    """
    at = len(run_args) - 3
    if at < 0 or run_args[at] != image:
        raise ValueError("docker run arguments do not end with the image and its command")
    extra = [part for name, value in env for part in ("--env", f"{name}={value}")]
    extra += [part for mount in mounts for part in ("--mount", mount)]
    args = [*run_args[:at], *extra, *run_args[at:]]
    if nofile is not None:
        if NOFILE_ARG not in args:
            raise ValueError(f"docker run arguments have no {NOFILE_ARG}")
        args[args.index(NOFILE_ARG)] = f"nofile={nofile}:{nofile}"
    return args


def git_view_mounts(workspace: str | Path, view_dir: Path) -> list[str]:
    """Build a git view of ``workspace`` in ``view_dir``; return its two ``--mount`` values.

    Only for a worktree (its ``.git`` is a file naming a host path, which a
    container cannot follow; see ``gitops.make_git_view``): the view is
    mounted read-only at ``/aqours/git``, and a file ``gitdir: /aqours/git``
    over ``/workspace/.git``. A directory whose ``.git`` is a directory (a
    clone) or that has none gets no view: ``[]``.
    """
    from aqours_code.docker_utils import normalize_bind_source  # noqa: PLC0415

    from . import gitops  # noqa: PLC0415

    if not (Path(workspace) / ".git").is_file():
        return []
    view_dir.mkdir(parents=True, exist_ok=True)
    gitops.make_git_view(Path(workspace), view_dir / "git")
    gitfile = view_dir / "gitfile"
    gitfile.write_text(f"gitdir: {GIT_VIEW_TARGET}\n", encoding="utf-8", newline="\n")
    return [f"type=bind,source={normalize_bind_source(view_dir / 'git')},"
            f"target={GIT_VIEW_TARGET},readonly",
            f"type=bind,source={normalize_bind_source(gitfile)},target=/workspace/.git,readonly"]


def docker_executor(workspace: str | Path, image: str, name: str, *,
                    deadline: float | None = None,
                    command_timeout: float = COMMAND_TIMEOUT_S,
                    env=(), nofile: int | None = None, pids: int | None = None,
                    git_view: bool = False) -> RestartingDockerExecutor:
    """The executor for shell commands in the sandbox.

    A worker uses it for ``bash``; the Coordinator uses it for checks, with
    the check's own timeout as ``command_timeout``. Both get the same
    container: ``workspace`` at ``/workspace``, no network, the same limits,
    and the task's options: ``env`` (pairs or a dict), which every command
    inherits, ``nofile`` and ``pids``, its limits of open files and of
    processes (``None``: the defaults), and ``git_view``: a
    worktree gets a git view (:func:`git_view_mounts`) in a temporary
    directory, built here and removed when the executor stops.
    """
    from aqours_code.command_executor import DockerCommandExecutor  # noqa: PLC0415

    pairs = tuple(env.items() if isinstance(env, dict) else env)
    view_dir = None
    mounts: list[str] = []
    if git_view and (Path(workspace) / ".git").is_file():
        view_dir = Path(tempfile.mkdtemp(prefix=f"{CONTAINER_PREFIX}-gitview-"))
        try:
            mounts = git_view_mounts(workspace, view_dir)
        except Exception:
            _remove_quietly(view_dir)
            raise

    class TaskDockerCommandExecutor(DockerCommandExecutor):
        """The Aqours Docker executor; its container also gets the task's options."""

        def docker_run_args(self) -> list[str]:
            return task_run_args(super().docker_run_args(), self.image, pairs, nofile, mounts)

        @property
        def resource_limits(self) -> dict:
            limits = dict(super().resource_limits)
            if nofile is not None:
                limits["nofile"] = f"{nofile}:{nofile}"
            return limits

    def factory(index: int):
        container = name if index == 0 else f"{name}-r{index}"
        return TaskDockerCommandExecutor(
            workspace=workspace, image=image, case_name=name, container_name=container,
            memory=CONTAINER_MEMORY, cpus=CONTAINER_CPUS,
            pids_limit=CONTAINER_PIDS if pids is None else pids,
            command_timeout=command_timeout, docker_timeout=DOCKER_TIMEOUT_S,
            operation_deadline=deadline, runner=utf8_run)

    return RestartingDockerExecutor(factory, deadline=deadline,
                                    cleanup=[view_dir] if view_dir else [])
