"""Tests for agent_manager.py — 100% coverage."""
from __future__ import annotations
import sys, os, time, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import db as db_mod
from models import Agent, AgentStatus, Task, TaskStatus, TaskPriority, Workspace, WorkspaceStatus


def _reset_db():
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


@pytest.fixture(autouse=True)
def setup_db():
    _reset_db()
    db_mod.init_db(":memory:")
    yield
    _reset_db()


def _make_ws_mgr():
    """Create a minimal mock WorkspaceManager."""
    ws_mgr = MagicMock()
    ws = Workspace(name="ws1", path="/tmp/ws1", status=WorkspaceStatus.FREE)
    ws_mgr.workspaces = {"ws1": ws}
    ws_mgr.get_free_workspace.return_value = ws
    ws_mgr.assign.return_value = ws
    ws_mgr.snapshot.return_value = ("abc123", False)
    ws_mgr.checkout_branch.return_value = True
    ws_mgr.release.return_value = ws
    ws_mgr.rollback.return_value = True
    ws_mgr.get_diff_stats.return_value = {
        "files_changed": 1, "lines_changed": 10, "diff": "some diff"
    }
    ws_mgr.list_all.return_value = [{"name": "ws1"}]
    return ws_mgr


def _make_quota_mgr():
    from quota_manager import QuotaManager
    return QuotaManager()


def _make_guardrails():
    from guardrails import Guardrails, GuardrailConfig
    return Guardrails(GuardrailConfig())


def _make_agent_mgr():
    from agent_manager import AgentManager
    return AgentManager(
        workspace_mgr=_make_ws_mgr(),
        quota_mgr=_make_quota_mgr(),
        guardrails=_make_guardrails(),
    )


class TestAgentManagerInit:
    def test_init(self):
        mgr = _make_agent_mgr()
        assert mgr.agents == {}
        assert mgr._on_output is None
        assert mgr._on_status_change is None

    def test_init_with_callbacks(self):
        from agent_manager import AgentManager
        cb1, cb2 = MagicMock(), MagicMock()
        mgr = AgentManager(
            workspace_mgr=_make_ws_mgr(),
            quota_mgr=_make_quota_mgr(),
            guardrails=_make_guardrails(),
            on_output=cb1,
            on_status_change=cb2,
        )
        assert mgr._on_output is cb1
        assert mgr._on_status_change is cb2


class TestSpawnAgent:
    @patch("agent_manager.log_event")
    async def test_spawn_quota_blocked(self, mock_log):
        mgr = _make_agent_mgr()
        mgr.quota_mgr._paused = True

        task = db_mod.create_task(Task(title="T", status=TaskStatus.READY))
        result = await mgr.spawn_agent(task, "ws1")
        assert result is None

    @patch("agent_manager.log_event")
    async def test_spawn_branch_blocked(self, mock_log):
        mgr = _make_agent_mgr()
        mgr.guardrails.config.allowed_branches = ["main"]

        task = db_mod.create_task(Task(title="T", status=TaskStatus.READY,
                                       branch="forbidden"))
        result = await mgr.spawn_agent(task, "ws1")
        assert result is None

    @patch("agent_manager.log_event")
    @patch("agent_manager.SessionLogger")
    @patch("asyncio.create_subprocess_exec")
    async def test_spawn_success(self, mock_exec, mock_session_cls, mock_log):
        mgr = _make_agent_mgr()

        mock_proc = AsyncMock()
        mock_proc.pid = 1234
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.__aiter__ = lambda self: self
        mock_proc.stdout.__anext__ = AsyncMock(side_effect=StopAsyncIteration)
        mock_exec.return_value = mock_proc

        task = db_mod.create_task(Task(title="T", status=TaskStatus.READY,
                                       branch="feature/x"))

        # Prevent the background task from running
        with patch("asyncio.create_task"):
            agent = await mgr.spawn_agent(task, "ws1")

        assert agent is not None
        assert agent.workspace == "ws1"
        assert agent.status == AgentStatus.RUNNING

    @patch("agent_manager.log_event")
    @patch("agent_manager.SessionLogger")
    @patch("asyncio.create_subprocess_exec")
    async def test_spawn_no_branch(self, mock_exec, mock_session_cls, mock_log):
        mgr = _make_agent_mgr()

        mock_proc = AsyncMock()
        mock_proc.pid = 5678
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.__aiter__ = lambda self: self
        mock_proc.stdout.__anext__ = AsyncMock(side_effect=StopAsyncIteration)
        mock_exec.return_value = mock_proc

        task = db_mod.create_task(Task(title="T", status=TaskStatus.READY))

        with patch("asyncio.create_task"):
            agent = await mgr.spawn_agent(task, "ws1")
        assert agent is not None

    @patch("agent_manager.log_event")
    @patch("agent_manager.SessionLogger")
    @patch("asyncio.create_subprocess_exec", side_effect=OSError("not found"))
    async def test_spawn_failure(self, mock_exec, mock_session_cls, mock_log):
        mgr = _make_agent_mgr()
        task = db_mod.create_task(Task(title="T", status=TaskStatus.READY))
        agent = await mgr.spawn_agent(task, "ws1")
        assert agent is None


