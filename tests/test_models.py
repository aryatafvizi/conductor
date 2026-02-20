"""Tests for models.py — 100% coverage."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

from models import (
    Agent, AgentStatus, BlockReason, Pipeline, PipelineStatus,
    PRLifecycle, PRStage, QuotaStatus, Rule, Task, TaskPriority,
    TaskStatus, Workspace, WorkspaceStatus, PRIORITY_ORDER,
)


class TestTask:
    def test_defaults(self):
        t = Task()
        assert t.status == TaskStatus.PENDING
        assert t.priority == TaskPriority.NORMAL
        assert t.depends_on == []
        assert t.metadata == {}

    def test_to_dict(self):
        t = Task(id=1, title="T", branch="b", pr_number=42)
        d = t.to_dict()
        assert d["id"] == 1
        assert d["branch"] == "b"
        assert d["pr_number"] == 42
        assert d["status"] == "pending"


class TestAgent:
    def test_to_dict(self):
        a = Agent(id="a1", task_id=1, workspace="w1")
        d = a.to_dict()
        assert d["id"] == "a1"
        assert d["output_tail"] == []

    def test_to_dict_with_output(self):
        a = Agent(id="a1", task_id=1, workspace="w1",
                  output_lines=["line"] * 30)
        d = a.to_dict()
        assert len(d["output_tail"]) == 20


class TestWorkspace:
    def test_to_dict(self):
        ws = Workspace(name="ws1", path="/tmp/ws1")
        d = ws.to_dict()
        assert d["name"] == "ws1"
        assert d["status"] == "free"
        assert d["has_stash"] is False


class TestPipeline:
    def test_to_dict(self):
        p = Pipeline(id=1, name="pipe1", total_steps=3)
        d = p.to_dict()
        assert d["name"] == "pipe1"
        assert d["status"] == "active"
        assert d["total_steps"] == 3


class TestPRLifecycle:
    def test_to_dict(self):
        prl = PRLifecycle(id=1, title="Fix bug", branch="fix/bug")
        d = prl.to_dict()
        assert d["title"] == "Fix bug"
        assert d["stage"] == "planning"
        assert d["iteration"] == 0


class TestRule:
    def test_to_dict(self):
        r = Rule(name="r1", trigger_type="ci_failure", action_type="create_task",
                 action_template="Fix {check_name}")
        d = r.to_dict()
        assert d["name"] == "r1"
        assert d["action_priority"] == "normal"
        assert d["enabled"] is True


class TestQuotaStatus:
    def test_zero_limits(self):
        qs = QuotaStatus(agent_requests_limit=0, prompts_limit=0)
        assert qs.agent_pct == 0
        assert qs.prompt_pct == 0

    def test_to_dict(self):
        qs = QuotaStatus(agent_requests_used=50, agent_requests_limit=200,
                         prompts_used=100, prompts_limit=1500)
        d = qs.to_dict()
        assert d["agent_pct"] == 25.0
        assert d["is_paused"] is False


class TestEnums:
    def test_task_status_values(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.CANCELLED.value == "cancelled"

    def test_block_reason(self):
        assert BlockReason.DEPENDENCY.value == "dependency"
        assert BlockReason.QUOTA_EXHAUSTED.value == "quota_exhausted"

    def test_priority_order(self):
        assert PRIORITY_ORDER[TaskPriority.CRITICAL] < PRIORITY_ORDER[TaskPriority.LOW]

    def test_pr_stages(self):
        assert PRStage.CODING.value == "coding"
        assert PRStage.MERGED.value == "merged"

    def test_pipeline_status(self):
        assert PipelineStatus.ACTIVE.value == "active"

    def test_workspace_status(self):
        assert WorkspaceStatus.BUSY.value == "busy"

    def test_agent_status(self):
        assert AgentStatus.KILLED.value == "killed"
        assert AgentStatus.PAUSED.value == "paused"
