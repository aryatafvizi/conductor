#!/usr/bin/env python3
"""Conductor CLI — `con` entrypoint."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Add conductor directory to path
sys.path.insert(0, str(Path(__file__).parent))

import db
from logger import (
    get_session_log, log_event, search_logs, setup_system_logger,
    tail_system_log,
)
from models import TaskPriority, TaskStatus
import task_manager as tm
from workspace_manager import WorkspaceManager


CONFIG_DIR = Path.home() / ".conductor"
PID_FILE = CONFIG_DIR / "server.pid"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="con",
        description="Conductor — Agent orchestration system",
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # ── start ───────────────────────────────────────────────────────────
    p_start = sub.add_parser("start", help="Start server + dashboard")
    p_start.add_argument("--port", type=int, default=4000)

    # ── stop ────────────────────────────────────────────────────────────
    sub.add_parser("stop", help="Stop server gracefully")

    # ── kill ────────────────────────────────────────────────────────────
    p_kill = sub.add_parser("kill", help="Kill agents")
    p_kill.add_argument("agent_id", nargs="?", help="Agent ID (or kill all)")

    # ── plan ────────────────────────────────────────────────────────────
    p_plan = sub.add_parser("plan", help="Start planning chat")
    p_plan.add_argument("message", nargs="?", help="Initial message")

    # ── add ─────────────────────────────────────────────────────────────
    p_add = sub.add_parser("add", help="Add a task")
    p_add.add_argument("title", help="Task title")
    p_add.add_argument("--description", "-d", default="")
    p_add.add_argument("--branch", "-b", default="")
    p_add.add_argument("--priority", "-p", default="normal",
                       choices=["critical", "high", "normal", "low"])
    p_add.add_argument("--depends-on", type=int, nargs="*")

    # ── list ────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--status", "-s", default=None)

    # ── done ────────────────────────────────────────────────────────────
    p_done = sub.add_parser("done", help="Mark task complete")
    p_done.add_argument("task_id", type=int)

    # ── cancel ──────────────────────────────────────────────────────────
    p_cancel = sub.add_parser("cancel", help="Cancel a task")
    p_cancel.add_argument("task_id", type=int)

    # ── rollback ────────────────────────────────────────────────────────
    p_rollback = sub.add_parser("rollback", help="Rollback workspace")
    p_rollback.add_argument("workspace", help="Workspace name")

    # ── agents ──────────────────────────────────────────────────────────
    sub.add_parser("agents", help="Show running agents")

    # ── pr ──────────────────────────────────────────────────────────────
    p_pr = sub.add_parser("pr", help="PR lifecycle commands")
    p_pr.add_argument("pr_action", choices=["status", "create"])
    p_pr.add_argument("--task-id", type=int)

    # ── quota ───────────────────────────────────────────────────────────
    sub.add_parser("quota", help="Show quota usage")

    # ── batch ───────────────────────────────────────────────────────────
    p_batch = sub.add_parser("batch", help="Run command across workspaces")
    p_batch.add_argument("batch_cmd", help="Command to run")
    p_batch.add_argument("--all", action="store_true")
    p_batch.add_argument("--workspaces", type=str, help="Comma-separated IDs, e.g. 1,2,3")

    # ── logs ────────────────────────────────────────────────────────────
    p_logs = sub.add_parser("logs", help="View logs")
    p_logs.add_argument("task_id", nargs="?", type=int, help="View session log for task")
    p_logs.add_argument("--level", default="")
    p_logs.add_argument("--since", type=float, default=0, help="Hours")
    p_logs.add_argument("--search", default="")
    p_logs.add_argument("--tail", type=int, default=50)

    p_logs_export = sub.add_parser("logs-export", help="Export logs")
    p_logs_export.add_argument("--last", default="7d")

    # ── rules ───────────────────────────────────────────────────────────
    p_rules = sub.add_parser("rules", help="Rules commands")
    p_rules.add_argument("rules_action", choices=["list"])

    # ── watch ───────────────────────────────────────────────────────────
    p_watch = sub.add_parser("watch", help="GitHub poll")
    p_watch.add_argument("--once", action="store_true")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Init DB for CLI commands
    db.init_db()
    setup_system_logger()

    # Dispatch
    cmd = args.command
    if cmd == "start":
        cmd_start(args)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "kill":
        cmd_kill(args)
    elif cmd == "plan":
        cmd_plan(args)
    elif cmd == "add":
        cmd_add(args)
    elif cmd == "list":
        cmd_list(args)
    elif cmd == "done":
        cmd_done(args)
    elif cmd == "cancel":
        cmd_cancel(args)
    elif cmd == "rollback":
        cmd_rollback(args)
    elif cmd == "agents":
        cmd_agents()
    elif cmd == "pr":
        cmd_pr(args)
    elif cmd == "quota":
        cmd_quota()
    elif cmd == "batch":
        cmd_batch(args)
    elif cmd == "logs":
        cmd_logs(args)
    elif cmd == "logs-export":
        cmd_logs_export(args)
    elif cmd == "rules":
        cmd_rules(args)
    elif cmd == "watch":
        cmd_watch(args)
    else:
        parser.print_help()


# ── Command Implementations ─────────────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> None:
    port = args.port
    print(f"🎼 Starting Conductor on port {port}...")
    print(f"   Dashboard: http://localhost:{port}")
    print(f"   Press Ctrl+C to stop\n")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Save PID
    PID_FILE.write_text(str(os.getpid()))

    try:
        import uvicorn
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=port,
            log_level="info",
            reload=False,
        )
    except KeyboardInterrupt:
        print("\n🛑 Conductor stopped.")
    finally:
        PID_FILE.unlink(missing_ok=True)


def cmd_stop() -> None:
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"🛑 Sent stop signal to Conductor (PID {pid})")
        except ProcessLookupError:
            print("⚠️  Conductor is not running")
        PID_FILE.unlink(missing_ok=True)
    else:
        print("⚠️  No PID file found. Conductor may not be running.")


def cmd_kill(args: argparse.Namespace) -> None:
    if args.agent_id:
        print(f"🔴 Killing agent {args.agent_id}...")
        # Send kill via API
        _api_post(f"/api/agents/{args.agent_id}/kill")
    else:
        print("🔴 EMERGENCY: Killing all agents...")
        _api_post("/api/agents/kill-all")


def cmd_plan(args: argparse.Namespace) -> None:
    """Interactive planning chat."""
    print("🗓️  Planning Mode (type 'done' to approve, 'quit' to exit)")
    print("─" * 60)

    conv_id = f"plan-{int(time.time())}"

    if args.message:
        response = _api_post("/api/chat", {
            "conversation_id": conv_id, "text": args.message,
        })
        print(f"\n🤖 {response.get('response', 'No response')}\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Planning cancelled.")
            return

        if user_input.lower() == "quit":
            print("👋 Planning cancelled.")
            return

        if user_input.lower() == "done":
            result = _api_post("/api/chat/approve", {
                "conversation_id": conv_id,
            })
            if "error" in result:
                print(f"⚠️  {result['error']}")
                continue
            print(f"\n✅ Plan approved!")
            print(f"   Task ID: {result.get('task_id')}")
            print(f"   PR Lifecycle ID: {result.get('pr_lifecycle_id')}")
            return

        response = _api_post("/api/chat", {
            "conversation_id": conv_id, "text": user_input,
        })
        print(f"\n🤖 {response.get('response', 'No response')}\n")


def cmd_add(args: argparse.Namespace) -> None:
    task = tm.add_task(
        title=args.title,
        description=args.description,
        priority=args.priority,
        branch=args.branch,
        depends_on=args.depends_on,
    )
    print(f"✅ Task #{task.id}: {task.title}")
    print(f"   Status: {task.status.value} | Priority: {task.priority.value}")
    if task.branch:
        print(f"   Branch: {task.branch}")
    if task.depends_on:
        print(f"   Depends on: {task.depends_on}")


def cmd_list(args: argparse.Namespace) -> None:
    status = TaskStatus(args.status) if args.status else None
    tasks = db.list_tasks(status=status)

    if not tasks:
        print("📭 No tasks found.")
        return

    STATUS_ICONS = {
        "pending": "⏳", "blocked": "🚫", "ready": "🟢",
        "running": "🏃", "done": "✅", "failed": "❌", "cancelled": "⛔",
    }

    print(f"{'ID':>4}  {'Status':<10}  {'Priority':<10}  {'Title'}")
    print("─" * 70)
    for t in tasks:
        icon = STATUS_ICONS.get(t.status.value, "❓")
        print(f"{t.id:>4}  {icon} {t.status.value:<8}  {t.priority.value:<10}  {t.title}")


def cmd_done(args: argparse.Namespace) -> None:
    task = db.get_task(args.task_id)
    if not task:
        print(f"❌ Task #{args.task_id} not found")
        return
    task = tm.transition(task, TaskStatus.DONE)
    print(f"✅ Task #{task.id} marked done: {task.title}")


def cmd_cancel(args: argparse.Namespace) -> None:
    task = tm.cancel_task(args.task_id)
    if task:
        print(f"⛔ Task #{args.task_id} cancelled")
    else:
        print(f"❌ Task #{args.task_id} not found")


def cmd_rollback(args: argparse.Namespace) -> None:
    ws_mgr = WorkspaceManager()
    if args.workspace not in ws_mgr.workspaces:
        print(f"❌ Workspace '{args.workspace}' not found")
        return
    ok = ws_mgr.rollback(args.workspace)
    if ok:
        print(f"↩️  Workspace '{args.workspace}' rolled back")
    else:
        print(f"⚠️  No snapshot found for '{args.workspace}'")


def cmd_agents() -> None:
    agents = db.list_agents()
    running = [a for a in agents if a.status in (AgentStatus.STARTING, AgentStatus.RUNNING)]

    if not running:
        print("🤖 No agents running.")
        return

    print(f"{'ID':<16}  {'Task':>6}  {'Workspace':<16}  {'Status':<10}  {'Requests'}")
    print("─" * 70)
    from models import AgentStatus
    for a in running:
        print(f"{a.id:<16}  #{a.task_id:>5}  {a.workspace:<16}  {a.status.value:<10}  {a.request_count}")


def cmd_pr(args: argparse.Namespace) -> None:
    if args.pr_action == "status":
        prls = db.list_pr_lifecycles()
        if not prls:
            print("📭 No PR lifecycles.")
            return
        print(f"{'ID':>4}  {'PR':>6}  {'Stage':<22}  {'Iter':>4}  {'Title'}")
        print("─" * 75)
        for prl in prls:
            pr_str = f"#{prl.pr_number}" if prl.pr_number else "—"
            print(f"{prl.id:>4}  {pr_str:>6}  {prl.stage.value:<22}  {prl.iteration:>4}  {prl.title}")
    elif args.pr_action == "create":
        print("Use the dashboard or API to create PRs.")


def cmd_quota() -> None:
    from quota_manager import QuotaManager
    qm = QuotaManager()
    status = qm.get_status()

    print("📊 Quota Status")
    print("─" * 40)
    print(f"Agent requests:  {status.agent_requests_used}/{status.agent_requests_limit} ({status.agent_pct:.0f}%)")
    print(f"Prompts:         {status.prompts_used}/{status.prompts_limit} ({status.prompt_pct:.0f}%)")
    print(f"Concurrent:      {status.concurrent_agents}/{status.max_concurrent}")
    print(f"Paused:          {'Yes 🔴' if status.is_paused else 'No 🟢'}")
    print(f"Reset in:        {qm.time_until_reset()}")


def cmd_batch(args: argparse.Namespace) -> None:
    ws_mgr = WorkspaceManager()

    if args.workspaces:
        names = [f"workspace-{n}" for n in args.workspaces.split(",")]
    else:
        names = list(ws_mgr.workspaces.keys())

    print(f"🔄 Running across {len(names)} workspaces: {', '.join(names)}")
    print("─" * 60)

    for name in names:
        ws = ws_mgr.workspaces.get(name)
        if not ws:
            print(f"\n⚠️  {name}: not found")
            continue

        print(f"\n📂 {name} ({ws.path}):")
        try:
            result = subprocess.run(
                args.batch_cmd, shell=True, cwd=ws.path,
                capture_output=True, text=True, timeout=60,
            )
            if result.stdout:
                for line in result.stdout.strip().split("\n"):
                    print(f"   {line}")
            if result.returncode != 0 and result.stderr:
                print(f"   ❌ {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"   ⏱️  Timed out")
        except Exception as e:
            print(f"   ❌ {e}")


def cmd_logs(args: argparse.Namespace) -> None:
    if args.task_id:
        # Session log for specific task
        session = get_session_log(args.task_id)
        if "error" in session:
            print(f"❌ {session['error']}")
            return
        print(f"📋 Session Log — Task #{args.task_id}")
        print("─" * 60)
        if "summary" in session:
            s = session["summary"]
            print(f"Status: {s.get('status', '?')}")
            print(f"Duration: {s.get('duration_s', '?')}s")
            print(f"Files changed: {s.get('files_changed', '?')}")
            print(f"Lines changed: {s.get('lines_changed', '?')}")
            print(f"Agent requests: {s.get('request_count', '?')}")
        if "timeline" in session:
            print(f"\nTimeline ({len(session['timeline'])} events):")
            for evt in session["timeline"][-20:]:
                print(f"  [{evt.get('elapsed_s', '?')}s] {evt.get('event', '')}")
        return

    # System log
    if args.search or args.level or args.since:
        entries = search_logs(
            query=args.search or "",
            level=args.level or None,
            since_hours=args.since or None,
        )
    else:
        entries = tail_system_log(args.tail)

    if not entries:
        print("📭 No log entries found.")
        return

    LEVEL_COLORS = {
        "INFO": "\033[36m",   # Cyan
        "WARN": "\033[33m",   # Yellow
        "ERROR": "\033[31m",  # Red
        "DEBUG": "\033[90m",  # Gray
    }
    RESET = "\033[0m"

    for entry in entries:
        if isinstance(entry, dict):
            level = entry.get("level", "INFO")
            color = LEVEL_COLORS.get(level, "")
            ts = entry.get("ts", "")[:19]
            comp = entry.get("component", "")
            event = entry.get("event", "")
            extra = {k: v for k, v in entry.items()
                     if k not in ("ts", "level", "component", "event")}
            extra_str = f" {json.dumps(extra)}" if extra else ""
            print(f"{color}{ts} [{level}] {comp}: {event}{extra_str}{RESET}")
        else:
            print(entry)


def cmd_logs_export(args: argparse.Namespace) -> None:
    # Parse --last (e.g., "7d", "24h")
    last = args.last
    hours = 24 * 7  # default 7 days
    if last.endswith("d"):
        hours = int(last[:-1]) * 24
    elif last.endswith("h"):
        hours = int(last[:-1])

    entries = search_logs(query="", since_hours=hours)
    output = json.dumps(entries, indent=2, default=str)
    filename = f"conductor_logs_{int(time.time())}.json"
    Path(filename).write_text(output)
    print(f"📦 Exported {len(entries)} log entries to {filename}")


def cmd_rules(args: argparse.Namespace) -> None:
    from rules_engine import RulesEngine
    engine = RulesEngine()

    if not engine.rules:
        print("📭 No rules loaded.")
        return

    print(f"📏 {len(engine.rules)} rules loaded:")
    print("─" * 60)
    for r in engine.rules:
        enabled = "🟢" if r.enabled else "🔴"
        print(f"  {enabled} {r.name}")
        print(f"     Trigger: {r.trigger_type} ({r.trigger_pattern or r.trigger_source})")
        print(f"     Action:  {r.action_type} → {r.action_template[:50]}")


def cmd_watch(args: argparse.Namespace) -> None:
    print("🔍 GitHub poll (one-shot)...")
    import asyncio
    from github_monitor import GitHubMonitor
    import yaml
    config = {}
    config_file = CONFIG_DIR / "config.yaml"
    if config_file.exists():
        with open(config_file) as f:
            config = yaml.safe_load(f) or {}

    gh_cfg = config.get("github", {})
    if not gh_cfg.get("repo"):
        print("⚠️  No repo configured in ~/.conductor/config.yaml")
        return

    monitor = GitHubMonitor(repo=gh_cfg["repo"])
    events = asyncio.run(monitor.check_once())
    if events:
        for evt in events:
            print(f"  [{evt.get('type')}] PR #{evt.get('pr_number')} — {evt.get('check_name', evt.get('body', '')[:50])}")
    else:
        print("  No events.")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _api_post(path: str, data: dict | None = None) -> dict:
    """Make a request to the local Conductor API."""
    import urllib.request
    url = f"http://localhost:4000{path}"
    body = json.dumps(data or {}).encode() if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    main()
