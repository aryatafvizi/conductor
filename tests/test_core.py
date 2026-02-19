"""Core tests for Conductor modules."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow imports from the conductor package directory
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import threading

import pytest


def _reset_db():
    """Reset thread-local DB connection between tests."""
    import db as db_mod
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


class TestModels:
    """Test core dataclass models."""

    def test_task_creation(self):
        from models import Task, TaskStatus, TaskPriority
        task = Task(title="Test task", description="A test")
        assert task.title == "Test task"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.NORMAL

    def test_task_to_dict(self):
        from models import Task
        task = Task(id=1, title="Test")
        d = task.to_dict()
        assert d["id"] == 1
        assert d["title"] == "Test"
        assert d["status"] == "pending"

    def test_workspace_creation(self):
        from models import Workspace, WorkspaceStatus
        ws = Workspace(name="workspace-1", path="/tmp/test")
        assert ws.status == WorkspaceStatus.FREE
        assert ws.name == "workspace-1"

    def test_agent_creation(self):
        from models import Agent, AgentStatus
        agent = Agent(id="agent-abc", task_id=1, workspace="workspace-1")
        assert agent.status == AgentStatus.STARTING
        assert agent.request_count == 0

    def test_quota_status(self):
        from models import QuotaStatus
        qs = QuotaStatus(
            agent_requests_used=50,
            agent_requests_limit=200,
            prompts_used=100,
            prompts_limit=1500,
        )
        assert qs.agent_pct == 25.0
        assert qs.prompt_pct == pytest.approx(6.67, rel=0.01)


class TestTaskManager:
    """Test task state machine transitions."""

    def setup_method(self):
        _reset_db()
        import db
        db.init_db(":memory:")

    def teardown_method(self):
        _reset_db()

    def test_add_task(self):
        import task_manager as tm
        task = tm.add_task(title="Test task", priority="high")
        assert task.id is not None
        assert task.status.value == "ready"

    def test_add_task_with_depends(self):
        import task_manager as tm
        t1 = tm.add_task(title="First")
        t2 = tm.add_task(title="Second", depends_on=[t1.id])
        assert t2.status.value == "blocked"
        assert t2.depends_on == [t1.id]

    def test_transition(self):
        import task_manager as tm
        from models import TaskStatus
        task = tm.add_task(title="Test")
        task = tm.transition(task, TaskStatus.RUNNING)
        assert task.status == TaskStatus.RUNNING

    def test_cancel(self):
        import task_manager as tm
        task = tm.add_task(title="Test")
        cancelled = tm.cancel_task(task.id)
        assert cancelled is not None
        assert cancelled.status.value == "cancelled"


class TestQuotaManager:
    """Test quota tracking."""

    def setup_method(self):
        _reset_db()
        import db
        db.init_db(":memory:")

    def teardown_method(self):
        _reset_db()

    def test_initial_state(self):
        from quota_manager import QuotaManager
        qm = QuotaManager()
        status = qm.get_status()
        assert status.agent_requests_used == 0
        assert not status.is_paused

    def test_can_start_agent(self):
        from quota_manager import QuotaManager
        qm = QuotaManager(daily_agent_requests=100, max_concurrent=2, reserve_requests=0)
        can, reason = qm.can_start_agent()
        assert can is True

    def test_pause_at_threshold(self):
        from quota_manager import QuotaManager
        qm = QuotaManager(daily_agent_requests=10, pause_at_percent=50, reserve_requests=0)
        for _ in range(6):
            qm.record_agent_request()
        can, reason = qm.can_start_agent()
        assert can is False

    def test_concurrent_limit(self):
        from quota_manager import QuotaManager
        qm = QuotaManager(max_concurrent=1)
        qm.agent_started()
        can, reason = qm.can_start_agent()
        assert can is False
        assert "concurrent" in reason.lower()


class TestGuardrails:
    """Test safety enforcement."""

    def test_branch_protection(self):
        from guardrails import Guardrails, GuardrailConfig
        g = Guardrails(GuardrailConfig({"protected_branches": ["main", "release/*"]}))
        assert g.check_branch_allowed("feature/test") is True
        assert g.check_branch_allowed("main") is False

    def test_path_check(self):
        from guardrails import Guardrails, GuardrailConfig
        g = Guardrails(GuardrailConfig({"blocked_paths": ["/home/user/.ssh"]}))
        check = g.check_path_allowed("/home/user/.ssh/id_rsa")
        assert check is False

    def test_output_scanning(self):
        from guardrails import Guardrails, GuardrailConfig
        g = Guardrails(GuardrailConfig())
        result = g.check_agent_output("git push --force origin main")
        assert result["should_kill"] is True

    def test_safe_output(self):
        from guardrails import Guardrails, GuardrailConfig
        g = Guardrails(GuardrailConfig())
        result = g.check_agent_output("echo hello world")
        assert result["should_kill"] is False


class TestRulesEngine:
    """Test YAML rules evaluation."""

    def test_no_rules_file(self):
        from rules_engine import RulesEngine
        from pathlib import Path
        engine = RulesEngine(rules_path=Path("/nonexistent/rules.yaml"))
        assert engine.rules == []

    def test_evaluate_no_match(self):
        from rules_engine import RulesEngine
        from pathlib import Path
        engine = RulesEngine(rules_path=Path("/nonexistent/rules.yaml"))
        actions = engine.evaluate({"type": "test"})
        assert actions == []
