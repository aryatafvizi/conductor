"""Tests for task_manager.py — 100% coverage."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import pytest
import db as db_mod
from models import BlockReason, Task, TaskPriority, TaskStatus
import task_manager as tm


def _reset_db():
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


class TestTaskManager:
    def setup_method(self):
        _reset_db()
        db_mod.init_db(":memory:")

    def teardown_method(self):
        _reset_db()

    def test_add_task_basic(self):
        t = tm.add_task(title="Task1", priority="high", branch="b")
        assert t.id > 0
        assert t.status == TaskStatus.READY
        assert t.priority == TaskPriority.HIGH

    def test_add_task_with_metadata(self):
        t = tm.add_task(title="T", metadata={"k": "v"}, pr_number=42,
                        pipeline_id=1, pipeline_step=2)
        assert t.metadata == {"k": "v"}
        assert t.pr_number == 42

    def test_add_task_with_unmet_deps(self):
        t1 = tm.add_task(title="First")
        t2 = tm.add_task(title="Second", depends_on=[t1.id])
        assert t2.status == TaskStatus.BLOCKED
        assert t2.block_reason == BlockReason.DEPENDENCY.value

    def test_add_task_with_met_deps(self):
        t1 = tm.add_task(title="First")
        t1 = tm.transition(t1, TaskStatus.RUNNING)
        t1 = tm.transition(t1, TaskStatus.DONE)
        t2 = tm.add_task(title="Second", depends_on=[t1.id])
        assert t2.status == TaskStatus.READY

    def test_transition_valid(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        assert t.status == TaskStatus.RUNNING
        assert t.started_at is not None

    def test_transition_to_done_sets_completed(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.DONE)
        assert t.completed_at is not None

    def test_transition_to_failed(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.FAILED)
        assert t.status == TaskStatus.FAILED
        assert t.completed_at is not None

    def test_transition_to_cancelled(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.CANCELLED)
        assert t.status == TaskStatus.CANCELLED
        assert t.completed_at is not None

    def test_transition_invalid(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.DONE)
        with pytest.raises(ValueError):
            tm.transition(t, TaskStatus.RUNNING)

    def test_transition_clears_block_reason(self):
        t = tm.add_task(title="T")
        t = tm.mark_blocked(t, BlockReason.QUOTA_EXHAUSTED)
        t = tm.transition(t, TaskStatus.READY)
        assert t.block_reason == ""

    def test_unblock_dependents_on_done(self):
        t1 = tm.add_task(title="First")
        t2 = tm.add_task(title="Second", depends_on=[t1.id])
        assert t2.status == TaskStatus.BLOCKED
        t1 = tm.transition(t1, TaskStatus.RUNNING)
        t1 = tm.transition(t1, TaskStatus.DONE)
        t2_updated = db_mod.get_task(t2.id)
        assert t2_updated.status == TaskStatus.READY

    def test_mark_blocked(self):
        t = tm.add_task(title="T")
        t = tm.mark_blocked(t, BlockReason.NO_WORKSPACE)
        assert t.status == TaskStatus.BLOCKED
        assert t.block_reason == "no_workspace"

    def test_retry_task(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.FAILED)
        retried = tm.retry_task(t)
        assert retried is not None
        assert retried.status == TaskStatus.READY
        assert retried.retry_count == 1
        assert retried.started_at is None
        assert retried.workspace == ""

    def test_retry_task_max_exceeded(self):
        t = tm.add_task(title="T")
        t.max_retries = 0
        db_mod.update_task(t)
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.FAILED)
        assert tm.retry_task(t) is None

    def test_cancel_task(self):
        t = tm.add_task(title="T")
        cancelled = tm.cancel_task(t.id)
        assert cancelled.status == TaskStatus.CANCELLED

    def test_cancel_task_missing(self):
        assert tm.cancel_task(999) is None

    def test_cancel_already_done(self):
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        t = tm.transition(t, TaskStatus.DONE)
        result = tm.cancel_task(t.id)
        assert result.status == TaskStatus.DONE

    def test_get_ready_tasks_sorted(self):
        tm.add_task(title="Low", priority="low")
        tm.add_task(title="Critical", priority="critical")
        tm.add_task(title="Normal", priority="normal")
        ready = tm.get_ready_tasks()
        assert ready[0].priority == TaskPriority.CRITICAL
        assert ready[-1].priority == TaskPriority.LOW

    def test_assign_workspace(self):
        t = tm.add_task(title="T")
        t = tm.assign_workspace(t, "ws-1")
        assert t.workspace == "ws-1"
        got = db_mod.get_task(t.id)
        assert got.workspace == "ws-1"

    def test_unblock_skips_non_dependency_blocked(self):
        """Line 166: blocked task with non-dependency block reason is skipped."""
        t1 = tm.add_task(title="Dep")
        # Create a blocked task but with a non-dependency reason
        t2 = tm.add_task(title="Other")
        t2 = tm.mark_blocked(t2, BlockReason.NO_WORKSPACE)
        # Complete t1 — t2 should stay blocked (not a dependency block)
        t1 = tm.transition(t1, TaskStatus.RUNNING)
        t1 = tm.transition(t1, TaskStatus.DONE)
        t2_updated = db_mod.get_task(t2.id)
        assert t2_updated.status == TaskStatus.BLOCKED

    def test_unblock_skips_unrelated_dependency(self):
        """Line 168: blocked task that depends on different task is skipped."""
        t1 = tm.add_task(title="Task A")
        t2 = tm.add_task(title="Task B")
        # t3 depends on t2, not t1
        t3 = tm.add_task(title="Task C", depends_on=[t2.id])
        assert t3.status == TaskStatus.BLOCKED
        # Complete t1 — t3 should remain blocked (depends on t2)
        t1 = tm.transition(t1, TaskStatus.RUNNING)
        t1 = tm.transition(t1, TaskStatus.DONE)
        t3_updated = db_mod.get_task(t3.id)
        assert t3_updated.status == TaskStatus.BLOCKED

