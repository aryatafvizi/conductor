"""Tests for cli.py and __init__.py — 100% coverage."""
from __future__ import annotations
import sys, os, json, time, tempfile, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import pytest
from unittest.mock import patch, MagicMock
import db as db_mod
from models import Task, TaskStatus, TaskPriority, AgentStatus


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


class TestInit:
    def test_version(self):
        from conductor import __version__
        assert __version__ == "0.1.0"


class TestCmdAdd:
    def test_cmd_add(self, capsys):
        from cli import cmd_add
        args = argparse.Namespace(
            title="Test task", description="desc", branch="b",
            priority="high", depends_on=None)
        cmd_add(args)
        out = capsys.readouterr().out
        assert "Test task" in out


class TestCmdList:
    def test_cmd_list_empty(self, capsys):
        from cli import cmd_list
        args = argparse.Namespace(status=None)
        cmd_list(args)
        out = capsys.readouterr().out
        assert "No tasks" in out

    def test_cmd_list_with_tasks(self, capsys):
        import task_manager as tm
        tm.add_task(title="T1")
        from cli import cmd_list
        args = argparse.Namespace(status=None)
        cmd_list(args)
        out = capsys.readouterr().out
        assert "T1" in out

    def test_cmd_list_with_status_filter(self, capsys):
        import task_manager as tm
        tm.add_task(title="T1")
        from cli import cmd_list
        args = argparse.Namespace(status="ready")
        cmd_list(args)
        out = capsys.readouterr().out
        assert "T1" in out


class TestCmdDone:
    def test_cmd_done_success(self, capsys):
        import task_manager as tm
        t = tm.add_task(title="T")
        t = tm.transition(t, TaskStatus.RUNNING)
        from cli import cmd_done
        args = argparse.Namespace(task_id=t.id)
        cmd_done(args)
        out = capsys.readouterr().out
        assert "done" in out.lower()

    def test_cmd_done_not_found(self, capsys):
        from cli import cmd_done
        args = argparse.Namespace(task_id=999)
        cmd_done(args)
        out = capsys.readouterr().out
        assert "not found" in out.lower()


class TestCmdCancel:
    def test_cmd_cancel_success(self, capsys):
        import task_manager as tm
        t = tm.add_task(title="T")
        from cli import cmd_cancel
        args = argparse.Namespace(task_id=t.id)
        cmd_cancel(args)
        out = capsys.readouterr().out
        assert "cancelled" in out.lower()

    def test_cmd_cancel_not_found(self, capsys):
        from cli import cmd_cancel
        args = argparse.Namespace(task_id=999)
        cmd_cancel(args)
        out = capsys.readouterr().out
        assert "not found" in out.lower()


class TestCmdStop:
    def test_cmd_stop_no_pid(self, capsys):
        from cli import cmd_stop, PID_FILE
        if PID_FILE.exists():
            PID_FILE.unlink()
        cmd_stop()
        out = capsys.readouterr().out
        assert "not running" in out.lower() or "no pid" in out.lower()

    def test_cmd_stop_with_pid(self, capsys):
        from cli import cmd_stop, PID_FILE, CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text("99999")
        with patch("os.kill", side_effect=ProcessLookupError):
            cmd_stop()
        out = capsys.readouterr().out
        assert "not running" in out.lower()


class TestCmdKill:
    @patch("cli._api_post")
    def test_cmd_kill_specific(self, mock_api, capsys):
        mock_api.return_value = {}
        from cli import cmd_kill
        args = argparse.Namespace(agent_id="a1")
        cmd_kill(args)
        out = capsys.readouterr().out
        assert "a1" in out

    @patch("cli._api_post")
    def test_cmd_kill_all(self, mock_api, capsys):
        mock_api.return_value = {}
        from cli import cmd_kill
        args = argparse.Namespace(agent_id=None)
        cmd_kill(args)
        out = capsys.readouterr().out
        assert "all" in out.lower()


