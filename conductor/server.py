"""FastAPI server — REST API, WebSocket hub, serves dashboard."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import db
import task_manager as tm
import yaml
from agent_manager import AgentManager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from github_monitor import GitHubMonitor
from guardrails import GuardrailConfig, Guardrails
from logger import (
    get_session_log,
    log_event,
    search_logs,
    setup_system_logger,
    tail_system_log,
)
from models import (
    Agent,
    AgentStatus,
    BlockReason,
    PRLifecycle,
    PRStage,
    TaskStatus,
    WorkspaceStatus,
)
from planner import Planner
from pr_lifecycle import PRLifecycleManager
from quota_manager import QuotaManager
from rules_engine import RulesEngine
from workspace_manager import WorkspaceManager

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


# Track retry counts per task to detect repeated flakes
_agent_retry_counts: dict[int, int] = {}
MAX_FLAKE_RETRIES = 2

FLAKE_PATTERNS = [
    "GEMINI_API_KEY",
    "API key not valid",
    "quota exceeded",
    "rate limit",
    "RATE_LIMIT",
    "503 Service Unavailable",
    "502 Bad Gateway",
    "connection reset",
    "Connection refused",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "timeout",
    "internal server error",
    "Internal error",
    "DeadlineExceeded",
]

QUOTA_PATTERNS = [
    "ResourceExhausted",
    "Resource has been exhausted",
    "check quota",
    "rate limit",
    "quota exceeded",
    "RESOURCE_EXHAUSTED",
]

QUOTA_RETRY_DELAY = 60  # seconds to wait for quota reset
MAX_QUOTA_RETRIES = 3

FLAKE_EXIT_CODES = {41, 137, 143}  # 41=gemini auth, 137=OOM/killed, 143=SIGTERM


def _is_flake_failure(agent: Agent) -> tuple[bool, str]:
    """Check if an agent failure looks like a transient/flake error.

    Returns (is_flake, reason).
    """
    # Check exit code (from last output or agent metadata)
    output_tail = agent.output_lines[-30:] if agent.output_lines else []
    full_tail = "\n".join(output_tail).lower()

    # Check for known flake patterns in output
    for pattern in FLAKE_PATTERNS:
        if pattern.lower() in full_tail:
            return True, f"Matched flake pattern: {pattern}"

    # Check if the agent barely ran (< 10 seconds = likely startup failure)
    # But only if it also produced very little output — an agent that processed
    # many requests but ran briefly is NOT a flake
    if agent.started_at and agent.completed_at:
        duration = agent.completed_at - agent.started_at
        if duration < 10 and agent.request_count < 5:
            return True, f"Agent ran for only {duration:.0f}s with {agent.request_count} requests (likely startup failure)"

    # If agent produced zero output lines, likely a crash
    if len(agent.output_lines) == 0:
        return True, "Agent produced no output at all"

    return False, ""


def _is_quota_error(agent: Agent) -> bool:
    """Check if agent failed due to API quota exhaustion."""
    output_tail = agent.output_lines[-30:] if agent.output_lines else []
    full_tail = "\n".join(output_tail).lower()
    return any(p.lower() in full_tail for p in QUOTA_PATTERNS)


_quota_retry_counts: dict[int, int] = {}  # task_id -> retry count


def _on_agent_status(agent: Agent) -> None:
    asyncio.create_task(hub.broadcast("agent_status", agent.to_dict()))

    if agent.status not in (AgentStatus.COMPLETED, AgentStatus.FAILED):
        return

    try:
        task = db.get_task(agent.task_id)
        if not task:
            return

        prl_id = (task.metadata or {}).get("prl_id")

        if agent.status == AgentStatus.COMPLETED:
            # Success path
            tm.transition(task, TaskStatus.DONE)
            _agent_retry_counts.pop(task.id, None)
            if prl_id:
                asyncio.create_task(pr_lifecycle_mgr.advance(prl_id))

        elif agent.status == AgentStatus.FAILED:
            # Check for quota exhaustion first (separate from flakes)
            if _is_quota_error(agent):
                quota_retries = _quota_retry_counts.get(task.id, 0)
                if quota_retries < MAX_QUOTA_RETRIES:
                    _quota_retry_counts[task.id] = quota_retries + 1
                    log_event("server", "agent_quota_wait",
                              agent_id=agent.id, task_id=task.id,
                              retry=quota_retries + 1,
                              delay=QUOTA_RETRY_DELAY)

                    output_tail = agent.output_lines[-5:] if agent.output_lines else []
                    asyncio.create_task(hub.broadcast("agent_failure", {
                        "task_id": task.id,
                        "workspace": agent.workspace,
                        "is_flake": False,
                        "is_quota": True,
                        "reason": f"API quota exhausted — waiting {QUOTA_RETRY_DELAY}s for reset",
                        "retry_number": quota_retries + 1,
                        "max_retries": MAX_QUOTA_RETRIES,
                        "output_tail": output_tail,
                        "action": "quota_wait",
                    }))

                    asyncio.create_task(_persist_failure_to_chat(
                        agent, task,
                        f"API quota exhausted — waiting {QUOTA_RETRY_DELAY}s",
                        output_tail, is_retry=True,
                        retry_num=quota_retries + 1))

                    # Retry with longer delay
                    asyncio.create_task(
                        _retry_agent(task, agent.workspace, delay=QUOTA_RETRY_DELAY)
                    )
                    return  # Don't fall through to flake/failure handling

            # Failure path — detect flake vs real failure
            is_flake, reason = _is_flake_failure(agent)
            retry_count = _agent_retry_counts.get(task.id, 0)

            if is_flake and retry_count < MAX_FLAKE_RETRIES:
                # Auto-retry: flake detected, retries remaining
                _agent_retry_counts[task.id] = retry_count + 1
                log_event("server", "agent_flake_retry",
                          agent_id=agent.id, task_id=task.id,
                          retry=retry_count + 1, reason=reason)

                # Notify user via chat
                output_tail = agent.output_lines[-5:] if agent.output_lines else []
                asyncio.create_task(hub.broadcast("agent_failure", {
                    "task_id": task.id,
                    "workspace": agent.workspace,
                    "is_flake": True,
                    "reason": reason,
                    "retry_number": retry_count + 1,
                    "max_retries": MAX_FLAKE_RETRIES,
                    "output_tail": output_tail,
                    "action": "retrying",
                }))

                # Persist failure context to planner conversation
                asyncio.create_task(_persist_failure_to_chat(
                    agent, task, reason, output_tail, is_retry=True,
                    retry_num=retry_count + 1))

                # Re-spawn the agent after a short delay
                asyncio.create_task(_retry_agent(task, agent.workspace))

            else:
                # Real failure or retries exhausted
                tm.transition(task, TaskStatus.FAILED)
                _agent_retry_counts.pop(task.id, None)

                # Regress PRL back to PLANNING
                if prl_id:
                    prl = db.get_pr_lifecycle(prl_id)
                    if prl and prl.stage == PRStage.CODING:
                        asyncio.create_task(
                            pr_lifecycle_mgr._transition(prl, PRStage.PLANNING)
                        )
                        log_event("server", "prl_regressed_to_planning",
                                  prl_id=prl_id, task_id=task.id)

                # Notify user with failure details and ask how to proceed
                output_tail = agent.output_lines[-10:] if agent.output_lines else []
                asyncio.create_task(hub.broadcast("agent_failure", {
                    "task_id": task.id,
                    "workspace": agent.workspace,
                    "is_flake": False,
                    "reason": reason if is_flake else "Agent failed",
                    "retries_exhausted": is_flake and retry_count >= MAX_FLAKE_RETRIES,
                    "output_tail": output_tail,
                    "action": "needs_input",
                }))

                # Persist failure context to planner conversation
                asyncio.create_task(_persist_failure_to_chat(
                    agent, task, reason if is_flake else "Agent failed",
                    output_tail, is_retry=False))

    except Exception as e:
        log_event("server", "agent_completion_handler_error",
                  level="ERROR", agent_id=agent.id, error=str(e))


async def _persist_failure_to_chat(
    agent: Agent, task, reason: str, output_tail: list[str],
    is_retry: bool = False, retry_num: int = 0,
) -> None:
    """Persist agent failure details into the planner conversation."""
    conv_id = f"ws-{agent.workspace}" if agent.workspace else "default"

    duration = ""
    if agent.started_at and agent.completed_at:
        dur_s = agent.completed_at - agent.started_at
        duration = f" Duration: {dur_s:.0f}s."

    tail_text = "\n".join(output_tail[-5:]) if output_tail else "(no output)"

    if is_retry:
        event_msg = (
            f"[SYSTEM EVENT] ⚠️ Agent {agent.id} failed (auto-retrying, "
            f"attempt {retry_num}/{MAX_FLAKE_RETRIES}).{duration} "
            f"Requests: {agent.request_count}. "
            f"Reason: {reason}\n"
            f"Last output:\n{tail_text}"
        )
    else:
        event_msg = (
            f"[SYSTEM EVENT] ❌ Agent {agent.id} FAILED for task "
            f"#{task.id} '{task.title}'.{duration} "
            f"Requests: {agent.request_count}. "
            f"Reason: {reason}\n"
            f"Last output:\n{tail_text}\n"
            f"PR lifecycle returned to PLANNING. "
            f"Please suggest how to proceed."
        )

    if conv_id not in planner.conversations:
        planner.conversations[conv_id] = db.get_chat_history(conv_id)
    planner.conversations[conv_id].append({
        "role": "user",
        "content": event_msg,
    })
    db.save_chat_message(conv_id, "user", event_msg)


async def _retry_agent(task, workspace_name: str, delay: int = 5) -> None:
    """Retry spawning an agent after a delay."""
    await asyncio.sleep(delay)
    try:
        agent = await agent_mgr.spawn_agent(task, workspace_name)
        if agent:
            log_event("server", "agent_retry_spawned",
                      agent_id=agent.id, task_id=task.id)
    except Exception as e:
        log_event("server", "agent_retry_failed",
                  level="ERROR", task_id=task.id, error=str(e))
        # If retry spawn fails, treat as real failure
        asyncio.create_task(hub.broadcast("agent_failure", {
            "task_id": task.id,
            "workspace": workspace_name,
            "is_flake": False,
            "reason": f"Retry spawn failed: {e}",
            "action": "needs_input",
        }))


def _on_diff_stats(stats: dict) -> None:
    asyncio.create_task(hub.broadcast("diff_stats", stats))


agent_mgr = AgentManager(
    workspace_mgr=workspace_mgr,
    quota_mgr=quota_mgr,
    guardrails=guardrails,
    config=config,
    on_output=_on_agent_output,
    on_status_change=_on_agent_status,
    on_diff_stats=_on_diff_stats,
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

planner = Planner(config=config)

# Background tasks
_bg_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Startup/shutdown lifecycle."""
    log_event("server", "server_started", port=4000)

    # Recover tasks/agents stuck from previous crash
    recovered = db.recover_stuck_tasks()
    if recovered:
        log_event("server", "tasks_recovered", level="WARN", count=recovered)

    # Start background scheduler
    _bg_tasks.append(asyncio.create_task(_scheduler_loop()))
    _bg_tasks.append(asyncio.create_task(_diff_stats_loop()))

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
        workspace_name = msg.get("workspace", "")
        workspace_path = msg.get("workspace_path", "")
        model = msg.get("model", "")
        response = await planner.chat(
            conv_id, text,
            workspace_name=workspace_name,
            workspace_path=workspace_path,
            model=model,
        )
        quota_mgr.record_prompt()
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


