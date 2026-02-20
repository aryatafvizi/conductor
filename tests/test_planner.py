"""Tests for planner.py — 100% coverage."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "conductor"))

import json
import pytest
from unittest.mock import patch, MagicMock

import planner as planner_mod


class TestPlanner:
    def setup_method(self):
        # Reset the global client cache between tests
        planner_mod._genai_client = None

    @patch("planner.log_event")
    def test_init_default(self, mock_log):
        from planner import Planner
        p = Planner()
        assert p.conversations == {}
        assert p._api_key == ""
        assert p._model == "gemini-3.1-pro-preview"

    @patch("planner.log_event")
    def test_init_with_config(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key", "gemini_model": "gemini-pro"})
        assert p._api_key == "test-key"
        assert p._model == "gemini-pro"

    @patch("planner.log_event")
    async def test_chat_success(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_part = MagicMock()
        mock_part.text = "Here is my response"

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("planner._get_genai_client", return_value=mock_client), \
             patch("planner.db"):
            result = await p.chat("conv1", "hello")

        assert result == "Here is my response"

    @patch("planner.log_event")
    async def test_chat_empty_response(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_response = MagicMock()
        mock_response.text = ""

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("planner._get_genai_client", return_value=mock_client):
            result = await p.chat("conv1", "hi")

        assert "empty response" in result.lower()

    @patch("planner.log_event")
    async def test_chat_none_text(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_response = MagicMock()
        mock_response.text = None

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("planner._get_genai_client", return_value=mock_client):
            result = await p.chat("conv1", "hi")

        assert "empty response" in result.lower()

    @patch("planner.log_event")
    async def test_chat_exception(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("boom")

        with patch("planner._get_genai_client", return_value=mock_client):
            result = await p.chat("conv1", "hi")

        assert "Planning error" in result

    @patch("planner.log_event")
    async def test_chat_no_api_key(self, mock_log):
        from planner import Planner
        p = Planner()

        with patch.dict("os.environ", {}, clear=True):
            result = await p.chat("conv1", "hi")

        assert "Planning error" in result
        assert "API key" in result or "api_key" in result.lower() or "GEMINI_API_KEY" in result

    @patch("planner.log_event")
    async def test_chat_with_workspace_context(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_part = MagicMock()
        mock_part.text = "response with context"

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("planner._get_genai_client", return_value=mock_client), \
             patch.object(p, "_get_workspace_context", return_value="Context info"), \
             patch("planner.db"):
            result = await p.chat("conv1", "hi",
                                  workspace_name="ws1", workspace_path="/tmp/ws1")
        assert result == "response with context"

    @patch("planner.log_event")
    async def test_chat_builds_conversation_history(self, mock_log):
        from planner import Planner
        p = Planner(config={"gemini_api_key": "test-key"})

        mock_part = MagicMock()
        mock_part.text = "first response"

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        # Pre-populate conversations so db.get_chat_history isn't called
        p.conversations["conv1"] = []

        with patch("planner._get_genai_client", return_value=mock_client), \
             patch("planner.db"):
            await p.chat("conv1", "hello")

        mock_part.text = "second response"
        with patch("planner._get_genai_client", return_value=mock_client), \
             patch("planner.db"):
            await p.chat("conv1", "more")

        assert len(p.conversations["conv1"]) == 4  # 2 user + 2 assistant

    @patch("planner.log_event")
    def test_extract_plan_json_block(self, mock_log):
        from planner import Planner
        p = Planner()
        plan_json = json.dumps({"title": "Fix bug", "branch": "fix/bug"})
        p.conversations["conv1"] = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": f"Here is the plan:\n```json\n{plan_json}\n```"},
        ]
        plan = p.extract_plan("conv1")
        assert plan is not None
        assert plan["title"] == "Fix bug"

    @patch("planner.log_event")
    def test_extract_plan_bare_json(self, mock_log):
        from planner import Planner
        p = Planner()
        plan_json = json.dumps({"title": "T", "branch": "b"})
        p.conversations["conv1"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": f"Plan: {plan_json}"},
        ]
        plan = p.extract_plan("conv1")
        assert plan is not None
        assert plan["title"] == "T"

    @patch("planner.log_event")
    def test_extract_plan_no_json(self, mock_log):
        from planner import Planner
        p = Planner()
        p.conversations["conv1"] = [
            {"role": "assistant", "content": "no json here"},
        ]
        plan = p.extract_plan("conv1")
        assert plan is None

    @patch("planner.log_event")
    def test_extract_plan_invalid_json(self, mock_log):
        from planner import Planner
        p = Planner()
        p.conversations["conv1"] = [
            {"role": "assistant", "content": "```json\n{bad json}\n```"},
        ]
        plan = p.extract_plan("conv1")
        assert plan is None

    @patch("planner.log_event")
    def test_extract_plan_no_conversation(self, mock_log):
        from planner import Planner
        p = Planner()
        assert p.extract_plan("nonexistent") is None

    @patch("planner.log_event")
    def test_get_history(self, mock_log):
        from planner import Planner
        p = Planner()
        assert p.get_history("x") == []
        p.conversations["x"] = [{"role": "user", "content": "hi"}]
        assert len(p.get_history("x")) == 1

    @patch("planner.log_event")
    def test_clear(self, mock_log):
        from planner import Planner
        p = Planner()
        p.conversations["x"] = [{"role": "user", "content": "hi"}]
        p.clear("x")
        assert "x" not in p.conversations
        p.clear("nonexistent")  # should not raise

    @patch("planner.log_event")
    def test_extract_plan_json_block_no_closing_backticks(self, mock_log):
        """Cover line 168: ```json block found, but no closing ```.
        Falls back to content.rfind('}') + 1."""
        from planner import Planner
        p = Planner()
        plan_json = json.dumps({"title": "T", "branch": "b"})
        # Intentionally no closing ```
        p.conversations["conv1"] = [
            {"role": "assistant", "content": f"Plan:\n```json\n{plan_json}\nsome trailing text"},
        ]
        plan = p.extract_plan("conv1")
        assert plan is not None
        assert plan["title"] == "T"


class TestGetGenaiClient:
    def setup_method(self):
        planner_mod._genai_client = None

    @patch("planner.log_event")
    def test_no_api_key_raises(self, mock_log):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
                planner_mod._get_genai_client("")

    @patch("planner.log_event")
    def test_api_key_from_param(self, mock_log):
        mock_genai = MagicMock()
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client

        with patch.dict("sys.modules", {"google": MagicMock(), "google.genai": mock_genai}):
            with patch("planner._genai_client", None):
                # Force reimport to use the mock
                planner_mod._genai_client = None
                # Directly test the function logic
                from google import genai
                with patch("google.genai.Client", return_value=mock_client) as mock_cls:
                    planner_mod._genai_client = None
                    result = planner_mod._get_genai_client("my-key")
                    assert result is not None

    @patch("planner.log_event")
    def test_api_key_from_env(self, mock_log):
        mock_client = MagicMock()
        with patch.dict("os.environ", {"GEMINI_API_KEY": "env-key"}):
            with patch("google.genai.Client", return_value=mock_client):
                planner_mod._genai_client = None
                result = planner_mod._get_genai_client("")
                assert result == mock_client

    @patch("planner.log_event")
    def test_client_cached(self, mock_log):
        sentinel = object()
        planner_mod._genai_client = sentinel
        result = planner_mod._get_genai_client("key")
        assert result is sentinel


class TestGetWorkspaceContext:
    @patch("planner.log_event")
    async def test_workspace_context(self, mock_log):
        from planner import Planner
        p = Planner()

        with patch("subprocess.run") as mock_run:
            # Branch call
            branch_result = MagicMock()
            branch_result.returncode = 0
            branch_result.stdout = "main"
            # Git log call
            log_result = MagicMock()
            log_result.returncode = 0
            log_result.stdout = "abc123 Initial commit"
            # Find call
            find_result = MagicMock()
            find_result.returncode = 0
            find_result.stdout = ".\n./README.md\n./src"

            mock_run.side_effect = [branch_result, log_result, find_result]
            ctx = await p._get_workspace_context("ws1", "/tmp/ws1")
            assert "ws1" in ctx
            assert "main" in ctx
            assert "Initial commit" in ctx
            assert "README.md" in ctx

    @patch("planner.log_event")
    async def test_workspace_context_errors(self, mock_log):
        from planner import Planner
        p = Planner()

        with patch("subprocess.run", side_effect=Exception("fail")):
            ctx = await p._get_workspace_context("ws1", "/tmp/ws1")
            assert "ws1" in ctx

    @patch("planner.log_event")
    async def test_workspace_context_many_files(self, mock_log):
        from planner import Planner
        p = Planner()

        with patch("subprocess.run") as mock_run:
            # All git commands fail except find
            fail_result = MagicMock()
            fail_result.returncode = 1
            fail_result.stdout = ""
            # File tree has lots of files (exceeds 150 truncation)
            find_result = MagicMock()
            find_result.returncode = 0
            find_result.stdout = "\n".join([f"./file{i}.py" for i in range(200)])

            # _get_workspace_context calls: branch, status, log, diff stat,
            # gh pr, branches, find — 7 total subprocess calls
            mock_run.side_effect = [
                fail_result, fail_result, fail_result,  # branch, status, log
                fail_result, fail_result, fail_result,  # diff stat, gh pr, branches
                find_result,                             # find
            ]
            ctx = await p._get_workspace_context("ws1", "/tmp/ws1")
            assert "and 50 more" in ctx
