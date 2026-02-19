"""GitHub Monitor — poll PRs, CI status, review comments via gh CLI."""
from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from logger import log_event


class GitHubMonitor:
    """Polls GitHub for PR status, CI results, and review comments."""

    def __init__(self, repo: str, poll_interval: int = 60) -> None:
        self.repo = repo
        self.poll_interval = poll_interval
        self._running = False

    async def start_polling(
        self, on_event: Any = None,
    ) -> None:
        """Start the polling loop."""
        self._running = True
        log_event("github_monitor", "polling_started",
                  repo=self.repo, interval=self.poll_interval)

        while self._running:
            try:
                events = await self.check_once()
                if on_event:
                    for event in events:
                        await on_event(event)
            except Exception as e:
                log_event("github_monitor", "poll_error", level="ERROR",
                          error=str(e))
            await asyncio.sleep(self.poll_interval)

    def stop_polling(self) -> None:
        self._running = False

    async def check_once(self) -> list[dict[str, Any]]:
        """Single poll: check PRs, CI, and reviews."""
        events: list[dict[str, Any]] = []

        # Check open PRs
        prs = await self._get_open_prs()
        for pr in prs:
            pr_num = pr.get("number")

            # Check CI status
            ci_events = await self._check_ci_status(pr_num)
            events.extend(ci_events)

            # Check review comments
            review_events = await self._check_reviews(pr_num)
            events.extend(review_events)

        return events

    async def _get_open_prs(self) -> list[dict]:
        """Get all open PRs."""
        try:
            result = await self._run_gh(
                "pr", "list", "--repo", self.repo,
                "--state", "open", "--json",
                "number,title,headRefName,statusCheckRollup",
            )
            return json.loads(result) if result else []
        except Exception as e:
            log_event("github_monitor", "pr_list_error", level="ERROR",
                      error=str(e))
            return []

    async def _check_ci_status(self, pr_number: int) -> list[dict[str, Any]]:
        """Check CI status for a PR."""
        events = []
        try:
            result = await self._run_gh(
                "pr", "checks", str(pr_number),
                "--repo", self.repo, "--json",
                "name,state,conclusion",
            )
            checks = json.loads(result) if result else []

            for check in checks:
                if check.get("conclusion") == "failure":
                    events.append({
                        "type": "ci_failure",
                        "pr_number": pr_number,
                        "check_name": check.get("name", ""),
                        "state": check.get("state", ""),
                    })
                elif check.get("conclusion") == "success":
                    events.append({
                        "type": "ci_success",
                        "pr_number": pr_number,
                        "check_name": check.get("name", ""),
                    })
        except Exception as e:
            log_event("github_monitor", "ci_check_error", level="ERROR",
                      pr_number=pr_number, error=str(e))
        return events

    async def _check_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Check for new review comments on a PR."""
        events = []
        try:
            result = await self._run_gh(
                "pr", "view", str(pr_number),
                "--repo", self.repo, "--json",
                "reviews,comments",
            )
            data = json.loads(result) if result else {}

            # Check review comments
            reviews = data.get("reviews", [])
            for review in reviews:
                author = review.get("author", {}).get("login", "")
                body = review.get("body", "")
                state = review.get("state", "")

                if body.strip():  # Only non-empty reviews
                    events.append({
                        "type": "review_comment",
                        "pr_number": pr_number,
                        "author": author,
                        "body": body,
                        "state": state,
                        "source": "greptile" if "greptile" in author.lower() else "human",
                    })

            # Check PR comments
            comments = data.get("comments", [])
            for comment in comments:
                author = comment.get("author", {}).get("login", "")
                body = comment.get("body", "")
                events.append({
                    "type": "pr_comment",
                    "pr_number": pr_number,
                    "author": author,
                    "body": body,
                    "source": "greptile" if "greptile" in author.lower() else "human",
                })

        except Exception as e:
            log_event("github_monitor", "review_check_error", level="ERROR",
                      pr_number=pr_number, error=str(e))
        return events

    async def get_ci_failure_logs(
        self, pr_number: int, check_name: str,
    ) -> str:
        """Get CI failure logs for a specific check."""
        try:
            # List workflow runs
            result = await self._run_gh(
                "run", "list", "--repo", self.repo,
                "--json", "databaseId,name,conclusion,headBranch",
                "--limit", "10",
            )
            runs = json.loads(result) if result else []

            # Find the failed run
            for run in runs:
                if run.get("conclusion") == "failure" and check_name in run.get("name", ""):
                    run_id = run.get("databaseId")
                    if run_id:
                        log_result = await self._run_gh(
                            "run", "view", str(run_id),
                            "--repo", self.repo, "--log-failed",
                        )
                        return log_result or "No logs available"
            return "No matching failed run found"
        except Exception as e:
            return f"Error getting CI logs: {e}"

    async def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str = "main",
        workspace_path: str = "",
    ) -> int | None:
        """Create a PR and return the PR number."""
        try:
            result = await self._run_gh(
                "pr", "create",
                "--title", title,
                "--body", body,
                "--head", branch,
                "--base", base,
                "--repo", self.repo,
                "--json", "number",
                cwd=workspace_path or None,
            )
            data = json.loads(result) if result else {}
            pr_number = data.get("number")
            log_event("github_monitor", "pr_created",
                      pr_number=pr_number, branch=branch, title=title)
            return pr_number
        except Exception as e:
            log_event("github_monitor", "pr_create_failed", level="ERROR",
                      error=str(e))
            return None

    async def request_review(
        self, pr_number: int, reviewers: list[str] | None = None,
    ) -> bool:
        """Request review on a PR."""
        try:
            cmd = ["pr", "edit", str(pr_number), "--repo", self.repo]
            if reviewers:
                cmd.extend(["--add-reviewer", ",".join(reviewers)])
            await self._run_gh(*cmd)
            log_event("github_monitor", "review_requested",
                      pr_number=pr_number, reviewers=reviewers)
            return True
        except Exception as e:
            log_event("github_monitor", "review_request_failed", level="ERROR",
                      pr_number=pr_number, error=str(e))
            return False

    async def comment_on_pr(self, pr_number: int, body: str) -> bool:
        """Add a comment to a PR."""
        try:
            await self._run_gh(
                "pr", "comment", str(pr_number),
                "--repo", self.repo, "--body", body,
            )
            return True
        except Exception as e:
            log_event("github_monitor", "comment_failed", level="ERROR",
                      pr_number=pr_number, error=str(e))
            return False

    async def _run_gh(self, *args: str, cwd: str | None = None) -> str:
        """Run a gh CLI command asynchronously."""
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"gh command failed: {err}")
        return stdout.decode("utf-8", errors="replace")
