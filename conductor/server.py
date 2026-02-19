"""FastAPI server — REST API, WebSocket hub, serves dashboard."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import db
from agent_manager import AgentManager
from github_monitor import GitHubMonitor
from guardrails import GuardrailConfig, Guardrails
from logger import (
    get_session_log, log_event, search_logs, setup_system_logger,
    tail_system_log,
)
from models import (
    Agent, AgentStatus, BlockReason, PRLifecycle, PRStage,
    Task, TaskPriority, TaskStatus,
)
from planner import Planner
from pr_lifecycle import PRLifecycleManager
from quota_manager import QuotaManager
from rules_engine import RulesEngine
from workspace_manager import WorkspaceManager
import task_manager as tm

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".conductor"
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


# ── WebSocket Hub ───────────────────────────────────────────────────────────


class WebSocketHub:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self) -> None:
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, event_type: str, data: Any) -> None:
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# ── App Setup ───────────────────────────────────────────────────────────────

hub = WebSocketHub()

config = load_config()
logging_cfg = config.get("logging", {})
setup_system_logger(level=logging_cfg.get("level", "INFO"))

db.init_db()

guardrail_cfg = GuardrailConfig(config.get("guardrails"))
guardrails = Guardrails(guardrail_cfg)

quota_cfg = config.get("quota", {})
quota_mgr = QuotaManager(
    daily_agent_requests=quota_cfg.get("daily_agent_requests", 200),
    daily_prompts=quota_cfg.get("daily_prompts", 1500),
    max_concurrent=quota_cfg.get("max_concurrent_agents", 3),
    pause_at_percent=quota_cfg.get("pause_at_percent", 90),
    reserve_requests=quota_cfg.get("reserve_requests", 20),
)

workspace_mgr = WorkspaceManager(
    pattern=config.get("workspace_pattern", str(Path.home() / "workspace-*"))
)


def _on_agent_output(agent_id: str, line: str) -> None:
    asyncio.create_task(hub.broadcast("agent_output", {
        "agent_id": agent_id, "line": line,
    }))


def _on_agent_status(agent: Agent) -> None:
    asyncio.create_task(hub.broadcast("agent_status", agent.to_dict()))


agent_mgr = AgentManager(
    workspace_mgr=workspace_mgr,
    quota_mgr=quota_mgr,
    guardrails=guardrails,
    on_output=_on_agent_output,
    on_status_change=_on_agent_status,
)

github_cfg = config.get("github", {})
github_monitor = GitHubMonitor(
    repo=github_cfg.get("repo", ""),
    poll_interval=github_cfg.get("poll_interval", 60),
)

rules_engine = RulesEngine()


def _on_prl_stage(prl: PRLifecycle) -> None:
    asyncio.create_task(hub.broadcast("pr_lifecycle", prl.to_dict()))


pr_lifecycle_mgr = PRLifecycleManager(
    github=github_monitor,
    config=config.get("pr_lifecycle", {}),
    on_stage_change=_on_prl_stage,
)

planner = Planner()

# Background tasks
_bg_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Startup/shutdown lifecycle."""
    log_event("server", "server_started", port=4000)

    # Start background scheduler
    _bg_tasks.append(asyncio.create_task(_scheduler_loop()))

    # Start GitHub polling if configured
    if github_cfg.get("repo"):
        _bg_tasks.append(asyncio.create_task(
            github_monitor.start_polling(on_event=_handle_github_event)
        ))

    yield

    # Shutdown
    for task in _bg_tasks:
        task.cancel()
    await agent_mgr.kill_all()
    log_event("server", "server_stopped")


app = FastAPI(title="Conductor", lifespan=lifespan)

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Dashboard ───────────────────────────────────────────────────────────────


@app.get("/")
async def dashboard():
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>Conductor</h1><p>Dashboard not found</p>")


# ── WebSocket ───────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "type": "init",
            "data": await _get_full_state(),
        }))

        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            await _handle_ws_message(ws, msg)
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


async def _handle_ws_message(ws: WebSocket, msg: dict) -> None:
    """Handle incoming WebSocket messages from the dashboard."""
    action = msg.get("action", "")

    if action == "chat":
        conv_id = msg.get("conversation_id", "default")
        text = msg.get("text", "")
        response = await planner.chat(conv_id, text)
        await ws.send_text(json.dumps({
            "type": "chat_response",
            "data": {
                "conversation_id": conv_id,
                "response": response,
            },
        }))

    elif action == "kill_agent":
        agent_id = msg.get("agent_id", "")
        await agent_mgr.kill_agent(agent_id)

    elif action == "kill_all":
        await agent_mgr.kill_all()

    elif action == "rollback":
        ws_name = msg.get("workspace", "")
        workspace_mgr.rollback(ws_name)

    elif action == "refresh":
        await ws.send_text(json.dumps({
            "type": "init",
            "data": await _get_full_state(),
        }))


# ── REST API ────────────────────────────────────────────────────────────────


@app.get("/api/state")
async def get_state():
    return await _get_full_state()


@app.get("/api/tasks")
async def get_tasks():
    return [t.to_dict() for t in db.list_tasks()]


@app.post("/api/tasks")
async def create_task(body: dict):
    task = tm.add_task(
        title=body.get("title", ""),
        description=body.get("description", ""),
        priority=body.get("priority", "normal"),
        branch=body.get("branch", ""),
        depends_on=body.get("depends_on"),
        pr_number=body.get("pr_number"),
    )
    await hub.broadcast("task_created", task.to_dict())
    return task.to_dict()


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    task = tm.cancel_task(task_id)
    if task:
        await hub.broadcast("task_updated", task.to_dict())
    return {"ok": task is not None}


