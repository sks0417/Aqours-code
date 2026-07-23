from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable choices for one Agent runtime."""

    model_provider: str
    model: str
    primary_model: str
    fallback_model: str | None = None
    tool_policy: dict | None = None
    approval_mode: str = "interactive"
    background_tasks_enabled: bool = True


@dataclass(frozen=True)
class RuntimePaths:
    """Every filesystem location owned or consumed by one runtime."""

    workdir: Path
    state_root: Path
    skills_dir: Path
    transcript_dir: Path
    tool_results_dir: Path
    memory_dir: Path
    memory_index: Path
    mailbox_dir: Path
    tasks_dir: Path
    worktrees_dir: Path
    durable_path: Path
    once_durable_path: Path

    @classmethod
    def create(
        cls,
        workdir: str | Path,
        state_root: str | Path | None = None,
    ) -> "RuntimePaths":
        workspace = Path(workdir).resolve()
        root = Path(state_root).resolve() if state_root else workspace
        return cls(
            workdir=workspace,
            state_root=root,
            skills_dir=root / "skills",
            transcript_dir=root / ".transcripts",
            tool_results_dir=root / ".task_outputs" / "tool-results",
            memory_dir=root / ".memory",
            memory_index=root / ".memory" / "MEMORY.md",
            mailbox_dir=root / ".mailboxes",
            tasks_dir=root / ".tasks",
            worktrees_dir=root / ".worktrees",
            durable_path=root / ".scheduled_tasks.json",
            once_durable_path=root / ".scheduled_once_tasks.json",
        )


@dataclass
class RunState:
    """Mutable state that belongs to one task/run rather than to a module."""

    root_task: str = ""
    deadline: float | None = None
    todos: list[dict] = field(default_factory=list)
    changed_files: set[str] = field(default_factory=set)
    lead_read_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeServices:
    """External capabilities used by a runtime."""

    model_client: Any
    command_executor: Any
    trace_recorder: Any = None


@dataclass
class AgentRuntime:
    """Explicit ownership boundary for one Agent execution.

    Legacy modules may still mirror a subset of these values while the project
    migrates away from ``runtime_state``. New core code should accept this
    object explicitly and must not discover per-run state through module
    globals when a runtime is available.
    """

    config: RuntimeConfig
    paths: RuntimePaths
    state: RunState
    services: RuntimeServices

    @classmethod
    def create(
        cls,
        *,
        workdir: str | Path,
        model_client: Any,
        command_executor: Any,
        model_provider: str,
        model: str,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        tool_policy: dict | None = None,
        approval_mode: str = "interactive",
        background_tasks_enabled: bool = True,
        root_task: str = "",
        deadline: float | None = None,
        state_root: str | Path | None = None,
    ) -> "AgentRuntime":
        return cls(
            config=RuntimeConfig(
                model_provider=model_provider,
                model=model,
                primary_model=primary_model or model,
                fallback_model=fallback_model,
                tool_policy=tool_policy,
                approval_mode=approval_mode,
                background_tasks_enabled=background_tasks_enabled,
            ),
            paths=RuntimePaths.create(workdir, state_root),
            state=RunState(root_task=root_task, deadline=deadline),
            services=RuntimeServices(
                model_client=model_client,
                command_executor=command_executor,
            ),
        )

    def child(
        self,
        *,
        workdir: str | Path | None = None,
        root_task: str | None = None,
        tool_policy: dict | None = None,
    ) -> "AgentRuntime":
        """Create an isolated child state while sharing bounded services.

        Role-specific limits are still enforced by the current role runtime;
        this method only establishes the ownership semantics needed for their
        later migration.
        """
        child_workdir = Path(workdir).resolve() if workdir else self.paths.workdir
        return AgentRuntime.create(
            workdir=child_workdir,
            state_root=self.paths.state_root,
            model_client=self.services.model_client,
            command_executor=self.services.command_executor,
            model_provider=self.config.model_provider,
            model=self.config.model,
            primary_model=self.config.primary_model,
            fallback_model=self.config.fallback_model,
            tool_policy=(self.config.tool_policy
                         if tool_policy is None else tool_policy),
            approval_mode=self.config.approval_mode,
            background_tasks_enabled=self.config.background_tasks_enabled,
            root_task=(self.state.root_task if root_task is None else root_task),
            deadline=self.state.deadline,
        )