@app.post("/api/workspaces/{name}/reset")
async def reset_workspace(name: str):
    """Reset workspace: rollback, clean untracked files, checkout main, release."""
    ws = workspace_mgr.workspaces.get(name)
    if not ws:
        return {"ok": False, "error": "Workspace not found"}

    try:
        # Kill any running agent in this workspace
        for agent_id, agent in list(agent_mgr.agents.items()):
            if agent.workspace == name and agent.status in (AgentStatus.STARTING, AgentStatus.RUNNING):
                await agent_mgr.kill_agent(agent_id)

        # Rollback if snapshot exists
        if ws.snapshot_sha:
            workspace_mgr.rollback(name)

        # Hard reset and clean
        def _run_git(*args):
            return subprocess.run(
                ["git"] + list(args), cwd=ws.path,
                capture_output=True, text=True
            )
        _run_git("checkout", "main")
        _run_git("reset", "--hard", "HEAD")
        _run_git("clean", "-fd")

        # Release workspace
        ws.status = WorkspaceStatus.FREE
        ws.assigned_task_id = None
        ws.agent_id = None

        # Refresh state
        workspace_mgr.discover()
        await hub.broadcast("workspaces", workspace_mgr.list_all())

        log_event("server", "workspace_reset", workspace=name)
        return {"ok": True}
    except Exception as e:
        log_event("server", "workspace_reset_failed", level="ERROR",
                  workspace=name, error=str(e))
        return {"ok": False, "error": str(e)}


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


