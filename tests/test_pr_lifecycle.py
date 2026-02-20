"""Tests for pr_lifecycle.py — 100% coverage."""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import db as db_mod
from models import PRLifecycle, PRStage, Task, TaskPriority, TaskStatus


def _reset_db():
    if hasattr(db_mod._local, "conn") and db_mod._local.conn is not None:
        db_mod._local.conn.close()
        db_mod._local.conn = None


@pytest.fixture(autouse=True)
def setup_db():
    _reset_db()
    db_mod.init_db(":memory:")
    yield
    _reset_db()


def _make_github():
    gh = MagicMock()
    gh._check_ci_status = AsyncMock(return_value=[])
    gh._check_reviews = AsyncMock(return_value=[])
    gh.create_pr = AsyncMock(return_value=42)
    gh.comment_on_pr = AsyncMock(return_value=True)
    gh.get_ci_failure_logs = AsyncMock(return_value="error log text")
    return gh


def _make_mgr(on_stage_change=None):
    from pr_lifecycle import PRLifecycleManager
    return PRLifecycleManager(
        github=_make_github(),
        config={},
        on_stage_change=on_stage_change,
    )


class TestInit:
    def test_init_defaults(self):
        mgr = _make_mgr()
        assert mgr.max_greptile_iterations == 3
        assert mgr.pr_base_branch == "main"
        assert mgr.test_commands == []

    def test_init_with_config(self):
        from pr_lifecycle import PRLifecycleManager
        mgr = PRLifecycleManager(
            github=_make_github(),
            config={
                "max_greptile_iterations": 5,
                "max_precheck_retries": 2,
                "max_ci_fix_retries": 1,
                "precheck_command": "make lint",
                "test_commands": ["pytest"],
                "pr_base_branch": "develop",
                "greptile": {"poll_interval": 60},
            },
        )
        assert mgr.max_greptile_iterations == 5
        assert mgr.precheck_command == "make lint"


class TestStartLifecycle:
    @patch("pr_lifecycle.log_event")
    async def test_start(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="Fix bug", branch="fix/bug")
        assert prl.id > 0
        assert prl.title == "Fix bug"
        assert prl.stage == PRStage.CODING

    @patch("pr_lifecycle.log_event")
    async def test_start_with_plan(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b", plan="some plan")
        assert prl.id > 0


class TestAdvance:
    @patch("pr_lifecycle.log_event")
    async def test_advance_not_found(self, mock_log):
        mgr = _make_mgr()
        result = await mgr.advance(999)
        assert result is None

    @patch("pr_lifecycle.log_event")
    async def test_advance_coding_to_prechecks(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.PRECHECKS

    @patch("pr_lifecycle.log_event")
    @patch("pr_lifecycle.task_manager")
    async def test_advance_prechecks_creates_task(self, mock_tm, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        # Move to PRECHECKS
        prl.stage = PRStage.PRECHECKS
        db_mod.update_pr_lifecycle(prl)

        mock_tm.add_task.return_value = MagicMock(id=1)
        result = await mgr.advance(prl.id)
        mock_tm.add_task.assert_called_once()

    @patch("pr_lifecycle.log_event")
    async def test_advance_pr_created_to_ci_monitoring(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.PR_CREATED
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.CI_MONITORING

    @patch("pr_lifecycle.log_event")
    async def test_advance_ci_monitoring_all_pass(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.CI_MONITORING
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        # All CI passes
        mgr.github._check_ci_status.return_value = [
            {"type": "ci_success", "check_name": "lint"}
        ]
        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.GREPTILE_REVIEW

    @patch("pr_lifecycle.log_event")
    @patch("pr_lifecycle.task_manager")
    async def test_advance_ci_monitoring_failure(self, mock_tm, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.CI_MONITORING
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        mgr.github._check_ci_status.return_value = [
            {"type": "ci_failure", "check_name": "lint"},
        ]
        mock_tm.add_task.return_value = MagicMock(id=1)
        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.CI_FIXING

    @patch("pr_lifecycle.log_event")
    async def test_advance_ci_fixing_back_to_monitoring(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.CI_FIXING
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.CI_MONITORING

    @patch("pr_lifecycle.log_event")
    @patch("pr_lifecycle.task_manager")
    async def test_advance_greptile_review_with_comments(self, mock_tm, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.GREPTILE_REVIEW
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        mgr.github._check_reviews.return_value = [
            {"type": "review_comment", "source": "greptile",
             "body": "Fix this", "pr_number": 42},
        ]
        mock_tm.add_task.return_value = MagicMock(id=1)
        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.ADDRESSING_COMMENTS

    @patch("pr_lifecycle.log_event")
    async def test_advance_addressing_comments_max_iterations(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.ADDRESSING_COMMENTS
        prl.iteration = 2
        prl.max_iterations = 3
        db_mod.update_pr_lifecycle(prl)

        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.NEEDS_HUMAN

    @patch("pr_lifecycle.log_event")
    async def test_advance_addressing_comments_continue(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.ADDRESSING_COMMENTS
        prl.iteration = 0
        prl.max_iterations = 3
        db_mod.update_pr_lifecycle(prl)

        result = await mgr.advance(prl.id)
        assert result.stage == PRStage.CI_MONITORING

    @patch("pr_lifecycle.log_event")
    async def test_advance_ready_for_review(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.stage = PRStage.READY_FOR_REVIEW
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        result = await mgr.advance(prl.id)
        assert result is not None


class TestCreatePR:
    @patch("pr_lifecycle.log_event")
    async def test_create_pr_success(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        pr_num = await mgr.create_pr(prl.id, workspace_path="/tmp/ws1")
        assert pr_num == 42

    @patch("pr_lifecycle.log_event")
    async def test_create_pr_not_found(self, mock_log):
        mgr = _make_mgr()
        pr_num = await mgr.create_pr(999)
        assert pr_num is None

    @patch("pr_lifecycle.log_event")
    async def test_create_pr_failed(self, mock_log):
        mgr = _make_mgr()
        mgr.github.create_pr.return_value = None
        prl = await mgr.start_lifecycle(title="T", branch="b")
        pr_num = await mgr.create_pr(prl.id)
        assert pr_num is None


class TestMarkReady:
    @patch("pr_lifecycle.log_event")
    async def test_mark_ready(self, mock_log):
        mgr = _make_mgr()
        prl = await mgr.start_lifecycle(title="T", branch="b")
        prl.pr_number = 42
        db_mod.update_pr_lifecycle(prl)

        await mgr.mark_ready(prl.id)
        updated = db_mod.get_pr_lifecycle(prl.id)
        assert updated.stage == PRStage.READY_FOR_REVIEW

    @patch("pr_lifecycle.log_event")
    async def test_mark_ready_not_found(self, mock_log):
        mgr = _make_mgr()
        await mgr.mark_ready(999)  # no-op


class TestTransition:
    @patch("pr_lifecycle.log_event")
    async def test_transition_with_callback(self, mock_log):
        callback = MagicMock()
        mgr = _make_mgr(on_stage_change=callback)
        prl = await mgr.start_lifecycle(title="T", branch="b")

        await mgr._transition(prl, PRStage.PRECHECKS)
        callback.assert_called_once_with(prl)
        assert prl.stage == PRStage.PRECHECKS