@app.get("/api/agents")
async def get_agents():
    return [a.to_dict() for a in agent_mgr.get_running_agents()]


@app.post("/api/agents/kill-all")
async def kill_all_agents():
    count = await agent_mgr.kill_all()
    return {"killed": count}


@app.post("/api/agents/{agent_id}/kill")
async def kill_agent(agent_id: str):
    ok = await agent_mgr.kill_agent(agent_id)
    return {"ok": ok}


@app.get("/api/workspaces")
async def get_workspaces():
    return workspace_mgr.list_all()


@app.post("/api/workspaces/{name}/rollback")
async def rollback_workspace(name: str):
    ok = workspace_mgr.rollback(name)
    return {"ok": ok}


@app.get("/api/quota")
async def get_quota():
    status = quota_mgr.get_status()
    return {
        **status.to_dict(),
        "time_until_reset": quota_mgr.time_until_reset(),
    }


@app.get("/api/pr-lifecycles")
async def get_pr_lifecycles():
    return [prl.to_dict() for prl in db.list_pr_lifecycles()]


@app.get("/api/logs")
async def get_logs(n: int = 50, level: str = "", search: str = "", since: float = 0):
    if search or level or since:
        return search_logs(
            query=search or "",
            level=level or None,
            since_hours=since or None,
        )
    return tail_system_log(n)


@app.get("/api/logs/session/{task_id}")
async def get_session_logs(task_id: int):
    return get_session_log(task_id)


@app.get("/api/rules")
async def get_rules():
    return [r.to_dict() for r in rules_engine.rules]


# ── Chat API ────────────────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(body: dict):
    conv_id = body.get("conversation_id", "default")
    text = body.get("text", "")
    response = await planner.chat(conv_id, text)
    return {"response": response, "conversation_id": conv_id}


@app.post("/api/chat/approve")
async def approve_plan(body: dict):
    conv_id = body.get("conversation_id", "default")
    plan = planner.extract_plan(conv_id)
    if not plan:
        return {"error": "No plan found in conversation"}

    # Create PR lifecycle from plan
    prl = await pr_lifecycle_mgr.start_lifecycle(
        title=plan.get("title", "Untitled"),
        branch=plan.get("branch", ""),
        plan=json.dumps(plan),
    )

    # Create implementation task
    task = tm.add_task(
        title=plan.get("title", "Untitled"),
        description=plan.get("description", ""),
        branch=plan.get("branch", ""),
        metadata={"plan": plan, "prl_id": prl.id},
    )

    await hub.broadcast("plan_approved", {
        "plan": plan,
        "task": task.to_dict(),
        "pr_lifecycle": prl.to_dict(),
    })

    return {
        "plan": plan,
        "task_id": task.id,
        "pr_lifecycle_id": prl.id,
    }


# ── Background Scheduler ───────────────────────────────────────────────────


async def _scheduler_loop() -> None:
    """Main scheduler — runs ready tasks, checks quota, etc."""
    while True:
        try:
            # Check quota reset
            quota_mgr.check_reset()

            # Get ready tasks
            ready = tm.get_ready_tasks()

            for task in ready:
                # Check quota
                can_start, reason = quota_mgr.can_start_agent()
                if not can_start:
                    tm.mark_blocked(task, BlockReason.QUOTA_EXHAUSTED)
                    continue

                # Find free workspace
                ws = workspace_mgr.get_free_workspace()
                if not ws:
                    tm.mark_blocked(task, BlockReason.NO_WORKSPACE)
                    continue

                # Launch agent
                tm.transition(task, TaskStatus.RUNNING)
                tm.assign_workspace(task, ws.name)

                agent = await agent_mgr.spawn_agent(task, ws.name)
                if not agent:
                    tm.transition(task, TaskStatus.FAILED)
                    continue

                await hub.broadcast("task_started", {
                    "task": task.to_dict(),
                    "agent": agent.to_dict(),
                })

        except Exception as e:
            log_event("scheduler", "scheduler_error", level="ERROR",
                      error=str(e))

        await asyncio.sleep(5)  # Check every 5 seconds


async def _handle_github_event(event: dict[str, Any]) -> None:
    """Handle events from GitHub polling."""
    log_event("github_monitor", "event_received",
              event_type=event.get("type", ""))

    # Run through rules engine
    actions = rules_engine.evaluate(event)
    for action in actions:
        if action["type"] == "create_task":
            task = tm.add_task(
                title=action["title"],
                priority=action.get("priority", "normal"),
                metadata={"triggered_by": action["rule_name"], "event": event},
            )
            await hub.broadcast("rule_triggered", {
                "rule": action["rule_name"],
                "task": task.to_dict(),
            })

    await hub.broadcast("github_event", event)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _get_full_state() -> dict[str, Any]:
    """Get the complete current state for dashboard init."""
    return {
        "tasks": [t.to_dict() for t in db.list_tasks()],
        "agents": [a.to_dict() for a in agent_mgr.get_running_agents()],
        "workspaces": workspace_mgr.list_all(),
        "quota": {
            **quota_mgr.get_status().to_dict(),
            "time_until_reset": quota_mgr.time_until_reset(),
        },
        "pr_lifecycles": [prl.to_dict() for prl in db.list_pr_lifecycles()],
        "logs": tail_system_log(20),
        "rules": [r.to_dict() for r in rules_engine.rules],
    }
