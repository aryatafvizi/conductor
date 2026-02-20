"""Quota Manager — track AI Ultra usage, pause/resume, countdown to reset."""
from __future__ import annotations

import time
from datetime import datetime

import db
from logger import log_event
from models import QuotaStatus


class QuotaManager:
    """Tracks Gemini AI Ultra quota and manages pause/resume."""

    def __init__(
        self,
        daily_agent_requests: int = 200,
        daily_prompts: int = 1500,
        max_concurrent: int = 3,
        pause_at_percent: int = 90,
        reserve_requests: int = 20,
    ) -> None:
        self.daily_agent_requests = daily_agent_requests
        self.daily_prompts = daily_prompts
        self.max_concurrent = max_concurrent
        self.pause_at_percent = pause_at_percent
        self.reserve_requests = reserve_requests
        self._paused = False
        self._active_agents = 0

    def _today(self) -> str:
        """Get today's date in PT (Google resets at midnight PT)."""
        # Approximate PT as UTC-8 (ignoring DST for simplicity)
        import datetime as dt
        pt_offset = dt.timezone(dt.timedelta(hours=-8))
        return datetime.now(pt_offset).strftime("%Y-%m-%d")

    def get_status(self) -> QuotaStatus:
        """Get current quota status."""
        agent_used, prompts_used = db.get_quota_usage(self._today())
        reset_at = self._next_reset_timestamp()

        return QuotaStatus(
            agent_requests_used=agent_used,
            agent_requests_limit=self.daily_agent_requests,
            prompts_used=prompts_used,
            prompts_limit=self.daily_prompts,
            concurrent_agents=self._active_agents,
            max_concurrent=self.max_concurrent,
            is_paused=self._paused,
            reset_at=reset_at,
        )

    def can_start_agent(self) -> tuple[bool, str]:
        """Check if a new agent can be started within quota."""
        if self._paused:
            return False, "Quota is paused"

        if self._active_agents >= self.max_concurrent:
            return False, f"Max concurrent agents ({self.max_concurrent}) reached"

        agent_used, _ = db.get_quota_usage(self._today())
        effective_limit = self.daily_agent_requests - self.reserve_requests

        if agent_used >= effective_limit:
            self._paused = True
            log_event("quota_manager", "quota_exhausted", level="WARN",
                      agent_used=agent_used, limit=effective_limit)
            return False, f"Agent request quota exhausted ({agent_used}/{effective_limit})"

        # Check pause threshold
        pct = (agent_used / self.daily_agent_requests) * 100
        if pct >= self.pause_at_percent:
            log_event("quota_manager", "quota_threshold_reached", level="WARN",
                      percent=round(pct, 1), threshold=self.pause_at_percent)
            self._paused = True
            return False, f"Quota at {pct:.0f}% (threshold: {self.pause_at_percent}%)"

        return True, "OK"

    def record_agent_request(self, count: int = 1) -> None:
        """Record agent requests used."""
        db.increment_quota(self._today(), agent_requests=count)
        log_event("quota_manager", "request_recorded", count=count)

    def record_prompt(self, count: int = 1) -> None:
        """Record prompts used."""
        db.increment_quota(self._today(), prompts=count)

    def agent_started(self) -> None:
        """Track a new concurrent agent."""
        self._active_agents += 1
        log_event("quota_manager", "agent_started",
                  active=self._active_agents, max=self.max_concurrent)

    def agent_stopped(self) -> None:
        """Track an agent stopping."""
        self._active_agents = max(0, self._active_agents - 1)
        log_event("quota_manager", "agent_stopped",
                  active=self._active_agents)

    def resume(self) -> None:
        """Manually resume after quota pause."""
        self._paused = False
        log_event("quota_manager", "quota_resumed")

    def check_reset(self) -> bool:
        """Check if quota has reset (new day in PT). Auto-resumes if so."""
        today = self._today()
        agent_used, _ = db.get_quota_usage(today)
        if agent_used == 0 and self._paused:
            self._paused = False
            log_event("quota_manager", "quota_auto_reset")
            return True
        return False

    def _next_reset_timestamp(self) -> float:
        """Calculate the Unix timestamp of the next midnight PT."""
        import datetime as dt
        pt_offset = dt.timezone(dt.timedelta(hours=-8))
        now_pt = datetime.now(pt_offset)
        tomorrow_pt = now_pt.replace(
            hour=0, minute=0, second=0, microsecond=0,
        ) + dt.timedelta(days=1)
        return tomorrow_pt.timestamp()

    def time_until_reset(self) -> str:
        """Human-readable time until quota reset."""
        reset_ts = self._next_reset_timestamp()
        remaining = reset_ts - time.time()
        if remaining <= 0:
            return "resetting now"
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        return f"{hours}h {mins}m"
