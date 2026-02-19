"""PR Lifecycle Manager — end-to-end PR automation pipeline."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import db
from github_monitor import GitHubMonitor
from logger import log_event
from models import PRLifecycle, PRStage, Task, TaskPriority, TaskStatus
import task_manager


class PRLifecycleManager:
    """Manages the full PR lifecycle: plan → code → test → PR → CI → Greptile → merge."""

    def __init__(
        self,
        github: GitHubMonitor,
        config: dict[str, Any] | None = None,
        on_stage_change: Callable[[PRLifecycle], None] | None = None,
    ) -> None:
        self.github = github
        self.config = config or {}
        self._on_stage_change = on_stage_change

        # Config defaults
        self.max_greptile_iterations = self.config.get("max_greptile_iterations", 3)
        self.max_precheck_retries = self.config.get("max_precheck_retries", 3)
        self.max_ci_fix_retries = self.config.get("max_ci_fix_retries", 3)
        self.precheck_command = self.config.get("precheck_command", "scripts/precheck.sh")
        self.test_commands = self.config.get("test_commands", [])
        self.pr_base_branch = self.config.get("pr_base_branch", "main")
        self.greptile_poll_interval = self.config.get(
            "greptile", {}
        ).get("poll_interval", 120)

    async def start_lifecycle(
        self,
        title: str,
        branch: str,
        plan: str = "",
    ) -> PRLifecycle:
        """Start a new PR lifecycle."""
        prl = PRLifecycle(
            title=title,
            branch=branch,
            stage=PRStage.CODING,
            created_at=time.time(),
        )
        prl = db.create_pr_lifecycle(prl)

        log_event("pr_lifecycle", "lifecycle_started",
                  prl_id=prl.id, title=title, branch=branch)

        return prl

    async def advance(
        self,
        prl_id: int,
        trigger_agent: Callable | None = None,
    ) -> PRLifecycle | None:
        """Advance the PR lifecycle to the next appropriate stage."""
        prl = db.get_pr_lifecycle(prl_id)
        if not prl:
            return None

        old_stage = prl.stage

        if prl.stage == PRStage.CODING:
            # Coding done → run prechecks
            await self._transition(prl, PRStage.PRECHECKS)

        elif prl.stage == PRStage.PRECHECKS:
            # Create precheck task
            task = task_manager.add_task(
                title=f"[PR {prl.title}] Run prechecks",
                description=f"Run {self.precheck_command} in workspace. Fix any failures.",
                priority="high",
                branch=prl.branch,
                metadata={"prl_id": prl.id, "type": "precheck"},
            )
            log_event("pr_lifecycle", "precheck_task_created",
                      prl_id=prl.id, task_id=task.id)

        elif prl.stage == PRStage.PR_CREATED:
            # PR created → start monitoring CI
            await self._transition(prl, PRStage.CI_MONITORING)

        elif prl.stage == PRStage.CI_MONITORING:
            # Check CI status
            if prl.pr_number:
                events = await self.github._check_ci_status(prl.pr_number)
                failures = [e for e in events if e["type"] == "ci_failure"]

                if not failures:
                    # All CI passed → Greptile review
                    await self._transition(prl, PRStage.GREPTILE_REVIEW)
                    if prl.pr_number:
                        await self.github.comment_on_pr(
                            prl.pr_number,
                            "✅ All CI checks passed. Requesting Greptile review."
                        )
                else:
                    # CI failed → create fix task
                    await self._transition(prl, PRStage.CI_FIXING)
                    prl.ci_fix_count += 1

                    # Get failure logs
                    for failure in failures[:3]:  # Max 3 failures to fix
                        logs = await self.github.get_ci_failure_logs(
                            prl.pr_number, failure.get("check_name", "")
                        )
                        task = task_manager.add_task(
                            title=f"[PR {prl.title}] Fix CI: {failure.get('check_name', '')}",
                            description=f"CI check '{failure.get('check_name', '')}' failed.\n\nLogs:\n{logs[:3000]}",
                            priority="high",
                            branch=prl.branch,
                            metadata={"prl_id": prl.id, "type": "ci_fix"},
                        )
                        log_event("pr_lifecycle", "ci_fix_task_created",
                                  prl_id=prl.id, task_id=task.id,
                                  check=failure.get("check_name", ""))

        elif prl.stage == PRStage.CI_FIXING:
            # Fix committed → back to CI monitoring
            await self._transition(prl, PRStage.CI_MONITORING)

        elif prl.stage == PRStage.GREPTILE_REVIEW:
            # Poll for Greptile comments
            if prl.pr_number:
                events = await self.github._check_reviews(prl.pr_number)
                greptile_comments = [
                    e for e in events
                    if e.get("source") == "greptile" and e.get("body", "").strip()
                ]

                if greptile_comments:
                    prl.greptile_comments_total = len(greptile_comments)
                    prl.greptile_comments_resolved = 0
                    await self._transition(prl, PRStage.ADDRESSING_COMMENTS)

                    # Create tasks for each comment
                    for i, comment in enumerate(greptile_comments):
                        task = task_manager.add_task(
                            title=f"[PR {prl.title}] Address Greptile #{i+1}",
                            description=(
                                f"Greptile comment on PR #{prl.pr_number}:\n\n"
                                f"{comment.get('body', '')}\n\n"
                                f"Fix the issue, commit with [conductor:task-ID], "
                                f"and reply to the comment."
                            ),
                            priority="normal",
                            branch=prl.branch,
                            metadata={
                                "prl_id": prl.id,
                                "type": "greptile_fix",
                                "comment": comment,
                            },
                        )
                        log_event("pr_lifecycle", "greptile_task_created",
                                  prl_id=prl.id, task_id=task.id, comment_idx=i)

        elif prl.stage == PRStage.ADDRESSING_COMMENTS:
            # All comments addressed → check iteration count
            prl.iteration += 1
            db.update_pr_lifecycle(prl)

            if prl.iteration >= prl.max_iterations:
                # Max iterations reached → escalate
                await self._transition(prl, PRStage.NEEDS_HUMAN)
                log_event("pr_lifecycle", "max_iterations_reached",
                          level="WARN", prl_id=prl.id,
                          iterations=prl.iteration)
            else:
                # Back to CI monitoring for another round
                await self._transition(prl, PRStage.CI_MONITORING)

        elif prl.stage == PRStage.READY_FOR_REVIEW:
            log_event("pr_lifecycle", "ready_for_human_review",
                      prl_id=prl.id, pr_number=prl.pr_number)

        db.update_pr_lifecycle(prl)
        return prl

    async def create_pr(self, prl_id: int, workspace_path: str = "") -> int | None:
        """Create a GitHub PR for a lifecycle."""
        prl = db.get_pr_lifecycle(prl_id)
        if not prl:
            return None

        body = f"## {prl.title}\n\n_Created by Conductor_\n"
        pr_number = await self.github.create_pr(
            title=prl.title,
            body=body,
            branch=prl.branch,
            base=self.pr_base_branch,
            workspace_path=workspace_path,
        )

        if pr_number:
            prl.pr_number = pr_number
            await self._transition(prl, PRStage.PR_CREATED)
            db.update_pr_lifecycle(prl)

        return pr_number

    async def mark_ready(self, prl_id: int) -> None:
        """Mark a PR lifecycle as ready for human review."""
        prl = db.get_pr_lifecycle(prl_id)
        if prl:
            await self._transition(prl, PRStage.READY_FOR_REVIEW)
            db.update_pr_lifecycle(prl)
            if prl.pr_number:
                await self.github.comment_on_pr(
                    prl.pr_number,
                    "✅ **Ready for human review.** All CI checks pass and "
                    "Greptile comments have been addressed."
                )

    async def _transition(self, prl: PRLifecycle, new_stage: PRStage) -> None:
        """Transition PR lifecycle to a new stage."""
        old = prl.stage
        prl.stage = new_stage
        db.update_pr_lifecycle(prl)

        log_event("pr_lifecycle", "stage_transition",
                  prl_id=prl.id, from_stage=old.value, to_stage=new_stage.value)

        if self._on_stage_change:
            self._on_stage_change(prl)
