"""Data models for Conductor."""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any

# ── Task ────────────────────────────────────────────────────────────────────


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_ORDER = {
    TaskPriority.CRITICAL: 0,
    TaskPriority.HIGH: 1,
    TaskPriority.NORMAL: 2,
    TaskPriority.LOW: 3,
}


class BlockReason(str, enum.Enum):
    DEPENDENCY = "dependency"
    QUOTA_EXHAUSTED = "quota_exhausted"
    NO_WORKSPACE = "no_workspace"


@dataclass
class Task:
    id: int = 0
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    branch: str = ""
    workspace: str = ""
    depends_on: list[int] = field(default_factory=list)
    block_reason: str = ""
    retry_count: int = 0
    max_retries: int = 2
    pipeline_id: int | None = None
    pipeline_step: int = 0
    pr_number: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "branch": self.branch,
            "workspace": self.workspace,
            "depends_on": self.depends_on,
            "block_reason": self.block_reason,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "pipeline_id": self.pipeline_id,
            "pipeline_step": self.pipeline_step,
            "pr_number": self.pr_number,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }


# ── Agent ───────────────────────────────────────────────────────────────────


class AgentStatus(str, enum.Enum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class Agent:
    id: str = ""
    task_id: int = 0
    workspace: str = ""
    pid: int | None = None
    status: AgentStatus = AgentStatus.STARTING
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    request_count: int = 0
    output_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "workspace": self.workspace,
            "pid": self.pid,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_count": self.request_count,
            "output_tail": self.output_lines[-20:] if self.output_lines else [],
        }


# ── Workspace ───────────────────────────────────────────────────────────────


class WorkspaceStatus(str, enum.Enum):
    FREE = "free"
    ASSIGNED = "assigned"
    BUSY = "busy"


@dataclass
class Workspace:
    name: str = ""
    path: str = ""
    status: WorkspaceStatus = WorkspaceStatus.FREE
    assigned_task_id: int | None = None
    agent_id: str | None = None
    branch: str = ""
    snapshot_sha: str = ""
    has_stash: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "status": self.status.value,
            "assigned_task_id": self.assigned_task_id,
            "agent_id": self.agent_id,
            "branch": self.branch,
            "snapshot_sha": self.snapshot_sha,
            "has_stash": self.has_stash,
        }


# ── Pipeline ────────────────────────────────────────────────────────────────


class PipelineStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Pipeline:
    id: int = 0
    name: str = ""
    status: PipelineStatus = PipelineStatus.ACTIVE
    current_step: int = 0
    total_steps: int = 0
    task_ids: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "task_ids": self.task_ids,
            "created_at": self.created_at,
        }


# ── PR Lifecycle ────────────────────────────────────────────────────────────


class PRStage(str, enum.Enum):
    PLANNING = "planning"
    CODING = "coding"
    PRECHECKS = "prechecks"
    PR_CREATED = "pr_created"
    CI_MONITORING = "ci_monitoring"
    CI_FIXING = "ci_fixing"
    GREPTILE_REVIEW = "greptile_review"
    ADDRESSING_COMMENTS = "addressing_comments"
    READY_FOR_REVIEW = "ready_for_review"
    NEEDS_HUMAN = "needs_human"
    MERGED = "merged"


@dataclass
class PRLifecycle:
    id: int = 0
    pr_number: int | None = None
    branch: str = ""
    title: str = ""
    stage: PRStage = PRStage.PLANNING
    iteration: int = 0
    max_iterations: int = 3
    ci_fix_count: int = 0
    precheck_retry_count: int = 0
    greptile_comments_total: int = 0
    greptile_comments_resolved: int = 0
    pipeline_id: int | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "title": self.title,
            "stage": self.stage.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "ci_fix_count": self.ci_fix_count,
            "precheck_retry_count": self.precheck_retry_count,
            "greptile_comments_total": self.greptile_comments_total,
            "greptile_comments_resolved": self.greptile_comments_resolved,
            "pipeline_id": self.pipeline_id,
            "created_at": self.created_at,
        }


# ── Rule ────────────────────────────────────────────────────────────────────


@dataclass
class Rule:
    name: str = ""
    trigger_type: str = ""
    trigger_pattern: str = ""
    trigger_source: str = ""
    action_type: str = ""
    action_template: str = ""
    action_priority: TaskPriority = TaskPriority.NORMAL
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger_type": self.trigger_type,
            "trigger_pattern": self.trigger_pattern,
            "trigger_source": self.trigger_source,
            "action_type": self.action_type,
            "action_template": self.action_template,
            "action_priority": self.action_priority.value,
            "enabled": self.enabled,
        }


# ── Quota ───────────────────────────────────────────────────────────────────


@dataclass
class QuotaStatus:
    agent_requests_used: int = 0
    agent_requests_limit: int = 200
    prompts_used: int = 0
    prompts_limit: int = 1500
    concurrent_agents: int = 0
    max_concurrent: int = 3
    is_paused: bool = False
    reset_at: float = 0.0

    @property
    def agent_pct(self) -> float:
        if self.agent_requests_limit == 0:
            return 0
        return (self.agent_requests_used / self.agent_requests_limit) * 100

    @property
    def prompt_pct(self) -> float:
        if self.prompts_limit == 0:
            return 0
        return (self.prompts_used / self.prompts_limit) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_requests_used": self.agent_requests_used,
            "agent_requests_limit": self.agent_requests_limit,
            "agent_pct": round(self.agent_pct, 1),
            "prompts_used": self.prompts_used,
            "prompts_limit": self.prompts_limit,
            "prompt_pct": round(self.prompt_pct, 1),
            "concurrent_agents": self.concurrent_agents,
            "max_concurrent": self.max_concurrent,
            "is_paused": self.is_paused,
            "reset_at": self.reset_at,
        }