@app.delete("/api/pr-lifecycles/{prl_id}")
async def delete_pr_lifecycle(prl_id: int):
    prl = db.get_pr_lifecycle(prl_id)
    if not prl:
        return {"error": "PR lifecycle not found"}
    db.delete_pr_lifecycle(prl_id)
    await hub.broadcast("prl_deleted", {"prl_id": prl_id})
    return {"ok": True}


@app.get("/api/chat/{conversation_id}")
async def get_chat(conversation_id: str):
    """Get chat history for a conversation."""
    history = db.get_chat_history(conversation_id)
    return {"messages": history}


@app.get("/api/diff-stats/{workspace_name}")
async def get_diff_stats(workspace_name: str):
    """Get current diff stats for a workspace."""
    if workspace_name not in workspace_mgr.workspaces:
        return {"error": "Workspace not found"}
    stats = workspace_mgr.get_diff_stats(workspace_name)
    return stats


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


@app.get("/api/models")
async def get_models():
    return {
        "planning": config.get("gemini_model", "gemini-3.1-pro-preview"),
        "coding": config.get("gemini_coding_model", "gemini-3.1-pro-preview"),
        "testing": config.get("gemini_test_model", "gemini-3-pro-preview"),
        "available": [
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
    }


@app.post("/api/models")
async def update_models(body: dict):
    key_map = {
        "planning": "gemini_model",
        "coding": "gemini_coding_model",
        "testing": "gemini_test_model",
    }
    for role, cfg_key in key_map.items():
        if role in body:
            config[cfg_key] = body[role]
    # Update planner's default model
    planner._model = config.get("gemini_model", "gemini-3.1-pro-preview")
    # Persist to config.yaml
    _save_config()
    await hub.broadcast("models_updated", await get_models())
    return await get_models()


def _save_config() -> None:
    """Persist model-related config keys to config.yaml (merge, not overwrite)."""
    disk = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            disk = yaml.safe_load(f) or {}
    # Merge only model keys
    for key in ("gemini_model", "gemini_coding_model", "gemini_test_model"):
        if key in config:
            disk[key] = config[key]
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(disk, f, default_flow_style=False, sort_keys=False)


@app.get("/api/rules")
async def get_rules():
    return [r.to_dict() for r in rules_engine.rules]


# ── Chat API ────────────────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(body: dict):
    conv_id = body.get("conversation_id", "default")
    text = body.get("text", "")
    model = body.get("model", "")
    response = await planner.chat(conv_id, text, model=model)
    return {"response": response, "conversation_id": conv_id}


@app.post("/api/chat/system-event")
async def chat_system_event(body: dict):
    """Persist a system event into the planner conversation so the LLM can see it."""
    conv_id = body.get("conversation_id", "default")
    text = body.get("text", "")
    if not text:
        return {"ok": True}

    # Save as a user message with system prefix so the LLM sees it in context
    event_msg = f"[SYSTEM EVENT] {text}"

    # Add to in-memory conversation
    if conv_id not in planner.conversations:
        planner.conversations[conv_id] = db.get_chat_history(conv_id)
    planner.conversations[conv_id].append({
        "role": "user",
        "content": event_msg,
    })
    db.save_chat_message(conv_id, "user", event_msg)
    return {"ok": True}


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

    # Create implementation task — use workspace from conversation
    workspace_name = ""
    if conv_id.startswith("ws-"):
        workspace_name = conv_id[3:]  # "ws-workbench-5" -> "workbench-5"

    task = tm.add_task(
        title=plan.get("title", "Untitled"),
        description=plan.get("description", ""),
        branch=plan.get("branch", ""),
        workspace=workspace_name,
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


@app.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(task_id: int):
    ok = tm.delete_task(task_id)
    if not ok:
        return {"error": "Cannot delete task (not found or still active)"}
    await hub.broadcast("task_deleted", {"task_id": task_id})
    return {"ok": True}


# ── Background Scheduler ───────────────────────────────────────────────────


async def _diff_stats_loop() -> None:
    """Periodically broadcast diff stats for workspaces with changes."""
    while True:
        try:
            for ws_name, _ws in workspace_mgr.workspaces.items():
                try:
                    stats = workspace_mgr.get_diff_stats(ws_name)
                    if stats.get("total_files", 0) > 0:
                        await hub.broadcast("diff_stats", stats)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(8)


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

                # Find workspace — prefer the one where plan was discussed
                ws = None
                if task.workspace and task.workspace in workspace_mgr.workspaces:
                    candidate = workspace_mgr.workspaces[task.workspace]
                    if candidate.status == WorkspaceStatus.FREE:
                        ws = candidate
                if not ws:
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
