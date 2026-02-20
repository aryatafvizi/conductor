"""Tests for github_monitor.py — 100% coverage."""
from __future__ import annotations
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import asyncio
import pytest
from unittest.mock import AsyncMock, patch


class TestGitHubMonitorInit:
    def test_init(self):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo", poll_interval=120)
        assert gm.repo == "owner/repo"
        assert gm.poll_interval == 120
        assert gm._running is False


class TestStopPolling:
    def test_stop_polling(self):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        gm._running = True
        gm.stop_polling()
        assert gm._running is False


class TestCheckCIStatus:
    @patch("github_monitor.log_event")
    async def test_ci_success_and_failure(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")

        checks_json = json.dumps([
            {"name": "lint", "state": "completed", "conclusion": "failure"},
            {"name": "test", "state": "completed", "conclusion": "success"},
            {"name": "build", "state": "completed", "conclusion": "neutral"},
        ])
        with patch.object(gm, "_run_gh", AsyncMock(return_value=checks_json)):
            events = await gm._check_ci_status(42)

        failures = [e for e in events if e["type"] == "ci_failure"]
        successes = [e for e in events if e["type"] == "ci_success"]
        assert len(failures) == 1
        assert failures[0]["check_name"] == "lint"
        assert len(successes) == 1

    @patch("github_monitor.log_event")
    async def test_ci_empty(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="")):
            events = await gm._check_ci_status(42)
        assert events == []

    @patch("github_monitor.log_event")
    async def test_ci_exception(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("boom"))):
            events = await gm._check_ci_status(42)
        assert events == []


class TestCheckReviews:
    @patch("github_monitor.log_event")
    async def test_reviews_with_data(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")

        data = {
            "reviews": [
                {"author": {"login": "greptile[bot]"}, "body": "Fix this", "state": "CHANGES_REQUESTED"},
                {"author": {"login": "human"}, "body": "LGTM", "state": "APPROVED"},
                {"author": {"login": "bot"}, "body": "", "state": "COMMENTED"},  # empty body, skipped
            ],
            "comments": [
                {"author": {"login": "greptile-bot"}, "body": "Comment text"},
            ],
        }
        with patch.object(gm, "_run_gh", AsyncMock(return_value=json.dumps(data))):
            events = await gm._check_reviews(42)

        review_events = [e for e in events if e["type"] == "review_comment"]
        assert len(review_events) == 2  # 2 non-empty review bodies
        assert review_events[0]["source"] == "greptile"
        assert review_events[1]["source"] == "human"

        comment_events = [e for e in events if e["type"] == "pr_comment"]
        assert len(comment_events) == 1
        assert comment_events[0]["source"] == "greptile"

    @patch("github_monitor.log_event")
    async def test_reviews_empty(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="")):
            events = await gm._check_reviews(42)
        assert events == []

    @patch("github_monitor.log_event")
    async def test_reviews_exception(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("err"))):
            events = await gm._check_reviews(42)
        assert events == []


class TestGetOpenPRs:
    @patch("github_monitor.log_event")
    async def test_get_open_prs_success(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        prs_json = json.dumps([{"number": 1}, {"number": 2}])
        with patch.object(gm, "_run_gh", AsyncMock(return_value=prs_json)):
            prs = await gm._get_open_prs()
        assert len(prs) == 2

    @patch("github_monitor.log_event")
    async def test_get_open_prs_error(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=RuntimeError("err"))):
            prs = await gm._get_open_prs()
        assert prs == []


class TestCheckOnce:
    @patch("github_monitor.log_event")
    async def test_check_once(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")

        with patch.object(gm, "_get_open_prs", AsyncMock(return_value=[{"number": 1}])), \
             patch.object(gm, "_check_ci_status", AsyncMock(return_value=[{"type": "ci_success"}])), \
             patch.object(gm, "_check_reviews", AsyncMock(return_value=[])):
            events = await gm.check_once()
        assert len(events) == 1

    @patch("github_monitor.log_event")
    async def test_check_once_no_prs(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_get_open_prs", AsyncMock(return_value=[])):
            events = await gm.check_once()
        assert events == []


class TestGetCIFailureLogs:
    @patch("github_monitor.log_event")
    async def test_get_ci_failure_logs_found(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        runs_json = json.dumps([
            {"databaseId": 123, "name": "lint", "conclusion": "failure", "headBranch": "main"}
        ])
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=[runs_json, "log output"])):
            logs = await gm.get_ci_failure_logs(42, "lint")
        assert logs == "log output"

    @patch("github_monitor.log_event")
    async def test_get_ci_failure_logs_not_found(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="[]")):
            logs = await gm.get_ci_failure_logs(42, "noexist")
        assert "No matching" in logs

    @patch("github_monitor.log_event")
    async def test_get_ci_failure_logs_exception(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("fail"))):
            logs = await gm.get_ci_failure_logs(42, "lint")
        assert "Error" in logs


class TestCreatePR:
    @patch("github_monitor.log_event")
    async def test_create_pr_success(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value='{"number": 99}')):
            pr_num = await gm.create_pr("title", "body", "branch")
        assert pr_num == 99

    @patch("github_monitor.log_event")
    async def test_create_pr_failure(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("err"))):
            pr_num = await gm.create_pr("title", "body", "branch")
        assert pr_num is None


class TestRequestReview:
    @patch("github_monitor.log_event")
    async def test_request_review_success(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="")):
            ok = await gm.request_review(42, reviewers=["alice"])
        assert ok is True

    @patch("github_monitor.log_event")
    async def test_request_review_no_reviewers(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="")):
            ok = await gm.request_review(42)
        assert ok is True

    @patch("github_monitor.log_event")
    async def test_request_review_failure(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("err"))):
            ok = await gm.request_review(42)
        assert ok is False


class TestCommentOnPR:
    @patch("github_monitor.log_event")
    async def test_comment_success(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(return_value="")):
            ok = await gm.comment_on_pr(42, "Great work!")
        assert ok is True

    @patch("github_monitor.log_event")
    async def test_comment_failure(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        with patch.object(gm, "_run_gh", AsyncMock(side_effect=Exception("err"))):
            ok = await gm.comment_on_pr(42, "text")
        assert ok is False


class TestRunGh:
    @patch("github_monitor.log_event")
    async def test_run_gh_success(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"output", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await gm._run_gh("version")
        assert result == "output"

    @patch("github_monitor.log_event")
    async def test_run_gh_failure(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo")
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error msg")
        mock_proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            with pytest.raises(RuntimeError, match="error msg"):
                await gm._run_gh("bad-cmd")


class TestStartPolling:
    @patch("github_monitor.log_event")
    async def test_start_polling_with_event(self, mock_log):
        from github_monitor import GitHubMonitor
        gm = GitHubMonitor(repo="owner/repo", poll_interval=0)

        call_count = 0

        async def mock_check_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                gm.stop_polling()
            return [{"type": "ci_success"}]

        mock_on_event = AsyncMock()
        with patch.object(gm, "check_once", side_effect=mock_check_once):
            await gm.start_polling(on_event=mock_on_event)

        assert mock_on_event.call_count >= 1
