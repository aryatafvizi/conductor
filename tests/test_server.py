"""Tests for server.py — comprehensive coverage of REST API, internal funcs."""
from __future__ import annotations
import sys, json, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import db as db_mod
from models import Task, TaskStatus, TaskPriority


def _reset_db():
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


class TestWebSocketHub:
    def test_hub_init(self):
        _reset_db()
        db_mod.init_db(":memory:")
        from server import WebSocketHub
        hub = WebSocketHub()
        assert hub.connections == []

    def test_hub_disconnect_from_empty(self):
        _reset_db()
        db_mod.init_db(":memory:")
        from server import WebSocketHub
        hub = WebSocketHub()
        ws = MagicMock()
        hub.disconnect(ws)  # should not raise

    async def test_hub_connect(self):
        _reset_db()
        db_mod.init_db(":memory:")
        from server import WebSocketHub
        hub = WebSocketHub()
        ws = AsyncMock()
        await hub.connect(ws)
        assert ws in hub.connections
        ws.accept.assert_called_once()

    async def test_hub_broadcast(self):
        _reset_db()
        db_mod.init_db(":memory:")
        from server import WebSocketHub
        hub = WebSocketHub()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws2.send_text.side_effect = Exception("closed")
        hub.connections = [ws1, ws2]
        await hub.broadcast("test_event", {"key": "val"})
        ws1.send_text.assert_called_once()
        # ws2 should have been disconnected due to exception
        assert ws2 not in hub.connections

    def test_hub_disconnect_existing(self):
        _reset_db()
        db_mod.init_db(":memory:")
        from server import WebSocketHub
        hub = WebSocketHub()
        ws = MagicMock()
        hub.connections.append(ws)
        hub.disconnect(ws)
        assert ws not in hub.connections


class TestLoadConfig:
    def test_load_config_no_file(self):
        from server import load_config
        with patch("server.CONFIG_FILE") as mock_cf:
            mock_cf.exists.return_value = False
            result = load_config()
        assert result == {}


class TestServerAPIRoutes:
    """Test the API route functions directly using FastAPI TestClient."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    def _get_client(self):
        from server import app
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_get_tasks(self):
        client = self._get_client()
        r = client.get("/api/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_create_task(self):
        client = self._get_client()
        r = client.post("/api/tasks",
                        json={"title": "Test task", "priority": "normal"})
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "Test task"

    def test_cancel_task(self):
        client = self._get_client()
        r = client.post("/api/tasks", json={"title": "T"})
        task_id = r.json()["id"]
        r = client.post(f"/api/tasks/{task_id}/cancel")
        assert r.status_code == 200

    def test_get_agents(self):
        client = self._get_client()
        r = client.get("/api/agents")
        assert r.status_code == 200

    def test_kill_all_agents(self):
        client = self._get_client()
        r = client.post("/api/agents/kill-all")
        assert r.status_code == 200
        assert "killed" in r.json()

    def test_kill_agent(self):
        client = self._get_client()
        r = client.post("/api/agents/nonexistent/kill")
        assert r.status_code == 200

    def test_get_workspaces(self):
        client = self._get_client()
        r = client.get("/api/workspaces")
        assert r.status_code == 200

    @patch("server.workspace_mgr")
    def test_rollback_workspace(self, mock_ws_mgr):
        mock_ws_mgr.rollback.return_value = True
        client = self._get_client()
        r = client.post("/api/workspaces/test/rollback")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_get_quota(self):
        client = self._get_client()
        r = client.get("/api/quota")
        assert r.status_code == 200
        data = r.json()
        assert "agent_pct" in data

    def test_get_pr_lifecycles(self):
        client = self._get_client()
        r = client.get("/api/pr-lifecycles")
        assert r.status_code == 200

    def test_get_logs(self):
        client = self._get_client()
        r = client.get("/api/logs")
        assert r.status_code == 200

    def test_get_logs_with_search(self):
        client = self._get_client()
        r = client.get("/api/logs?search=test&level=INFO&since=1.0")
        assert r.status_code == 200

    def test_get_session_logs(self):
        client = self._get_client()
        r = client.get("/api/logs/session/999")
        assert r.status_code == 200

    def test_get_rules(self):
        client = self._get_client()
        r = client.get("/api/rules")
        assert r.status_code == 200

    def test_chat(self):
        client = self._get_client()
        r = client.post("/api/chat", json={
            "conversation_id": "test", "text": "hello"})
        assert r.status_code == 200

    def test_approve_plan_no_plan(self):
        client = self._get_client()
        r = client.post("/api/chat/approve",
                        json={"conversation_id": "nonexistent"})
        assert r.status_code == 200
        assert "error" in r.json()

    @patch("server.hub")
    @patch("server.pr_lifecycle_mgr")
    @patch("server.planner")
    def test_approve_plan_success(self, mock_planner, mock_prl_mgr, mock_hub):
        """Cover approve_plan success path (lines 371-391)."""
        mock_planner.extract_plan.return_value = {
            "title": "Fix bug",
            "branch": "fix/bug",
            "description": "desc",
        }
        mock_prl = MagicMock()
        mock_prl.id = "prl-1"
        mock_prl.to_dict.return_value = {"id": "prl-1"}
        mock_prl_mgr.start_lifecycle = AsyncMock(return_value=mock_prl)
        mock_hub.broadcast = AsyncMock()

        client = self._get_client()
        r = client.post("/api/chat/approve",
                        json={"conversation_id": "conv1"})
        assert r.status_code == 200
        data = r.json()
        assert data["plan"]["title"] == "Fix bug"
        assert "task_id" in data
        assert data["pr_lifecycle_id"] == "prl-1"

    def test_get_state(self):
        client = self._get_client()
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert "tasks" in data
        assert "agents" in data
        assert "workspaces" in data
        assert "quota" in data

    def test_dashboard(self):
        client = self._get_client()
        r = client.get("/")
        assert r.status_code == 200

    @patch("server.static_dir")
    def test_dashboard_with_index(self, mock_static):
        """Cover dashboard FileResponse path (line 188)."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            # Create a fake index.html
            idx = Path(td) / "index.html"
            idx.write_text("<h1>Test</h1>")
            mock_static.__truediv__ = lambda _, x: idx if x == "index.html" else Path(td) / x
            client = self._get_client()
            r = client.get("/")
            assert r.status_code == 200


