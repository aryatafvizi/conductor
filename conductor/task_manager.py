"""Task Manager — state machine, dependency resolution, priority queue."""
from __future__ import annotations

import time
from typing import Any

import db
from logger import log_event
from models import (
    PRIORITY_ORDER, BlockReason, Task, TaskPriority, TaskStatus,
)


def add_task(
    title: str,
    description: str = "",
    priority: str = "normal",
    branch: str = "",
    depends_on: list[int] | None = None,
    pr_number: int | None = None,
    pipeline_id: int | None = None,
    pipeline_step: int = 0,
    metadata: dict[str, Any] | None = None,
) -> Task:
    """Create a new task with proper initial state."""
    dep_ids = depends_on or []

    # Determine initial status
    if dep_ids:
        # Check if all dependencies are done
        all_done = all(
            (t := db.get_task(d)) and t.status == TaskStatus.DONE
            for d in dep_ids
        )
        status = TaskStatus.READY if all_done else TaskStatus.BLOCKED
        block_reason = "" if all_done else BlockReason.DEPENDENCY.value
    else:
        status = TaskStatus.READY
        block_reason = ""

    task = Task(
        title=title,
        description=description,
        status=status,
        priority=TaskPriority(priority),
        branch=branch,
        depends_on=dep_ids,
        block_reason=block_reason,
        pr_number=pr_number,
        pipeline_id=pipeline_id,
        pipeline_step=pipeline_step,
        metadata=metadata or {},
        created_at=time.time(),
    )
    task = db.create_task(task)

    log_event("task_manager", "task_created",
              task_id=task.id, title=title, priority=priority, status=status.value)

    return task


def transition(task: Task, new_status: TaskStatus) -> Task:
    """Transition a task to a new status with validation."""
    valid = _valid_transitions()
    if new_status not in valid.get(task.status, set()):
        log_event("task_manager", "invalid_transition", level="WARN",
                  task_id=task.id, from_status=task.status.value,
                  to_status=new_status.value)
        raise ValueError(
            f"Cannot transition task {task.id} from {task.status.value} "
            f"to {new_status.value}"
        )

    old_status = task.status
    task.status = new_status

    if new_status == TaskStatus.RUNNING:
        task.started_at = time.time()
    elif new_status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task.completed_at = time.time()

    if new_status != TaskStatus.BLOCKED:
        task.block_reason = ""

    db.update_task(task)

    log_event("task_manager", "task_transitioned",
              task_id=task.id, from_status=old_status.value,
              to_status=new_status.value)

    # If task completed, unblock dependents
    if new_status == TaskStatus.DONE:
        _unblock_dependents(task.id)

    return task


def mark_blocked(task: Task, reason: BlockReason) -> Task:
    """Block a task with a specific reason."""
    task.status = TaskStatus.BLOCKED
    task.block_reason = reason.value
    db.update_task(task)
    log_event("task_manager", "task_blocked",
              task_id=task.id, reason=reason.value)
    return task


def retry_task(task: Task) -> Task | None:
    """Retry a failed task if retries remain."""
    if task.retry_count >= task.max_retries:
        log_event("task_manager", "max_retries_exceeded", level="WARN",
                  task_id=task.id, retries=task.retry_count)
        return None

    task.retry_count += 1
    task.status = TaskStatus.READY
    task.started_at = None
    task.completed_at = None
    task.workspace = ""
    db.update_task(task)

    log_event("task_manager", "task_retried",
              task_id=task.id, retry_count=task.retry_count)
    return task


def cancel_task(task_id: int) -> Task | None:
    """Cancel a task."""
    task = db.get_task(task_id)
    if task is None:
        return None
    if task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
        return task
    task.status = TaskStatus.CANCELLED
    task.completed_at = time.time()
    db.update_task(task)
    log_event("task_manager", "task_cancelled", task_id=task_id)
    return task


def get_ready_tasks() -> list[Task]:
    """Get tasks ready for execution, ordered by priority."""
    tasks = db.list_tasks(status=TaskStatus.READY)
    tasks.sort(key=lambda t: PRIORITY_ORDER.get(t.priority, 99))
    return tasks


def assign_workspace(task: Task, workspace_name: str) -> Task:
    """Assign a workspace to a task."""
    task.workspace = workspace_name
    db.update_task(task)
    log_event("task_manager", "workspace_assigned",
              task_id=task.id, workspace=workspace_name)
    return task


# ── Internal helpers ────────────────────────────────────────────────────────


def _unblock_dependents(completed_task_id: int) -> None:
    """When a task completes, check if any blocked tasks can be unblocked."""
    all_tasks = db.list_tasks(status=TaskStatus.BLOCKED)
    for task in all_tasks:
        if task.block_reason != BlockReason.DEPENDENCY.value:
            continue
        if completed_task_id not in task.depends_on:
            continue
        # Check if ALL dependencies are now done
        all_done = all(
            (dep := db.get_task(d)) is not None and dep.status == TaskStatus.DONE
            for d in task.depends_on
        )
        if all_done:
            task.status = TaskStatus.READY
            task.block_reason = ""
            db.update_task(task)
            log_event("task_manager", "task_unblocked",
                      task_id=task.id, unblocked_by=completed_task_id)


def _valid_transitions() -> dict[TaskStatus, set[TaskStatus]]:
    return {
        TaskStatus.PENDING: {TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.CANCELLED},
        TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
        TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
        TaskStatus.RUNNING: {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},  # retry → ready
        TaskStatus.DONE: set(),
        TaskStatus.CANCELLED: set(),
    }
