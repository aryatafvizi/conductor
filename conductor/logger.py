"""Structured logging for Conductor.

Three layers:
1. System log  — ~/.conductor/logs/conductor.log (rotating JSON)
2. Session logs — ~/.conductor/logs/sessions/<task-id>/ (per-task deep record)
3. Summary log — ~/.conductor/logs/summaries.jsonl (one-line-per-task analytics)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGS_DIR = Path.home() / ".conductor" / "logs"
SYSTEM_LOG = LOGS_DIR / "conductor.log"
SUMMARY_LOG = LOGS_DIR / "summaries.jsonl"
SESSIONS_DIR = LOGS_DIR / "sessions"


class JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", record.getMessage()),
        }
        # Merge any extra fields
        extra = getattr(record, "extra_data", {})
        if extra:
            entry.update(extra)
        return json.dumps(entry, default=str)


def setup_system_logger(
    level: str = "INFO",
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the main conductor system logger with rotation."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("conductor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # Rotating file handler — structured JSON
    fh = logging.handlers.RotatingFileHandler(
        str(SYSTEM_LOG), maxBytes=max_bytes, backupCount=backup_count,
    )
    fh.setFormatter(JSONFormatter())
    logger.addHandler(fh)

    # Console handler — human-readable for development
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(ch)

    return logger


def log_event(
    component: str,
    event: str,
    level: str = "INFO",
    **kwargs: Any,
) -> None:
    """Log a structured event to the system log."""
    logger = logging.getLogger("conductor")
    record = logger.makeRecord(
        name="conductor",
        level=getattr(logging, level.upper(), logging.INFO),
        fn="",
        lno=0,
        msg=event,
        args=(),
        exc_info=None,
    )
    record.component = component  # type: ignore[attr-defined]
    record.event = event  # type: ignore[attr-defined]
    record.extra_data = kwargs  # type: ignore[attr-defined]
    logger.handle(record)


# ── Session Logs ────────────────────────────────────────────────────────────


class SessionLogger:
    """Per-task session logger — captures prompts, agent output, diffs, etc."""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.session_dir = SESSIONS_DIR / f"task-{task_id}"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "diffs").mkdir(exist_ok=True)
        self._start_time = time.time()

    def log_prompt(self, prompt: str) -> None:
        (self.session_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    def log_agent_output(self, line: str) -> None:
        with open(self.session_dir / "agent_output.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "data": line,
            }) + "\n")

    def log_command(
        self, command: str, output: str, exit_code: int, duration_s: float,
    ) -> None:
        with open(self.session_dir / "commands.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "command": command,
                "output": output[:5000],  # Cap output size
                "exit_code": exit_code,
                "duration_s": round(duration_s, 2),
            }) + "\n")

    def log_timeline_event(self, event: str, **data: Any) -> None:
        with open(self.session_dir / "timeline.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "elapsed_s": round(time.time() - self._start_time, 2),
                "event": event,
                **data,
            }) + "\n")

    def log_pre_snapshot(self, diff: str) -> None:
        (self.session_dir / "diffs" / "pre_snapshot.patch").write_text(
            diff, encoding="utf-8"
        )

    def log_final_diff(self, diff: str) -> None:
        (self.session_dir / "diffs" / "final.patch").write_text(
            diff, encoding="utf-8"
        )

    def log_files_changed(self, files: list[dict[str, str]]) -> None:
        (self.session_dir / "files_changed.json").write_text(
            json.dumps(files, indent=2), encoding="utf-8"
        )

    def write_summary(self, summary: dict[str, Any]) -> None:
        summary["duration_s"] = round(time.time() - self._start_time, 2)
        (self.session_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )


# ── Summary Log ─────────────────────────────────────────────────────────────


def log_task_summary(summary: dict[str, Any]) -> None:
    """Append a one-line task summary to summaries.jsonl for analytics."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    summary["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(SUMMARY_LOG, "a") as f:
        f.write(json.dumps(summary, default=str) + "\n")


# ── Log Reading ─────────────────────────────────────────────────────────────


def tail_system_log(n: int = 50) -> list[dict[str, Any]]:
    """Read the last N lines from the system log."""
    if not SYSTEM_LOG.exists():
        return []
    lines = SYSTEM_LOG.read_text().strip().split("\n")[-n:]
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            result.append({"raw": line})
    return result


def search_logs(
    query: str,
    level: str | None = None,
    since_hours: float | None = None,
) -> list[dict[str, Any]]:
    """Search system log for matching entries."""
    if not SYSTEM_LOG.exists():
        return []

    cutoff = None
    if since_hours:
        cutoff = time.time() - (since_hours * 3600)

    results = []
    for line in SYSTEM_LOG.read_text().strip().split("\n"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if level and entry.get("level", "").upper() != level.upper():
            continue
        if cutoff:
            ts = entry.get("ts", "")
            try:
                entry_time = datetime.fromisoformat(ts).timestamp()
                if entry_time < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
        if query.lower() in json.dumps(entry).lower():
            results.append(entry)
    return results


def get_session_log(task_id: int) -> dict[str, Any]:
    """Read full session log for a task."""
    session_dir = SESSIONS_DIR / f"task-{task_id}"
    if not session_dir.exists():
        return {"error": f"No session log for task {task_id}"}

    result: dict[str, Any] = {"task_id": task_id}

    prompt_file = session_dir / "prompt.txt"
    if prompt_file.exists():
        result["prompt"] = prompt_file.read_text()

    summary_file = session_dir / "summary.json"
    if summary_file.exists():
        result["summary"] = json.loads(summary_file.read_text())

    timeline_file = session_dir / "timeline.jsonl"
    if timeline_file.exists():
        result["timeline"] = [
            json.loads(line)
            for line in timeline_file.read_text().strip().split("\n")
            if line.strip()
        ]

    files_file = session_dir / "files_changed.json"
    if files_file.exists():
        result["files_changed"] = json.loads(files_file.read_text())

    return result
