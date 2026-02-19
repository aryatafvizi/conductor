"""Workspace Manager — discover workspaces, track assignments, health checks."""
from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path

from logger import log_event
from models import Workspace, WorkspaceStatus

DEFAULT_PATTERN = str(Path.home() / "workspace-*")


class WorkspaceManager:
    """Manages workspace discovery, assignment, and health."""

    def __init__(self, pattern: str = DEFAULT_PATTERN) -> None:
        self.pattern = pattern
        self.workspaces: dict[str, Workspace] = {}
        self.discover()

    def discover(self) -> list[Workspace]:
        """Discover workspaces matching the configured pattern."""
        expanded = os.path.expanduser(self.pattern)
        paths = sorted(glob.glob(expanded))
        for p in paths:
            path = Path(p)
            if not path.is_dir():
                continue
            name = path.name
            if name not in self.workspaces:
                self.workspaces[name] = Workspace(
                    name=name,
                    path=str(path),
                    status=WorkspaceStatus.FREE,
                )
        log_event("workspace_manager", "workspaces_discovered",
                  count=len(self.workspaces),
                  names=list(self.workspaces.keys()))
        return list(self.workspaces.values())

    def get_free_workspace(self) -> Workspace | None:
        """Get the first free workspace."""
        for ws in self.workspaces.values():
            if ws.status == WorkspaceStatus.FREE:
                return ws
        return None

    def assign(self, workspace_name: str, task_id: int, agent_id: str) -> Workspace:
        """Assign a workspace to a task/agent."""
        ws = self.workspaces[workspace_name]
        ws.status = WorkspaceStatus.ASSIGNED
        ws.assigned_task_id = task_id
        ws.agent_id = agent_id
        log_event("workspace_manager", "workspace_assigned",
                  workspace=workspace_name, task_id=task_id, agent_id=agent_id)
        return ws

    def release(self, workspace_name: str) -> Workspace:
        """Release a workspace back to free."""
        ws = self.workspaces[workspace_name]
        ws.status = WorkspaceStatus.FREE
        ws.assigned_task_id = None
        ws.agent_id = None
        log_event("workspace_manager", "workspace_released",
                  workspace=workspace_name)
        return ws

    def snapshot(self, workspace_name: str) -> tuple[str, bool]:
        """Create a git snapshot (stash + record HEAD) for rollback.

        Returns (head_sha, has_stash).
        """
        ws = self.workspaces[workspace_name]
        cwd = ws.path

        # Record HEAD
        head = _run_git(cwd, "rev-parse", "HEAD").strip()
        ws.snapshot_sha = head

        # Stash any changes
        stash_result = _run_git(cwd, "stash", "--include-untracked")
        has_stash = "No local changes" not in stash_result
        ws.has_stash = has_stash

        log_event("guardrails", "snapshot_created",
                  workspace=workspace_name, sha=head[:7],
                  has_stash=has_stash)
        return head, has_stash

    def rollback(self, workspace_name: str) -> bool:
        """Rollback workspace to pre-task snapshot."""
        ws = self.workspaces[workspace_name]
        if not ws.snapshot_sha:
            log_event("workspace_manager", "rollback_no_snapshot", level="WARN",
                      workspace=workspace_name)
            return False

        cwd = ws.path
        _run_git(cwd, "reset", "--hard", ws.snapshot_sha)
        if ws.has_stash:
            _run_git(cwd, "stash", "pop")

        log_event("workspace_manager", "rollback_completed",
                  workspace=workspace_name, sha=ws.snapshot_sha[:7])

        ws.snapshot_sha = ""
        ws.has_stash = False
        return True

    def checkout_branch(self, workspace_name: str, branch: str) -> bool:
        """Checkout a branch in the workspace."""
        ws = self.workspaces[workspace_name]
        try:
            # Fetch first
            _run_git(ws.path, "fetch", "origin")
            # Try checking out existing branch
            try:
                _run_git(ws.path, "checkout", branch)
            except subprocess.CalledProcessError:
                # Create branch if it doesn't exist
                _run_git(ws.path, "checkout", "-b", branch)
            ws.branch = branch
            log_event("workspace_manager", "branch_checked_out",
                      workspace=workspace_name, branch=branch)
            return True
        except subprocess.CalledProcessError as e:
            log_event("workspace_manager", "checkout_failed", level="ERROR",
                      workspace=workspace_name, branch=branch, error=str(e))
            return False

    def get_branch(self, workspace_name: str) -> str:
        """Get the current branch of a workspace."""
        ws = self.workspaces[workspace_name]
        try:
            branch = _run_git(ws.path, "branch", "--show-current").strip()
            ws.branch = branch
            return branch
        except subprocess.CalledProcessError:
            return ""

    def get_diff_stats(self, workspace_name: str) -> dict:
        """Get diff statistics for the workspace."""
        ws = self.workspaces[workspace_name]
        try:
            diff = _run_git(ws.path, "diff", "--stat", "HEAD")
            diff_full = _run_git(ws.path, "diff", "HEAD")
            files_changed = len([
                l for l in diff.strip().split("\n")
                if l and not l.startswith(" ")
            ]) - 1  # Exclude summary line
            lines_changed = sum(
                1 for l in diff_full.split("\n")
                if l.startswith("+") or l.startswith("-")
            )
            return {
                "files_changed": max(0, files_changed),
                "lines_changed": lines_changed,
                "diff": diff_full,
            }
        except subprocess.CalledProcessError:
            return {"files_changed": 0, "lines_changed": 0, "diff": ""}

    def health_check(self, workspace_name: str) -> dict:
        """Get workspace health status."""
        ws = self.workspaces[workspace_name]
        branch = self.get_branch(workspace_name)
        try:
            status = _run_git(ws.path, "status", "--porcelain")
            is_dirty = bool(status.strip())
        except subprocess.CalledProcessError:
            is_dirty = False

        return {
            **ws.to_dict(),
            "branch": branch,
            "is_dirty": is_dirty,
        }

    def list_all(self) -> list[dict]:
        """List all workspaces with health info."""
        return [self.health_check(name) for name in self.workspaces]


def _run_git(cwd: str, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout
