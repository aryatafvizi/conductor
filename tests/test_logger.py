"""Tests for logger.py — 100% coverage."""
from __future__ import annotations
import sys, json, time, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import logging
import pytest
from unittest.mock import patch


class TestJSONFormatter:
    def test_format(self):
        from logger import JSONFormatter
        fmt = JSONFormatter()
        logger = logging.getLogger("test_json_fmt")
        record = logger.makeRecord("test", logging.INFO, "", 0, "msg", (), None)
        record.component = "comp"
        record.event = "evt"
        record.extra_data = {"key": "val"}
        result = json.loads(fmt.format(record))
        assert result["component"] == "comp"
        assert result["event"] == "evt"
        assert result["key"] == "val"
        assert result["level"] == "INFO"

    def test_format_no_extra(self):
        from logger import JSONFormatter
        fmt = JSONFormatter()
        logger = logging.getLogger("test_json_fmt2")
        record = logger.makeRecord("test", logging.WARNING, "", 0, "msg", (), None)
        record.component = "c"
        record.event = "e"
        record.extra_data = {}
        result = json.loads(fmt.format(record))
        assert "key" not in result


class TestSetupSystemLogger:
    def test_setup(self):
        from logger import setup_system_logger, LOGS_DIR
        logger = setup_system_logger(level="DEBUG")
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 2


class TestLogEvent:
    def test_log_event(self):
        from logger import log_event, setup_system_logger
        setup_system_logger()
        # Should not raise
        log_event("test_comp", "test_event", level="INFO", extra="data")
        log_event("test_comp", "warning_event", level="WARN")


