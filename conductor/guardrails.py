"""Guardrails — filesystem sandbox, git safety, resource limits."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from logger import log_event

DEFAULT_PROTECTED_BRANCHES = ["main", "master", "release/*"]
DEFAULT_BLOCKED_PATHS = ["~/.ssh", "~/.conductor", "~/.env", "~/.gitconfig"]


class GuardrailConfig:
    """Guardrail settings loaded from config."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.protected_branches: list[str] = cfg.get(
            "protected_branches", DEFAULT_PROTECTED_BRANCHES
        )
        self.blocked_paths: list[str] = cfg.get(
            "blocked_paths", DEFAULT_BLOCKED_PATHS
        )
        self.max_files_changed: int = cfg.get("max_files_changed", 50)
        self.max_lines_changed: int = cfg.get("max_lines_changed", 2000)
        self.task_timeout_minutes: int = cfg.get("task_timeout_minutes", 30)
        self.max_retries: int = cfg.get("max_retries", 2)
        self.block_force_push: bool = cfg.get("block_force_push", True)
        self.require_commit_tag: bool = cfg.get("require_commit_tag", True)
        self.auto_rollback_on_failure: bool = cfg.get("auto_rollback_on_failure", True)


class Guardrails:
    """Enforces safety rules on agent operations."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or GuardrailConfig()

    # ── Pre-launch checks ───────────────────────────────────────────────

    def check_branch_allowed(self, branch: str) -> bool:
        """Check if the agent is allowed to operate on this branch."""
        for pattern in self.config.protected_branches:
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if branch.startswith(prefix):
                    log_event("guardrails", "branch_blocked", level="WARN",
                              branch=branch, pattern=pattern)
                    return False
            elif branch == pattern:
                log_event("guardrails", "branch_blocked", level="WARN",
                          branch=branch, pattern=pattern)
                return False
        return True

    def check_path_allowed(self, path: str) -> bool:
        """Check if a path is allowed (not in blocked list)."""
        resolved = str(Path(path).expanduser().resolve())
        for blocked in self.config.blocked_paths:
            blocked_resolved = str(Path(blocked).expanduser().resolve())
            if resolved.startswith(blocked_resolved):
                log_event("guardrails", "path_blocked", level="WARN",
                          path=path, blocked_pattern=blocked)
                return False
        return True

    def check_workspace_scope(self, path: str, workspace_path: str) -> bool:
        """Check if a path is within the workspace scope."""
        resolved = str(Path(path).resolve())
        workspace_resolved = str(Path(workspace_path).resolve())
        if not resolved.startswith(workspace_resolved):
            log_event("guardrails", "out_of_scope", level="WARN",
                      path=path, workspace=workspace_path)
            return False
        return True

    # ── Runtime checks ──────────────────────────────────────────────────

    def check_agent_output(self, output_line: str) -> dict[str, Any]:
        """Scan agent output for dangerous operations.

        Only scans actual shell command executions, not model reasoning text.
        Stream-json output contains model thinking which may reference
        dangerous patterns without actually executing them.
        """
        violations = []

        # Try to parse as JSON (stream-json format)
        # Only scan actual command execution, not model text
        text_to_scan = ""
        try:
            import json as _json
            parsed = _json.loads(output_line)
            # Only scan tool execution output, not model response text
            if isinstance(parsed, dict):
                tool_name = parsed.get("tool", "") or parsed.get("name", "")
                tool_input = parsed.get("input", "") or parsed.get("args", "")
                # Only scan shell/terminal commands
                if any(t in str(tool_name).lower() for t in
                       ("shell", "terminal", "exec", "command", "bash", "run_command")):
                    text_to_scan = str(tool_input)
        except (ValueError, TypeError):
            # Not JSON — likely plain text output, could be a real command
            # Only scan if it looks like an actual command (starts with $ or >)
            stripped = output_line.strip()
            if stripped.startswith("$") or stripped.startswith(">"):
                text_to_scan = stripped

        if not text_to_scan:
            return {"violations": [], "should_kill": False}

        # Check for force push
        if self.config.block_force_push:
            force_push_patterns = [
                r"git\s+push\s+.*--force",
                r"git\s+push\s+-f\b",
                r"git\s+push\s+.*--force-with-lease",
            ]
            for pattern in force_push_patterns:
                if re.search(pattern, text_to_scan, re.IGNORECASE):
                    violations.append({
                        "type": "force_push_attempt",
                        "severity": "critical",
                        "line": text_to_scan[:200],
                    })

        # Check for dangerous commands
        dangerous_patterns = [
            (r"rm\s+-rf\s+/", "recursive_delete_root"),
            (r"rm\s+-rf\s+~/", "recursive_delete_home"),
            (r"chmod\s+-R\s+777", "insecure_permissions"),
            (r"curl\s+.*\|\s*sh", "pipe_to_shell"),
            (r"wget\s+.*\|\s*sh", "pipe_to_shell"),
        ]
        for pattern, violation_type in dangerous_patterns:
            if re.search(pattern, text_to_scan, re.IGNORECASE):
                violations.append({
                    "type": violation_type,
                    "severity": "critical",
                    "line": text_to_scan[:200],
                })

        if violations:
            for v in violations:
                log_event("guardrails", "violation_detected", level="ERROR", **v)

        return {"violations": violations, "should_kill": bool(violations)}

    # ── Post-run checks ─────────────────────────────────────────────────

    def check_diff_size(
        self, files_changed: int, lines_changed: int,
    ) -> dict[str, Any]:
        """Check if the diff is within limits."""
        result: dict[str, Any] = {
            "files_changed": files_changed,
            "lines_changed": lines_changed,
            "files_ok": files_changed <= self.config.max_files_changed,
            "lines_ok": lines_changed <= self.config.max_lines_changed,
        }
        if not result["files_ok"]:
            log_event("guardrails", "diff_files_exceeded", level="WARN",
                      files_changed=files_changed,
                      limit=self.config.max_files_changed)
        if not result["lines_ok"]:
            log_event("guardrails", "diff_lines_exceeded", level="WARN",
                      lines_changed=lines_changed,
                      limit=self.config.max_lines_changed)

        result["ok"] = result["files_ok"] and result["lines_ok"]
        return result

    def check_timeout(self, elapsed_seconds: float) -> bool:
        """Check if a task has exceeded its timeout."""
        limit = self.config.task_timeout_minutes * 60
        if elapsed_seconds > limit:
            log_event("guardrails", "timeout_exceeded", level="WARN",
                      elapsed_s=elapsed_seconds, limit_s=limit)
            return False
        return True

    # ── Prompt preamble ─────────────────────────────────────────────────

    def generate_preamble(self, workspace_path: str, task_id: int) -> str:
        """Generate guardrail instructions to inject into agent prompt."""
        protected = ", ".join(self.config.protected_branches)
        return f"""IMPORTANT SAFETY RULES — You MUST follow these:
1. Only modify files within: {workspace_path}
2. Do NOT access: {', '.join(self.config.blocked_paths)}
3. Do NOT push to protected branches: {protected}
4. Do NOT use git push --force or git push -f
5. Tag all commits with: [conductor:task-{task_id}]
6. Do NOT delete files outside the project directory
7. Do NOT run commands that modify system configuration
"""
