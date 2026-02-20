"""Tests for db.py — 100% coverage of CRUD operations."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import threading
import pytest
import db as db_mod
from models import (
    Agent, AgentStatus, Pipeline, PipelineStatus,
    PRLifecycle, PRStage, Task, TaskPriority, TaskStatus,
)


def _reset_db():
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


class TestDB:
    def setup_method(self):
        _reset_db()
        db_mod.init_db(":memory:")

    def teardown_method(self):
        _reset_db()

    # ── Task CRUD ──

    def test_create_and_get_task(self):
        t = Task(title="Test", description="desc", priority=TaskPriority.HIGH,
                 branch="feature/x", metadata={"key": "val"})
        t = db_mod.create_task(t)
        assert t.id is not None and t.id > 0
        got = db_mod.get_task(t.id)
        assert got is not None
        assert got.title == "Test"
        assert got.priority == TaskPriority.HIGH
        assert got.metadata == {"key": "val"}

    def test_get_task_missing(self):
        assert db_mod.get_task(999) is None

    def test_list_tasks_all(self):
        db_mod.create_task(Task(title="A"))
        db_mod.create_task(Task(title="B"))
        assert len(db_mod.list_tasks()) == 2

    def test_list_tasks_by_status(self):
        db_mod.create_task(Task(title="A", status=TaskStatus.READY))
        db_mod.create_task(Task(title="B", status=TaskStatus.BLOCKED))
        assert len(db_mod.list_tasks(status=TaskStatus.READY)) == 1

    def test_update_task(self):
        t = db_mod.create_task(Task(title="Old"))
        t.title = "New"
        t.status = TaskStatus.RUNNING
        db_mod.update_task(t)
        got = db_mod.get_task(t.id)
        assert got.title == "New"
        assert got.status == TaskStatus.RUNNING

    def test_delete_task(self):
        t = db_mod.create_task(Task(title="Del"))
        db_mod.delete_task(t.id)
        assert db_mod.get_task(t.id) is None

    # ── Pipeline CRUD ──

    def test_create_and_get_pipeline(self):
        p = Pipeline(name="pipe1", total_steps=5, task_ids=[1, 2])
        p = db_mod.create_pipeline(p)
        assert p.id > 0
        got = db_mod.get_pipeline(p.id)
        assert got.name == "pipe1"
        assert got.task_ids == [1, 2]

    def test_get_pipeline_missing(self):
        assert db_mod.get_pipeline(999) is None

    def test_update_pipeline(self):
        p = db_mod.create_pipeline(Pipeline(name="p"))
        p.status = PipelineStatus.COMPLETED
        p.current_step = 3
        db_mod.update_pipeline(p)
        got = db_mod.get_pipeline(p.id)
        assert got.status == PipelineStatus.COMPLETED
        assert got.current_step == 3

    # ── PR Lifecycle CRUD ──

    def test_create_and_get_pr_lifecycle(self):
        prl = PRLifecycle(title="Fix", branch="fix/x", stage=PRStage.CODING)
        prl = db_mod.create_pr_lifecycle(prl)
        assert prl.id > 0
        got = db_mod.get_pr_lifecycle(prl.id)
        assert got.title == "Fix"
        assert got.stage == PRStage.CODING

    def test_get_pr_lifecycle_missing(self):
        assert db_mod.get_pr_lifecycle(999) is None

    def test_list_pr_lifecycles(self):
        db_mod.create_pr_lifecycle(PRLifecycle(title="A"))
        db_mod.create_pr_lifecycle(PRLifecycle(title="B"))
        assert len(db_mod.list_pr_lifecycles()) == 2

    def test_update_pr_lifecycle(self):
        prl = db_mod.create_pr_lifecycle(PRLifecycle(title="T"))
        prl.pr_number = 42
        prl.stage = PRStage.CI_MONITORING
        db_mod.update_pr_lifecycle(prl)
        got = db_mod.get_pr_lifecycle(prl.id)
        assert got.pr_number == 42
        assert got.stage == PRStage.CI_MONITORING

    # ── Agent CRUD ──

    def test_save_and_get_agent(self):
        # Create task first to satisfy FK constraint
        t = db_mod.create_task(Task(title="T"))
        a = Agent(id="agent-1", task_id=t.id, workspace="ws1",
                  status=AgentStatus.RUNNING, pid=1234)
        db_mod.save_agent(a)
        got = db_mod.get_agent("agent-1")
        assert got is not None
        assert got.pid == 1234
        assert got.status == AgentStatus.RUNNING

    def test_get_agent_missing(self):
        assert db_mod.get_agent("nope") is None

    def test_list_agents(self):
        t1 = db_mod.create_task(Task(title="T1"))
        t2 = db_mod.create_task(Task(title="T2"))
        db_mod.save_agent(Agent(id="a1", task_id=t1.id, workspace="w",
                                status=AgentStatus.RUNNING))
        db_mod.save_agent(Agent(id="a2", task_id=t2.id, workspace="w",
                                status=AgentStatus.COMPLETED))
        assert len(db_mod.list_agents()) == 2
        assert len(db_mod.list_agents(status=AgentStatus.RUNNING)) == 1

    def test_save_agent_upsert(self):
        t = db_mod.create_task(Task(title="T"))
        a = Agent(id="a1", task_id=t.id, workspace="w", request_count=0)
        db_mod.save_agent(a)
        a.request_count = 10
        db_mod.save_agent(a)
        got = db_mod.get_agent("a1")
        assert got.request_count == 10

    # ── Quota ──

    def test_get_quota_usage_empty(self):
        used, prompts = db_mod.get_quota_usage("2026-01-01")
        assert used == 0 and prompts == 0

    def test_increment_quota(self):
        db_mod.increment_quota("2026-01-01", agent_requests=5, prompts=10)
        used, prompts = db_mod.get_quota_usage("2026-01-01")
        assert used == 5 and prompts == 10
        # Increment again
        db_mod.increment_quota("2026-01-01", agent_requests=3, prompts=2)
        used, prompts = db_mod.get_quota_usage("2026-01-01")
        assert used == 8 and prompts == 12