class TestSessionLogger:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Monkey-patch SESSIONS_DIR
        import logger as logger_mod
        self._orig = logger_mod.SESSIONS_DIR
        logger_mod.SESSIONS_DIR = Path(self.tmpdir) / "sessions"

    def teardown_method(self):
        import logger as logger_mod
        logger_mod.SESSIONS_DIR = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_session_logger_creates_dirs(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=99)
        assert sl.session_dir.exists()
        assert (sl.session_dir / "diffs").exists()

    def test_log_prompt(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=100)
        sl.log_prompt("test prompt")
        assert (sl.session_dir / "prompt.txt").read_text() == "test prompt"

    def test_log_agent_output(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=101)
        sl.log_agent_output("line1")
        sl.log_agent_output("line2")
        lines = (sl.session_dir / "agent_output.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["data"] == "line1"

    def test_log_command(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=102)
        sl.log_command("echo hi", "hi\n", 0, 0.5)
        data = json.loads((sl.session_dir / "commands.jsonl").read_text().strip())
        assert data["command"] == "echo hi"
        assert data["exit_code"] == 0

    def test_log_timeline_event(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=103)
        sl.log_timeline_event("spawned", pid=1234)
        data = json.loads((sl.session_dir / "timeline.jsonl").read_text().strip())
        assert data["event"] == "spawned"
        assert data["pid"] == 1234
        assert "elapsed_s" in data

    def test_log_pre_snapshot(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=104)
        sl.log_pre_snapshot("diff content")
        assert (sl.session_dir / "diffs" / "pre_snapshot.patch").read_text() == "diff content"

    def test_log_final_diff(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=105)
        sl.log_final_diff("final diff")
        assert (sl.session_dir / "diffs" / "final.patch").read_text() == "final diff"

    def test_log_files_changed(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=106)
        sl.log_files_changed([{"path": "a.py", "status": "modified"}])
        data = json.loads((sl.session_dir / "files_changed.json").read_text())
        assert data[0]["path"] == "a.py"

    def test_write_summary(self):
        from logger import SessionLogger
        sl = SessionLogger(task_id=107)
        sl.write_summary({"status": "completed", "files_changed": 3})
        data = json.loads((sl.session_dir / "summary.json").read_text())
        assert data["status"] == "completed"
        assert "duration_s" in data


class TestLogTaskSummary:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import logger as logger_mod
        self._orig_dir = logger_mod.LOGS_DIR
        self._orig_log = logger_mod.SUMMARY_LOG
        logger_mod.LOGS_DIR = Path(self.tmpdir)
        logger_mod.SUMMARY_LOG = Path(self.tmpdir) / "summaries.jsonl"

    def teardown_method(self):
        import logger as logger_mod
        logger_mod.LOGS_DIR = self._orig_dir
        logger_mod.SUMMARY_LOG = self._orig_log
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_task_summary(self):
        from logger import log_task_summary
        log_task_summary({"task_id": 1, "status": "completed"})
        import logger as logger_mod
        content = logger_mod.SUMMARY_LOG.read_text().strip()
        data = json.loads(content)
        assert data["task_id"] == 1
        assert "logged_at" in data


class TestLogReading:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import logger as logger_mod
        self._orig_log = logger_mod.SYSTEM_LOG
        self._orig_sessions = logger_mod.SESSIONS_DIR
        logger_mod.SYSTEM_LOG = Path(self.tmpdir) / "conductor.log"
        logger_mod.SESSIONS_DIR = Path(self.tmpdir) / "sessions"

    def teardown_method(self):
        import logger as logger_mod
        logger_mod.SYSTEM_LOG = self._orig_log
        logger_mod.SESSIONS_DIR = self._orig_sessions
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tail_system_log_no_file(self):
        from logger import tail_system_log
        assert tail_system_log() == []

    def test_tail_system_log_with_entries(self):
        from logger import tail_system_log
        import logger as logger_mod
        entries = [
            json.dumps({"ts": "2026-01-01T00:00:00", "level": "INFO",
                        "component": "test", "event": "e1"}),
            json.dumps({"ts": "2026-01-01T00:00:01", "level": "ERROR",
                        "component": "test", "event": "e2"}),
            "bad json line",
        ]
        logger_mod.SYSTEM_LOG.write_text("\n".join(entries))
        result = tail_system_log(10)
        assert len(result) == 3
        assert result[0]["event"] == "e1"
        assert "raw" in result[2]

    def test_search_logs_no_file(self):
        from logger import search_logs
        assert search_logs("test") == []

    def test_search_logs_with_filter(self):
        from logger import search_logs
        import logger as logger_mod
        entries = [
            json.dumps({"ts": "2026-02-19T10:00:00+00:00", "level": "INFO",
                        "component": "a", "event": "hello"}),
            json.dumps({"ts": "2026-02-19T10:00:00+00:00", "level": "ERROR",
                        "component": "b", "event": "world"}),
            "bad json",
        ]
        logger_mod.SYSTEM_LOG.write_text("\n".join(entries))
        # Search by query
        result = search_logs("hello")
        assert len(result) == 1
        # Filter by level
        result = search_logs("", level="ERROR")
        assert len(result) == 1
        assert result[0]["event"] == "world"

    def test_search_logs_since_hours(self):
        from logger import search_logs
        import logger as logger_mod
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        entries = [
            json.dumps({"ts": now, "level": "INFO", "component": "a", "event": "recent"}),
            json.dumps({"ts": "2020-01-01T00:00:00+00:00", "level": "INFO",
                        "component": "a", "event": "old"}),
            json.dumps({"ts": "invalid", "level": "INFO", "component": "a", "event": "bad_ts"}),
        ]
        logger_mod.SYSTEM_LOG.write_text("\n".join(entries))
        result = search_logs("", since_hours=1)
        assert len(result) == 1
        assert result[0]["event"] == "recent"

    def test_get_session_log_no_session(self):
        from logger import get_session_log
        result = get_session_log(999)
        assert "error" in result

    def test_get_session_log_with_data(self):
        from logger import get_session_log, SessionLogger
        import logger as logger_mod
        sl = SessionLogger(task_id=200)
        sl.log_prompt("prompt text")
        sl.log_timeline_event("started")
        sl.log_files_changed([{"path": "x.py"}])
        sl.write_summary({"status": "done"})
        result = get_session_log(200)
        assert result["task_id"] == 200
        assert result["prompt"] == "prompt text"
        assert len(result["timeline"]) == 1
        assert result["summary"]["status"] == "done"
        assert result["files_changed"][0]["path"] == "x.py"
