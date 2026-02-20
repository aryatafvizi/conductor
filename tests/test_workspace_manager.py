"""Tests for workspace_manager.py — 100% coverage with mocked git."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import subprocess
import tempfile
import shutil
import pytest
from unittest.mock import patch, MagicMock

from models import Workspace, WorkspaceStatus


class TestWorkspaceManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create fake workspace dirs
        for i in range(1, 4):
            (Path(self.tmpdir) / f"workspace-{i}").mkdir()
        self.pattern = str(Path(self.tmpdir) / "workspace-*")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("workspace_manager.log_event")
    def test_discover(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        assert len(wm.workspaces) == 3
        assert "workspace-1" in wm.workspaces

    @patch("workspace_manager.log_event")
    def test_discover_skips_files(self, mock_log):
        # Create a file (not dir) matching the pattern
        (Path(self.tmpdir) / "workspace-file").touch()
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        assert "workspace-file" not in wm.workspaces

    @patch("workspace_manager.log_event")
    def test_get_free_workspace(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ws = wm.get_free_workspace()
        assert ws is not None
        assert ws.status == WorkspaceStatus.FREE

    @patch("workspace_manager.log_event")
    def test_get_free_workspace_none(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        for name in list(wm.workspaces.keys()):
            wm.assign(name, 1, "agent-1")
        assert wm.get_free_workspace() is None

    @patch("workspace_manager.log_event")
    def test_assign(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ws = wm.assign("workspace-1", 42, "agent-x")
        assert ws.status == WorkspaceStatus.ASSIGNED
        assert ws.assigned_task_id == 42
        assert ws.agent_id == "agent-x"

    @patch("workspace_manager.log_event")
    def test_release(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        wm.assign("workspace-1", 42, "agent-x")
        ws = wm.release("workspace-1")
        assert ws.status == WorkspaceStatus.FREE
        assert ws.assigned_task_id is None

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_snapshot(self, mock_log, mock_git):
        mock_git.side_effect = ["abc123\n", "No local changes to save"]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        head, has_stash = wm.snapshot("workspace-1")
        assert head == "abc123"
        assert has_stash is False

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_snapshot_with_stash(self, mock_log, mock_git):
        mock_git.side_effect = ["abc123\n", "Saved working directory"]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        head, has_stash = wm.snapshot("workspace-1")
        assert has_stash is True

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_rollback(self, mock_log, mock_git):
        mock_git.return_value = ""
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        wm.workspaces["workspace-1"].snapshot_sha = "abc1234"
        wm.workspaces["workspace-1"].has_stash = True
        ok = wm.rollback("workspace-1")
        assert ok is True
        assert mock_git.call_count >= 2  # reset + stash pop

    @patch("workspace_manager.log_event")
    def test_rollback_no_snapshot(self, mock_log):
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ok = wm.rollback("workspace-1")
        assert ok is False

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_checkout_branch(self, mock_log, mock_git):
        mock_git.return_value = ""
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ok = wm.checkout_branch("workspace-1", "feature/x")
        assert ok is True

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_checkout_branch_create(self, mock_log, mock_git):
        mock_git.side_effect = ["", subprocess.CalledProcessError(1, "git"), ""]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ok = wm.checkout_branch("workspace-1", "new-branch")
        assert ok is True

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_checkout_branch_fail(self, mock_log, mock_git):
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        ok = wm.checkout_branch("workspace-1", "bad")
        assert ok is False

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_get_branch(self, mock_log, mock_git):
        mock_git.return_value = "main\n"
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        branch = wm.get_branch("workspace-1")
        assert branch == "main"

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_get_branch_error(self, mock_log, mock_git):
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        assert wm.get_branch("workspace-1") == ""

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_get_diff_stats(self, mock_log, mock_git):
        mock_git.side_effect = [
            " file1.py | 5 ++---\n 1 file changed\n",
            "+added\n-removed\n context\n",
        ]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        stats = wm.get_diff_stats("workspace-1")
        assert "total_files" in stats
        assert "total_added" in stats
        assert "total_removed" in stats

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_get_diff_stats_error(self, mock_log, mock_git):
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        stats = wm.get_diff_stats("workspace-1")
        assert stats["total_files"] == 0
        assert stats["total_added"] == 0
        assert stats["total_removed"] == 0

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_health_check(self, mock_log, mock_git):
        mock_git.side_effect = ["main\n", " M file.py\n"]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        health = wm.health_check("workspace-1")
        assert health["branch"] == "main"
        assert health["is_dirty"] is True

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_health_check_clean(self, mock_log, mock_git):
        mock_git.side_effect = ["main\n", ""]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        health = wm.health_check("workspace-1")
        assert health["is_dirty"] is False

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_health_check_git_error(self, mock_log, mock_git):
        mock_git.side_effect = [subprocess.CalledProcessError(1, "git"),
                                subprocess.CalledProcessError(1, "git")]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        health = wm.health_check("workspace-1")
        assert health["is_dirty"] is False

    @patch("workspace_manager._run_git")
    @patch("workspace_manager.log_event")
    def test_list_all(self, mock_log, mock_git):
        mock_git.side_effect = ["main\n", "", "main\n", "", "main\n", ""]
        from workspace_manager import WorkspaceManager
        wm = WorkspaceManager(pattern=self.pattern)
        result = wm.list_all()
        assert len(result) == 3


class TestRunGit:
    def test_run_git_success(self):
        from workspace_manager import _run_git
        result = _run_git("/tmp", "version")
        assert "git version" in result

    def test_run_git_failure(self):
        from workspace_manager import _run_git
        with pytest.raises(subprocess.CalledProcessError):
            _run_git("/tmp", "log", "--invalid-nonexistent-flag-xyz")