class TestInternalFunctions:
    """Test internal async functions from server.py directly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    async def test_get_full_state(self):
        from server import _get_full_state
        state = await _get_full_state()
        assert "tasks" in state
        assert "agents" in state
        assert "workspaces" in state
        assert "quota" in state
        assert "pr_lifecycles" in state
        assert "logs" in state
        assert "rules" in state

    @patch("server.log_event")
    @patch("server.rules_engine")
    @patch("server.hub")
    async def test_handle_github_event(self, mock_hub, mock_rules, mock_log):
        from server import _handle_github_event
        mock_rules.evaluate.return_value = []
        mock_hub.broadcast = AsyncMock()

        await _handle_github_event({"type": "ci_success", "pr_number": 1})
        mock_log.assert_called()
        mock_hub.broadcast.assert_called()

    @patch("server.log_event")
    @patch("server.rules_engine")
    @patch("server.hub")
    @patch("server.tm")
    async def test_handle_github_event_create_task(self, mock_tm, mock_hub,
                                                     mock_rules, mock_log):
        from server import _handle_github_event
        mock_task = MagicMock()
        mock_task.to_dict.return_value = {"id": 1, "title": "Fix"}
        mock_tm.add_task.return_value = mock_task
        mock_hub.broadcast = AsyncMock()

        mock_rules.evaluate.return_value = [{
            "type": "create_task",
            "title": "Fix lint",
            "priority": "high",
            "rule_name": "ci_fix",
        }]

        await _handle_github_event({"type": "ci_failure"})
        mock_tm.add_task.assert_called_once()

    @patch("server.log_event")
    @patch("server.hub")
    @patch("server.agent_mgr")
    @patch("server.workspace_mgr")
    @patch("server.quota_mgr")
    @patch("server.tm")
    async def test_scheduler_loop_runs_task(self, mock_tm, mock_quota,
                                             mock_ws, mock_agent, mock_hub,
                                             mock_log):
        """Test the scheduler loop processes ready tasks."""
        from server import _scheduler_loop

        mock_task = MagicMock()
        mock_task.to_dict.return_value = {"id": 1}
        mock_agent_obj = MagicMock()
        mock_agent_obj.to_dict.return_value = {"id": "a1"}

        # First call: return a ready task; second call: raise to break loop
        call_count = 0
        def get_ready_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_task]
            raise asyncio.CancelledError  # Break the loop

        mock_tm.get_ready_tasks.side_effect = get_ready_side_effect
        mock_quota.check_reset.return_value = False
        mock_quota.can_start_agent.return_value = (True, "")
        mock_ws.get_free_workspace.return_value = MagicMock(name="ws1")
        mock_agent.spawn_agent = AsyncMock(return_value=mock_agent_obj)
        mock_hub.broadcast = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await _scheduler_loop()

    @patch("server.log_event")
    @patch("server.hub")
    @patch("server.quota_mgr")
    @patch("server.tm")
    async def test_scheduler_loop_quota_blocked(self, mock_tm, mock_quota,
                                                  mock_hub, mock_log):
        from server import _scheduler_loop

        mock_task = MagicMock()
        call_count = 0
        def get_ready_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_task]
            raise asyncio.CancelledError

        mock_tm.get_ready_tasks.side_effect = get_ready_side_effect
        mock_quota.check_reset.return_value = False
        mock_quota.can_start_agent.return_value = (False, "quota exhausted")

        with pytest.raises(asyncio.CancelledError):
            await _scheduler_loop()
        mock_tm.mark_blocked.assert_called_once()

    @patch("server.log_event")
    @patch("server.hub")
    @patch("server.workspace_mgr")
    @patch("server.quota_mgr")
    @patch("server.tm")
    async def test_scheduler_loop_no_workspace(self, mock_tm, mock_quota,
                                                 mock_ws, mock_hub, mock_log):
        from server import _scheduler_loop

        mock_task = MagicMock()
        call_count = 0
        def get_ready_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_task]
            raise asyncio.CancelledError

        mock_tm.get_ready_tasks.side_effect = get_ready_side_effect
        mock_quota.check_reset.return_value = False
        mock_quota.can_start_agent.return_value = (True, "")
        mock_ws.get_free_workspace.return_value = None

        with pytest.raises(asyncio.CancelledError):
            await _scheduler_loop()
        mock_tm.mark_blocked.assert_called_once()

    @patch("server.log_event")
    @patch("server.hub")
    @patch("server.agent_mgr")
    @patch("server.workspace_mgr")
    @patch("server.quota_mgr")
    @patch("server.tm")
    async def test_scheduler_loop_spawn_fails(self, mock_tm, mock_quota,
                                                mock_ws, mock_agent, mock_hub,
                                                mock_log):
        from server import _scheduler_loop

        mock_task = MagicMock()
        call_count = 0
        def get_ready_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_task]
            raise asyncio.CancelledError

        mock_tm.get_ready_tasks.side_effect = get_ready_side_effect
        mock_quota.check_reset.return_value = False
        mock_quota.can_start_agent.return_value = (True, "")
        mock_ws.get_free_workspace.return_value = MagicMock(name="ws1")
        mock_agent.spawn_agent = AsyncMock(return_value=None)  # spawn failed
        mock_hub.broadcast = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await _scheduler_loop()

    @patch("server.log_event")
    @patch("server.hub")
    @patch("server.tm")
    async def test_scheduler_loop_exception(self, mock_tm, mock_hub, mock_log):
        from server import _scheduler_loop

        call_count = 0
        def get_ready_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("scheduler boom")
            raise asyncio.CancelledError

        mock_tm.get_ready_tasks = MagicMock(side_effect=get_ready_side_effect)

        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            with pytest.raises(asyncio.CancelledError):
                await _scheduler_loop()


class TestLifespan:
    """Test the lifespan context manager."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    @patch("server.log_event")
    @patch("server.agent_mgr")
    @patch("server.github_monitor")
    @patch("server.github_cfg", {"repo": "owner/repo"})
    async def test_lifespan_with_github(self, mock_gh, mock_agent, mock_log):
        from server import lifespan, app, _bg_tasks
        mock_agent.kill_all = AsyncMock()
        mock_gh.start_polling = AsyncMock()

        # Clear bg tasks
        _bg_tasks.clear()

        async with lifespan(app):
            assert len(_bg_tasks) >= 1
        mock_agent.kill_all.assert_called_once()

    @patch("server.log_event")
    @patch("server.agent_mgr")
    @patch("server.github_cfg", {})
    async def test_lifespan_no_github(self, mock_agent, mock_log):
        from server import lifespan, app, _bg_tasks
        mock_agent.kill_all = AsyncMock()
        _bg_tasks.clear()

        async with lifespan(app):
            assert len(_bg_tasks) >= 1  # scheduler only
        mock_agent.kill_all.assert_called_once()