class TestCmdRollback:
    @patch("cli.WorkspaceManager")
    def test_cmd_rollback_not_found(self, MockWM, capsys):
        wm = MagicMock()
        wm.workspaces = {}
        MockWM.return_value = wm
        from cli import cmd_rollback
        args = argparse.Namespace(workspace="nope")
        cmd_rollback(args)
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    @patch("cli.WorkspaceManager")
    def test_cmd_rollback_ok(self, MockWM, capsys):
        wm = MagicMock()
        wm.workspaces = {"ws1": True}
        wm.rollback.return_value = True
        MockWM.return_value = wm
        from cli import cmd_rollback
        args = argparse.Namespace(workspace="ws1")
        cmd_rollback(args)
        out = capsys.readouterr().out
        assert "rolled back" in out.lower()

    @patch("cli.WorkspaceManager")
    def test_cmd_rollback_no_snapshot(self, MockWM, capsys):
        wm = MagicMock()
        wm.workspaces = {"ws1": True}
        wm.rollback.return_value = False
        MockWM.return_value = wm
        from cli import cmd_rollback
        args = argparse.Namespace(workspace="ws1")
        cmd_rollback(args)
        out = capsys.readouterr().out
        assert "no snapshot" in out.lower()


class TestCmdQuota:
    def test_cmd_quota(self, capsys):
        from cli import cmd_quota
        cmd_quota()
        out = capsys.readouterr().out
        assert "Quota" in out


class TestCmdPr:
    def test_cmd_pr_status_empty(self, capsys):
        from cli import cmd_pr
        args = argparse.Namespace(pr_action="status")
        cmd_pr(args)
        out = capsys.readouterr().out
        assert "No PR" in out

    def test_cmd_pr_create(self, capsys):
        from cli import cmd_pr
        args = argparse.Namespace(pr_action="create")
        cmd_pr(args)
        out = capsys.readouterr().out
        assert "dashboard" in out.lower()


class TestCmdBatch:
    @patch("cli.WorkspaceManager")
    def test_cmd_batch(self, MockWM, capsys):
        ws = MagicMock()
        ws.path = "/tmp"
        wm = MagicMock()
        wm.workspaces = {"ws1": ws}
        MockWM.return_value = wm
        from cli import cmd_batch
        args = argparse.Namespace(batch_cmd="echo hi", workspaces=None)

        with patch("subprocess.run") as mock_run:
            result = MagicMock()
            result.stdout = "hello\n"
            result.returncode = 0
            result.stderr = ""
            mock_run.return_value = result
            cmd_batch(args)

        out = capsys.readouterr().out
        assert "ws1" in out

    @patch("cli.WorkspaceManager")
    def test_cmd_batch_specific_workspaces(self, MockWM, capsys):
        wm = MagicMock()
        wm.workspaces = {}
        MockWM.return_value = wm
        from cli import cmd_batch
        args = argparse.Namespace(batch_cmd="echo hi", workspaces="ws1,ws2")
        cmd_batch(args)
        out = capsys.readouterr().out
        assert "not found" in out.lower()


