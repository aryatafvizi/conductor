"""Agent Manager — spawn/monitor Gemini agents, stream output, detect completion."""
from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from collections.abc import Callable

import db
from guardrails import Guardrails
from logger import SessionLogger, log_event, log_task_summary
from models import Agent, AgentStatus, Task
from quota_manager import QuotaManager
from workspace_manager import WorkspaceManager


class AgentManager:
    """Manages the lifecycle of Gemini agent subprocesses."""

    def __init__(
        self,
        workspace_mgr: WorkspaceManager,
        quota_mgr: QuotaManager,
        guardrails: Guardrails,
        config: dict | None = None,
        on_output: Callable[[str, str], None] | None = None,
        on_status_change: Callable[[Agent], None] | None = None,
        on_diff_stats: Callable[[dict], None] | None = None,
    ) -> None:
        self.workspace_mgr = workspace_mgr
        self.quota_mgr = quota_mgr
        self.guardrails = guardrails
        self._config = config or {}
        self.agents: dict[str, Agent] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._session_loggers: dict[str, SessionLogger] = {}
        # Callbacks for real-time updates
        self._on_output = on_output         # (agent_id, line) -> None
        self._on_status_change = on_status_change  # (agent) -> None
        self._on_diff_stats = on_diff_stats  # (stats_dict) -> None

    def _build_agent_env(self) -> dict[str, str]:
        """Build environment variables for the agent subprocess."""
        env = {**os.environ, "HOME": os.environ.get("HOME", "")}
        # Inject API key from config so the gemini CLI can authenticate
        api_key = self._config.get("gemini_api_key", "")
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return env

    async def spawn_agent(
        self,
        task: Task,
        workspace_name: str,
    ) -> Agent | None:
        """Spawn a Gemini agent in the given workspace."""
        # Quota check
        can_start, reason = self.quota_mgr.can_start_agent()
        if not can_start:
            log_event("agent_manager", "spawn_blocked", level="WARN",
                      task_id=task.id, reason=reason)
            return None

        # Branch check
        if task.branch and not self.guardrails.check_branch_allowed(task.branch):
            log_event("agent_manager", "spawn_blocked_branch", level="ERROR",
                      task_id=task.id, branch=task.branch)
            return None

        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        ws = self.workspace_mgr.workspaces[workspace_name]

        # Create session logger
        session = SessionLogger(task.id)
        self._session_loggers[agent_id] = session

        # Snapshot workspace
        self.workspace_mgr.snapshot(workspace_name)
        session.log_timeline_event("snapshot_created")

        # Checkout branch if specified
        if task.branch:
            self.workspace_mgr.checkout_branch(workspace_name, task.branch)
            session.log_timeline_event("branch_checked_out", branch=task.branch)

        # Build prompt with guardrails preamble
        preamble = self.guardrails.generate_preamble(ws.path, task.id)
        full_prompt = f"{preamble}\n\n{task.description or task.title}"
        session.log_prompt(full_prompt)

        # Build command
        cmd = [
            "gemini",
            "-p", full_prompt,
            "--yolo",
            "--output-format", "stream-json",
        ]

        # Create agent record
        agent = Agent(
            id=agent_id,
            task_id=task.id,
            workspace=workspace_name,
            status=AgentStatus.STARTING,
        )
        self.agents[agent_id] = agent
        db.save_agent(agent)

        # Assign workspace
        self.workspace_mgr.assign(workspace_name, task.id, agent_id)

        try:
            # Spawn the process
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=ws.path,
                env=self._build_agent_env(),
            )
            self._processes[agent_id] = process
            agent.pid = process.pid
            agent.status = AgentStatus.RUNNING
            agent.started_at = time.time()
            db.save_agent(agent)

            self.quota_mgr.agent_started()

            log_event("agent_manager", "agent_spawned",
                      agent_id=agent_id, task_id=task.id,
                      workspace=workspace_name, pid=process.pid)
            session.log_timeline_event("agent_spawned", pid=process.pid)

            if self._on_status_change:
                self._on_status_change(agent)

            # Start monitoring in background
            asyncio.create_task(self._monitor_agent(agent_id))

            return agent

        except Exception as e:
            log_event("agent_manager", "spawn_failed", level="ERROR",
                      agent_id=agent_id, error=str(e))
            agent.status = AgentStatus.FAILED
            db.save_agent(agent)
            self.workspace_mgr.release(workspace_name)
            return None

    def _broadcast_diff_stats(self, ws_name: str) -> None:
        """Broadcast current diff stats for a workspace."""
        if not self._on_diff_stats:
            return
        try:
            stats = self.workspace_mgr.get_diff_stats(ws_name)
            self._on_diff_stats(stats)
        except Exception:
            pass

    async def _monitor_agent(self, agent_id: str) -> None:
        """Monitor agent process output and detect completion."""
        agent = self.agents[agent_id]
        process = self._processes[agent_id]
        session = self._session_loggers.get(agent_id)
        task_start = agent.started_at or time.time()
        last_diff_broadcast = 0.0

        try:
            while True:
                if process.stdout is None:
                    break

                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    # No output for 1s — check if process is still alive
                    if process.returncode is not None:
                        break  # Process has exited
                    # Timeout check even during idle periods
                    elapsed = time.time() - task_start
                    if not self.guardrails.check_timeout(elapsed):
                        log_event("agent_manager", "agent_killed_timeout",
                                  level="WARN", agent_id=agent_id,
                                  elapsed_s=elapsed)
                        await self.kill_agent(agent_id)
                        return
                    # Broadcast diff stats during idle periods too
                    now = time.time()
                    if now - last_diff_broadcast >= 5.0:
                        last_diff_broadcast = now
                        self._broadcast_diff_stats(agent.workspace)
                    continue

                if not line:
                    # Process ended
                    break

                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue

                agent.output_lines.append(decoded)
                agent.request_count += 1

                # Session log
                if session:
                    session.log_agent_output(decoded)

                # Guardrail output scanning
                check = self.guardrails.check_agent_output(decoded)
                if check["should_kill"]:
                    log_event("agent_manager", "agent_killed_guardrail",
                              level="ERROR", agent_id=agent_id,
                              violations=check["violations"])
                    await self.kill_agent(agent_id)
                    return

                # Timeout check
                elapsed = time.time() - task_start
                if not self.guardrails.check_timeout(elapsed):
                    log_event("agent_manager", "agent_killed_timeout",
                              level="WARN", agent_id=agent_id,
                              elapsed_s=elapsed)
                    await self.kill_agent(agent_id)
                    return

                # Quota tracking
                self.quota_mgr.record_agent_request()

                # Callback for real-time dashboard
                if self._on_output:
                    self._on_output(agent_id, decoded)

                # Broadcast diff stats every 5s during agent run
                now = time.time()
                if now - last_diff_broadcast >= 5.0:
                    last_diff_broadcast = now
                    self._broadcast_diff_stats(agent.workspace)

        except asyncio.CancelledError:
            return
        except Exception as e:
            log_event("agent_manager", "monitor_error", level="ERROR",
                      agent_id=agent_id, error=str(e))

        # Process completed — check exit code
        return_code = await process.wait()
        await self._handle_completion(agent_id, return_code)

    async def _handle_completion(self, agent_id: str, return_code: int) -> None:
        """Handle agent process completion."""
        agent = self.agents.get(agent_id)
        if not agent:
            return

        session = self._session_loggers.get(agent_id)
        ws_name = agent.workspace

        if return_code == 0:
            agent.status = AgentStatus.COMPLETED
        else:
            agent.status = AgentStatus.FAILED

        agent.completed_at = time.time()
        db.save_agent(agent)

        self.quota_mgr.agent_stopped()

        # Post-run diff check
        try:
            diff_stats = self.workspace_mgr.get_diff_stats(ws_name)
            files_changed = diff_stats.get("total_files", 0)
            lines_changed = diff_stats.get("total_added", 0) + diff_stats.get("total_removed", 0)
        except Exception:
            diff_stats = {}
            files_changed = 0
            lines_changed = 0

        diff_check = self.guardrails.check_diff_size(
            files_changed, lines_changed
        )

        if session:
            session.log_final_diff(diff_stats.get("diff", ""))
            session.log_timeline_event("agent_completed",
                                       exit_code=return_code,
                                       files_changed=files_changed,
                                       lines_changed=lines_changed)
            session.write_summary({
                "task_id": agent.task_id,
                "agent_id": agent_id,
                "exit_code": return_code,
                "status": agent.status.value,
                "files_changed": files_changed,
                "lines_changed": lines_changed,
                "request_count": agent.request_count,
                "diff_check": diff_check,
            })
            # Write to summary log
            log_task_summary({
                "task_id": agent.task_id,
                "agent_id": agent_id,
                "status": agent.status.value,
                "agent_requests": agent.request_count,
                "files_changed": files_changed,
                "lines_changed": lines_changed,
                "exit_code": return_code,
            })

        log_event("agent_manager", "agent_completed",
                  agent_id=agent_id, task_id=agent.task_id,
                  exit_code=return_code,
                  duration_s=round((agent.completed_at - (agent.started_at or 0)), 1))

        # Final diff stats broadcast BEFORE release/rollback
        self._broadcast_diff_stats(ws_name)

        # Release workspace
        self.workspace_mgr.release(ws_name)

        # Auto-rollback on failure
        if agent.status == AgentStatus.FAILED and self.guardrails.config.auto_rollback_on_failure:
            self.workspace_mgr.rollback(ws_name)
            log_event("agent_manager", "auto_rollback", workspace=ws_name)

        if self._on_status_change:
            self._on_status_change(agent)

    async def kill_agent(self, agent_id: str) -> bool:
        """Kill a specific agent."""
        process = self._processes.get(agent_id)
        agent = self.agents.get(agent_id)
        if not process or not agent:
            return False

        try:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()

            agent.status = AgentStatus.KILLED
            agent.completed_at = time.time()
            db.save_agent(agent)

            self.quota_mgr.agent_stopped()

            if agent.workspace:
                self.workspace_mgr.release(agent.workspace)

            log_event("agent_manager", "agent_killed",
                      agent_id=agent_id, task_id=agent.task_id)

            if self._on_status_change:
                self._on_status_change(agent)

            return True
        except Exception as e:
            log_event("agent_manager", "kill_failed", level="ERROR",
                      agent_id=agent_id, error=str(e))
            return False

    async def kill_all(self) -> int:
        """Kill all running agents. Returns count killed."""
        killed = 0
        for agent_id in list(self._processes.keys()):
            if await self.kill_agent(agent_id):
                killed += 1
        log_event("agent_manager", "kill_all", count=killed)
        return killed

    def get_running_agents(self) -> list[Agent]:
        """Get all currently running agents."""
        return [
            a for a in self.agents.values()
            if a.status in (AgentStatus.STARTING, AgentStatus.RUNNING)
        ]

    def get_agent_output(self, agent_id: str, tail: int = 50) -> list[str]:
        """Get recent output lines from an agent."""
        agent = self.agents.get(agent_id)
        if not agent:
            return []
        return agent.output_lines[-tail:]