class TestWSMessageHandling:
    """Test _handle_ws_message directly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    @patch("server.planner")
    async def test_ws_chat(self, mock_planner):
        from server import _handle_ws_message
        ws = AsyncMock()
        mock_planner.chat = AsyncMock(return_value="reply")

        await _handle_ws_message(ws, {
            "action": "chat",
            "conversation_id": "c1",
            "text": "hello",
            "workspace": "",
            "workspace_path": "",
        })
        ws.send_text.assert_called_once()
        sent_data = json.loads(ws.send_text.call_args[0][0])
        assert sent_data["type"] == "chat_response"

    @patch("server.agent_mgr")
    async def test_ws_kill_agent(self, mock_mgr):
        from server import _handle_ws_message
        ws = AsyncMock()
        mock_mgr.kill_agent = AsyncMock()

        await _handle_ws_message(ws, {"action": "kill_agent", "agent_id": "a1"})
        mock_mgr.kill_agent.assert_called_with("a1")

    @patch("server.agent_mgr")
    async def test_ws_kill_all(self, mock_mgr):
        from server import _handle_ws_message
        ws = AsyncMock()
        mock_mgr.kill_all = AsyncMock()

        await _handle_ws_message(ws, {"action": "kill_all"})
        mock_mgr.kill_all.assert_called_once()

    @patch("server.workspace_mgr")
    async def test_ws_rollback(self, mock_ws):
        from server import _handle_ws_message
        ws = AsyncMock()

        await _handle_ws_message(ws, {"action": "rollback", "workspace": "ws1"})
        mock_ws.rollback.assert_called_with("ws1")

    async def test_ws_refresh(self):
        from server import _handle_ws_message
        ws = AsyncMock()

        with patch("server._get_full_state", AsyncMock(return_value={"tasks": []})):
            await _handle_ws_message(ws, {"action": "refresh"})
        ws.send_text.assert_called_once()


class TestWebSocketEndpoint:
    """Test the websocket endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    def test_websocket_connect_disconnect(self):
        from server import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            # Should receive init message
            data = ws.receive_json()
            assert data["type"] == "init"
            assert "data" in data

    def test_websocket_send_message(self):
        from server import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            init = ws.receive_json()
            assert init["type"] == "init"
            # Send a refresh action
            ws.send_json({"action": "refresh"})
            response = ws.receive_json()
            # The refresh handler sends type "init" (same as initial state)
            assert response["type"] == "init"
            assert "data" in response


class TestCallbackHelpers:
    """Test _on_agent_output and _on_agent_status."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_db()
        db_mod.init_db(":memory:")
        yield
        _reset_db()

    @patch("server.hub")
    def test_on_agent_output(self, mock_hub):
        from server import _on_agent_output
        mock_hub.broadcast = AsyncMock()
        with patch("asyncio.create_task"):
            _on_agent_output("a1", "output line")

    @patch("server.hub")
    def test_on_agent_status(self, mock_hub):
        from server import _on_agent_status
        from models import Agent, AgentStatus
        mock_hub.broadcast = AsyncMock()
        agent = Agent(id="a1", task_id=1, workspace="ws1",
                      status=AgentStatus.RUNNING)
        with patch("asyncio.create_task"):
            _on_agent_status(agent)

    @patch("server.hub")
    def test_on_prl_stage(self, mock_hub):
        from server import _on_prl_stage
        from models import PRLifecycle, PRStage
        mock_hub.broadcast = AsyncMock()
        prl = PRLifecycle(title="T", stage=PRStage.CODING)
        with patch("asyncio.create_task"):
            _on_prl_stage(prl)
