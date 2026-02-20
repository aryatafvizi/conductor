"""SQLite database layer for Conductor."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from models import (
    Agent,
    AgentStatus,
    Pipeline,
    PipelineStatus,
    PRLifecycle,
    PRStage,
    Task,
    TaskPriority,
    TaskStatus,
)

DEFAULT_DB = Path.home() / ".conductor" / "conductor.db"

_local = threading.local()


def _get_conn(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Thread-local connection (SQLite is not thread-safe by default)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db(db_path: Path = DEFAULT_DB) -> None:
    """Create tables if they don't exist."""
    conn = _get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'normal',
            branch TEXT DEFAULT '',
            workspace TEXT DEFAULT '',
            depends_on TEXT DEFAULT '[]',
            block_reason TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 2,
            pipeline_id INTEGER,
            pipeline_step INTEGER DEFAULT 0,
            pr_number INTEGER,
            created_at REAL,
            started_at REAL,
            completed_at REAL,
            metadata TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            task_id INTEGER,
            workspace TEXT,
            pid INTEGER,
            status TEXT DEFAULT 'starting',
            started_at REAL,
            completed_at REAL,
            request_count INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS pipelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            current_step INTEGER DEFAULT 0,
            total_steps INTEGER DEFAULT 0,
            task_ids TEXT DEFAULT '[]',
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS pr_lifecycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_number INTEGER,
            branch TEXT DEFAULT '',
            title TEXT DEFAULT '',
            stage TEXT DEFAULT 'planning',
            iteration INTEGER DEFAULT 0,
            max_iterations INTEGER DEFAULT 3,
            ci_fix_count INTEGER DEFAULT 0,
            precheck_retry_count INTEGER DEFAULT 0,
            greptile_comments_total INTEGER DEFAULT 0,
            greptile_comments_resolved INTEGER DEFAULT 0,
            pipeline_id INTEGER,
            created_at REAL
        );

        CREATE TABLE IF NOT EXISTS quota_usage (
            date TEXT PRIMARY KEY,
            agent_requests INTEGER DEFAULT 0,
            prompts INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
        CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
        CREATE INDEX IF NOT EXISTS idx_chat_conv ON chat_messages(conversation_id);
    """)
    conn.commit()


# ── Task CRUD ───────────────────────────────────────────────────────────────


def create_task(task: Task, db_path: Path = DEFAULT_DB) -> Task:
    conn = _get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO tasks (title, description, status, priority, branch,
           workspace, depends_on, block_reason, retry_count, max_retries,
           pipeline_id, pipeline_step, pr_number, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task.title, task.description, task.status.value, task.priority.value,
         task.branch, task.workspace, json.dumps(task.depends_on),
         task.block_reason, task.retry_count, task.max_retries,
         task.pipeline_id, task.pipeline_step, task.pr_number,
         task.created_at, json.dumps(task.metadata)),
    )
    conn.commit()
    task.id = cur.lastrowid  # type: ignore[assignment]
    return task


def get_task(task_id: int, db_path: Path = DEFAULT_DB) -> Task | None:
    conn = _get_conn(db_path)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return _row_to_task(row)


def list_tasks(
    status: TaskStatus | None = None,
    db_path: Path = DEFAULT_DB,
) -> list[Task]:
    conn = _get_conn(db_path)
    if status:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY id", (status.value,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return [_row_to_task(r) for r in rows]


def update_task(task: Task, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        """UPDATE tasks SET title=?, description=?, status=?, priority=?,
           branch=?, workspace=?, depends_on=?, block_reason=?, retry_count=?,
           max_retries=?, pipeline_id=?, pipeline_step=?, pr_number=?,
           started_at=?, completed_at=?, metadata=? WHERE id=?""",
        (task.title, task.description, task.status.value, task.priority.value,
         task.branch, task.workspace, json.dumps(task.depends_on),
         task.block_reason, task.retry_count, task.max_retries,
         task.pipeline_id, task.pipeline_step, task.pr_number,
         task.started_at, task.completed_at, json.dumps(task.metadata),
         task.id),
    )
    conn.commit()


def delete_task(task_id: int, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute("DELETE FROM agents WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"] or "",
        status=TaskStatus(row["status"]),
        priority=TaskPriority(row["priority"]),
        branch=row["branch"] or "",
        workspace=row["workspace"] or "",
        depends_on=json.loads(row["depends_on"] or "[]"),
        block_reason=row["block_reason"] or "",
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        pipeline_id=row["pipeline_id"],
        pipeline_step=row["pipeline_step"] or 0,
        pr_number=row["pr_number"],
        created_at=row["created_at"] or 0,
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


# ── Pipeline CRUD ───────────────────────────────────────────────────────────


def create_pipeline(pipeline: Pipeline, db_path: Path = DEFAULT_DB) -> Pipeline:
    conn = _get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO pipelines (name, status, current_step, total_steps,
           task_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (pipeline.name, pipeline.status.value, pipeline.current_step,
         pipeline.total_steps, json.dumps(pipeline.task_ids),
         pipeline.created_at),
    )
    conn.commit()
    pipeline.id = cur.lastrowid  # type: ignore[assignment]
    return pipeline


def get_pipeline(pipeline_id: int, db_path: Path = DEFAULT_DB) -> Pipeline | None:
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM pipelines WHERE id = ?", (pipeline_id,)
    ).fetchone()
    if row is None:
        return None
    return Pipeline(
        id=row["id"], name=row["name"],
        status=PipelineStatus(row["status"]),
        current_step=row["current_step"],
        total_steps=row["total_steps"],
        task_ids=json.loads(row["task_ids"] or "[]"),
        created_at=row["created_at"] or 0,
    )


def update_pipeline(pipeline: Pipeline, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        """UPDATE pipelines SET name=?, status=?, current_step=?,
           total_steps=?, task_ids=? WHERE id=?""",
        (pipeline.name, pipeline.status.value, pipeline.current_step,
         pipeline.total_steps, json.dumps(pipeline.task_ids), pipeline.id),
    )
    conn.commit()


# ── PR Lifecycle CRUD ───────────────────────────────────────────────────────


def create_pr_lifecycle(prl: PRLifecycle, db_path: Path = DEFAULT_DB) -> PRLifecycle:
    conn = _get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO pr_lifecycles (pr_number, branch, title, stage,
           iteration, max_iterations, ci_fix_count, precheck_retry_count,
           greptile_comments_total, greptile_comments_resolved,
           pipeline_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (prl.pr_number, prl.branch, prl.title, prl.stage.value,
         prl.iteration, prl.max_iterations, prl.ci_fix_count,
         prl.precheck_retry_count, prl.greptile_comments_total,
         prl.greptile_comments_resolved, prl.pipeline_id, prl.created_at),
    )
    conn.commit()
    prl.id = cur.lastrowid  # type: ignore[assignment]
    return prl


def get_pr_lifecycle(prl_id: int, db_path: Path = DEFAULT_DB) -> PRLifecycle | None:
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM pr_lifecycles WHERE id = ?", (prl_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_pr_lifecycle(row)


def list_pr_lifecycles(db_path: Path = DEFAULT_DB) -> list[PRLifecycle]:
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM pr_lifecycles ORDER BY id DESC"
    ).fetchall()
    return [_row_to_pr_lifecycle(r) for r in rows]


def update_pr_lifecycle(prl: PRLifecycle, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        """UPDATE pr_lifecycles SET pr_number=?, branch=?, title=?, stage=?,
           iteration=?, ci_fix_count=?, precheck_retry_count=?,
           greptile_comments_total=?, greptile_comments_resolved=?,
           pipeline_id=? WHERE id=?""",
        (prl.pr_number, prl.branch, prl.title, prl.stage.value,
         prl.iteration, prl.ci_fix_count, prl.precheck_retry_count,
         prl.greptile_comments_total, prl.greptile_comments_resolved,
         prl.pipeline_id, prl.id),
    )
    conn.commit()


def delete_pr_lifecycle(prl_id: int, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute("DELETE FROM pr_lifecycles WHERE id = ?", (prl_id,))
    conn.commit()


def _row_to_pr_lifecycle(row: sqlite3.Row) -> PRLifecycle:
    return PRLifecycle(
        id=row["id"],
        pr_number=row["pr_number"],
        branch=row["branch"] or "",
        title=row["title"] or "",
        stage=PRStage(row["stage"]),
        iteration=row["iteration"],
        max_iterations=row["max_iterations"],
        ci_fix_count=row["ci_fix_count"],
        precheck_retry_count=row["precheck_retry_count"],
        greptile_comments_total=row["greptile_comments_total"],
        greptile_comments_resolved=row["greptile_comments_resolved"],
        pipeline_id=row["pipeline_id"],
        created_at=row["created_at"] or 0,
    )


# ── Agent CRUD ──────────────────────────────────────────────────────────────


def save_agent(agent: Agent, db_path: Path = DEFAULT_DB) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO agents (id, task_id, workspace, pid, status,
           started_at, completed_at, request_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent.id, agent.task_id, agent.workspace, agent.pid,
         agent.status.value, agent.started_at, agent.completed_at,
         agent.request_count),
    )
    conn.commit()


def get_agent(agent_id: str, db_path: Path = DEFAULT_DB) -> Agent | None:
    conn = _get_conn(db_path)
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        return None
    return Agent(
        id=row["id"], task_id=row["task_id"], workspace=row["workspace"],
        pid=row["pid"], status=AgentStatus(row["status"]),
        started_at=row["started_at"] or 0,
        completed_at=row["completed_at"],
        request_count=row["request_count"],
    )


def list_agents(
    status: AgentStatus | None = None, db_path: Path = DEFAULT_DB
) -> list[Agent]:
    conn = _get_conn(db_path)
    if status:
        rows = conn.execute(
            "SELECT * FROM agents WHERE status = ?", (status.value,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agents").fetchall()
    return [
        Agent(
            id=r["id"], task_id=r["task_id"], workspace=r["workspace"],
            pid=r["pid"], status=AgentStatus(r["status"]),
            started_at=r["started_at"] or 0, completed_at=r["completed_at"],
            request_count=r["request_count"],
        )
        for r in rows
    ]


# ── Quota ───────────────────────────────────────────────────────────────────


def get_quota_usage(date_str: str, db_path: Path = DEFAULT_DB) -> tuple[int, int]:
    """Returns (agent_requests, prompts) for the given date."""
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT agent_requests, prompts FROM quota_usage WHERE date = ?",
        (date_str,),
    ).fetchone()
    if row is None:
        return (0, 0)
    return (row["agent_requests"], row["prompts"])


def increment_quota(
    date_str: str,
    agent_requests: int = 0,
    prompts: int = 0,
    db_path: Path = DEFAULT_DB,
) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        """INSERT INTO quota_usage (date, agent_requests, prompts)
           VALUES (?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
             agent_requests = agent_requests + ?,
             prompts = prompts + ?""",
        (date_str, agent_requests, prompts, agent_requests, prompts),
    )
    conn.commit()


# ── Chat Persistence ────────────────────────────────────────────────────────


def save_chat_message(
    conversation_id: str,
    role: str,
    content: str,
    db_path: Path = DEFAULT_DB,
) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, time.time()),
    )
    conn.commit()


def get_chat_history(
    conversation_id: str,
    db_path: Path = DEFAULT_DB,
) -> list[dict[str, str]]:
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT role, content FROM chat_messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def list_chat_conversations(db_path: Path = DEFAULT_DB) -> list[str]:
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM chat_messages ORDER BY conversation_id",
    ).fetchall()
    return [r[0] for r in rows]


def delete_chat_history(
    conversation_id: str,
    db_path: Path = DEFAULT_DB,
) -> None:
    conn = _get_conn(db_path)
    conn.execute(
        "DELETE FROM chat_messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    conn.commit()


# ── Startup Recovery ────────────────────────────────────────────────────────


def recover_stuck_tasks(db_path: Path = DEFAULT_DB) -> int:
    """Reset tasks/agents stuck in running state after a crash.

    Returns the number of tasks recovered.
    """
    conn = _get_conn(db_path)

    # Find tasks stuck in 'running'
    stuck = conn.execute(
        "SELECT id, metadata FROM tasks WHERE status = 'running'"
    ).fetchall()

    if not stuck:
        return 0

    stuck_ids = []
    prl_ids = []
    for row in stuck:
        stuck_ids.append(row[0])
        # Extract prl_id from metadata if present
        try:
            meta = json.loads(row[1]) if row[1] else {}
            if "prl_id" in meta:
                prl_ids.append(meta["prl_id"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Mark stuck tasks as failed
    conn.executemany(
        "UPDATE tasks SET status = 'failed', "
        "completed_at = ?, "
        "metadata = json_set(COALESCE(metadata, '{}'), '$.recovery_note', "
        "'Interrupted — conductor restarted while task was running') "
        "WHERE id = ?",
        [(time.time(), tid) for tid in stuck_ids],
    )

    # Clean up any agents that were running
    conn.execute(
        "UPDATE agents SET status = 'failed', completed_at = ? "
        "WHERE status IN ('starting', 'running')",
        (time.time(),),
    )

    # Reset associated PR lifecycle stages back to planning
    if prl_ids:
        conn.executemany(
            "UPDATE pr_lifecycles SET stage = 'planning' WHERE id = ?",
            [(pid,) for pid in prl_ids],
        )

    conn.commit()
    return len(stuck_ids)