class TestHandleCompletion:
    @patch("agent_manager.log_event")
    @patch("agent_manager.log_task_summary")
    async def test_handle_completion_success(self, mock_summary, mock_log):
        mgr = _make_agent_mgr()

        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a1", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING, started_at=time.time())
        mgr.agents["a1"] = agent
        mgr._processes["a1"] = MagicMock()

        # Create a session logger mock
        from agent_manager import SessionLogger
        session = MagicMock()
        mgr._session_loggers["a1"] = session

        await mgr._handle_completion("a1", return_code=0)
        assert agent.status == AgentStatus.COMPLETED
        assert agent.completed_at is not None
        session.log_final_diff.assert_called_once()
        session.write_summary.assert_called_once()

    @patch("agent_manager.log_event")
    @patch("agent_manager.log_task_summary")
    async def test_handle_completion_failed(self, mock_summary, mock_log):
        mgr = _make_agent_mgr()
        # Enable auto-rollback
        mgr.guardrails.config.auto_rollback_on_failure = True

        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a2", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING, started_at=time.time())
        mgr.agents["a2"] = agent
        mgr._processes["a2"] = MagicMock()
        mgr._session_loggers["a2"] = MagicMock()

        await mgr._handle_completion("a2", return_code=1)
        assert agent.status == AgentStatus.FAILED
        mgr.workspace_mgr.rollback.assert_called_with("ws1")

    @patch("agent_manager.log_event")
    async def test_handle_completion_not_found(self, mock_log):
        mgr = _make_agent_mgr()
        # Should be a no-op
        await mgr._handle_completion("nonexistent", return_code=0)

    @patch("agent_manager.log_event")
    @patch("agent_manager.log_task_summary")
    async def test_handle_completion_with_callbacks(self, mock_summary, mock_log):
        from agent_manager import AgentManager
        on_status = MagicMock()
        mgr = AgentManager(
            workspace_mgr=_make_ws_mgr(),
            quota_mgr=_make_quota_mgr(),
            guardrails=_make_guardrails(),
            on_status_change=on_status,
        )

        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a3", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING, started_at=time.time())
        mgr.agents["a3"] = agent
        mgr._session_loggers["a3"] = MagicMock()

        await mgr._handle_completion("a3", return_code=0)
        on_status.assert_called_once_with(agent)


class TestKillAgent:
    @patch("agent_manager.log_event")
    async def test_kill_not_found(self, mock_log):
        mgr = _make_agent_mgr()
        result = await mgr.kill_agent("nonexistent")
        assert result is False

    @patch("agent_manager.log_event")
    async def test_kill_success(self, mock_log):
        mgr = _make_agent_mgr()
        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a1", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING)
        mgr.agents["a1"] = agent

        mock_proc = AsyncMock()
        mock_proc.wait.return_value = 0
        mgr._processes["a1"] = mock_proc

        result = await mgr.kill_agent("a1")
        assert result is True
        assert agent.status == AgentStatus.KILLED
        mock_proc.send_signal.assert_called_once()

    @patch("agent_manager.log_event")
    async def test_kill_timeout(self, mock_log):
        mgr = _make_agent_mgr()
        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a1", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING)
        mgr.agents["a1"] = agent

        mock_proc = AsyncMock()
        mock_proc.wait.side_effect = asyncio.TimeoutError
        mgr._processes["a1"] = mock_proc

        result = await mgr.kill_agent("a1")
        assert result is True
        mock_proc.kill.assert_called_once()

    @patch("agent_manager.log_event")
    async def test_kill_exception(self, mock_log):
        mgr = _make_agent_mgr()
        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))
        agent = Agent(id="a1", task_id=task.id, workspace="ws1",
                      status=AgentStatus.RUNNING)
        mgr.agents["a1"] = agent

        mock_proc = MagicMock()
        mock_proc.send_signal.side_effect = Exception("fail")
        mgr._processes["a1"] = mock_proc

        result = await mgr.kill_agent("a1")
        assert result is False


class TestKillAll:
    @patch("agent_manager.log_event")
    async def test_kill_all(self, mock_log):
        mgr = _make_agent_mgr()
        task = db_mod.create_task(Task(title="T", status=TaskStatus.RUNNING))

        for i in range(3):
            agent = Agent(id=f"a{i}", task_id=task.id, workspace="ws1",
                          status=AgentStatus.RUNNING)
            mgr.agents[f"a{i}"] = agent
            mock_proc = AsyncMock()
            mock_proc.wait.return_value = 0
            mgr._processes[f"a{i}"] = mock_proc

        count = await mgr.kill_all()
        assert count == 3


class TestGetRunningAgents:
    def test_get_running(self):
        mgr = _make_agent_mgr()
        mgr.agents["a1"] = Agent(id="a1", task_id=1, workspace="ws1",
                                  status=AgentStatus.RUNNING)
        mgr.agents["a2"] = Agent(id="a2", task_id=2, workspace="ws1",
                                  status=AgentStatus.COMPLETED)
        mgr.agents["a3"] = Agent(id="a3", task_id=3, workspace="ws1",
                                  status=AgentStatus.STARTING)
        running = mgr.get_running_agents()
        assert len(running) == 2


class TestGetAgentOutput:
    def test_get_output(self):
        mgr = _make_agent_mgr()
        agent = Agent(id="a1", task_id=1, workspace="ws1",
                      status=AgentStatus.RUNNING)
        agent.output_lines = [f"line{i}" for i in range(100)]
        mgr.agents["a1"] = agent
        result = mgr.get_agent_output("a1", tail=10)
        assert len(result) == 10
        assert result[0] == "line90"

    def test_get_output_not_found(self):
        mgr = _make_agent_mgr()
        assert mgr.get_agent_output("nope") == []