class TestCmdLogs:
    def test_cmd_logs_no_entries(self, capsys):
        from cli import cmd_logs
        with patch("cli.tail_system_log", return_value=[]):
            args = argparse.Namespace(task_id=None, level="", since=0,
                                      search="", tail=50)
            cmd_logs(args)
        out = capsys.readouterr().out
        assert "No log" in out

    def test_cmd_logs_with_entries(self, capsys):
        from cli import cmd_logs
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "level": "INFO",
             "component": "test", "event": "e1"},
            "raw string entry",
        ]
        with patch("cli.tail_system_log", return_value=entries):
            args = argparse.Namespace(task_id=None, level="", since=0,
                                      search="", tail=50)
            cmd_logs(args)
        out = capsys.readouterr().out
        assert "e1" in out

    def test_cmd_logs_search(self, capsys):
        from cli import cmd_logs
        entries = [{"ts": "2026-01-01T00:00:00Z", "level": "WARN",
                    "component": "c", "event": "e"}]
        with patch("cli.search_logs", return_value=entries):
            args = argparse.Namespace(task_id=None, level="WARN", since=0,
                                      search="", tail=50)
            cmd_logs(args)
        out = capsys.readouterr().out
        assert "WARN" in out

    def test_cmd_logs_task_session(self, capsys):
        from cli import cmd_logs
        session = {
            "summary": {"status": "done", "duration_s": 30,
                        "files_changed": 2, "lines_changed": 50,
                        "request_count": 5},
            "timeline": [{"elapsed_s": 1, "event": "started"}],
        }
        with patch("cli.get_session_log", return_value=session):
            args = argparse.Namespace(task_id=1, level="", since=0,
                                      search="", tail=50)
            cmd_logs(args)
        out = capsys.readouterr().out
        assert "Task #1" in out

    def test_cmd_logs_task_error(self, capsys):
        from cli import cmd_logs
        with patch("cli.get_session_log", return_value={"error": "not found"}):
            args = argparse.Namespace(task_id=999, level="", since=0,
                                      search="", tail=50)
            cmd_logs(args)
        out = capsys.readouterr().out
        assert "not found" in out.lower()


class TestCmdLogsExport:
    def test_logs_export_days(self, capsys, tmp_path):
        from cli import cmd_logs_export
        with patch("cli.search_logs", return_value=[{"event": "e"}]):
            with patch("cli.Path") as MockPath:
                mp = MagicMock()
                MockPath.return_value = mp
                args = argparse.Namespace(last="7d")
                cmd_logs_export(args)
        out = capsys.readouterr().out
        assert "Exported" in out

    def test_logs_export_hours(self, capsys, tmp_path):
        from cli import cmd_logs_export
        with patch("cli.search_logs", return_value=[]):
            with patch("cli.Path") as MockPath:
                MockPath.return_value = MagicMock()
                args = argparse.Namespace(last="24h")
                cmd_logs_export(args)
        out = capsys.readouterr().out
        assert "Exported" in out


class TestCmdRules:
    def test_cmd_rules_empty(self, capsys):
        from cli import cmd_rules
        with patch("rules_engine.RulesEngine") as MockRE:
            MockRE.return_value.rules = []
            args = argparse.Namespace(rules_action="list")
            cmd_rules(args)
        out = capsys.readouterr().out
        assert "No rules" in out

    def test_cmd_rules_with_rules(self, capsys):
        from cli import cmd_rules
        from models import Rule
        r = Rule(name="r1", trigger_type="ci_failure", trigger_pattern="lint",
                 action_type="create_task", action_template="Fix lint", enabled=True)
        with patch("rules_engine.RulesEngine") as MockRE:
            MockRE.return_value.rules = [r]
            args = argparse.Namespace(rules_action="list")
            cmd_rules(args)
        out = capsys.readouterr().out
        assert "r1" in out


class TestCmdAgents:
    def test_cmd_agents_none(self, capsys):
        from cli import cmd_agents
        cmd_agents()
        out = capsys.readouterr().out
        assert "No agents" in out


class TestApiPost:
    def test_api_post_error(self):
        from cli import _api_post
        result = _api_post("/api/nonexistent")
        assert "error" in result


class TestCmdWatch:
    def test_cmd_watch_no_repo(self, capsys):
        from cli import cmd_watch, CONFIG_DIR
        args = argparse.Namespace(once=True)
        with patch("cli.CONFIG_DIR", Path("/tmp/nonexistent_conductor_test")):
            cmd_watch(args)
        out = capsys.readouterr().out
        assert "No repo" in out or "not" in out.lower()


class TestMainDispatch:
    def test_main_no_command(self, capsys):
        from cli import main
        with patch("sys.argv", ["con"]):
            main()
        out = capsys.readouterr().out
        assert "Conductor" in out
