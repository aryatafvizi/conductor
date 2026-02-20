"""Planner — conversational planning via Gemini for task/PR design."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import db
from logger import log_event

# Lazy-import google.genai so tests can mock it easily
_genai_client = None

# Key config/doc files to auto-read (checked in order, first match wins per group)
_KEY_FILES = [
    # Documentation
    "README.md", "README.rst", "README.txt", "README",
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    # JavaScript / Node
    "package.json", "tsconfig.json",
    # Go / Rust
    "go.mod", "Cargo.toml",
    # Docker
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    # Config
    ".env.example", ".env", "Makefile",
]

# Directories to always exclude from file tree
_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".eggs", "*.egg-info", ".next", ".nuxt", "target",
}


def _get_genai_client(api_key: str = "") -> Any:
    """Return a cached google.genai.Client, creating on first call."""
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    from google import genai  # type: ignore[import-untyped]

    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "No Gemini API key configured. Set the GEMINI_API_KEY environment "
            "variable or add 'gemini_api_key' to ~/.conductor/config.yaml"
        )

    _genai_client = genai.Client(api_key=key)
    return _genai_client


def _run(cmd: list[str], cwd: str, timeout: int = 10) -> str | None:
    """Run a subprocess and return stdout, or None on failure."""
    import subprocess
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _read_file_safe(path: str, max_lines: int = 200) -> str | None:
    """Read a file safely, returning at most max_lines."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > 500_000:  # skip files > 500KB
            return None
        lines = p.read_text(errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        return "\n".join(lines)
    except Exception:
        return None


def _extract_file_paths(message: str, workspace_path: str) -> list[str]:
    """Extract file paths mentioned in user message that exist on disk."""
    # Match patterns like: path/to/file.ext, ./file.py, src/foo/bar.ts
    candidates = re.findall(r'(?:^|[\s\'"(,])([.\w][\w./\\-]*\.\w+)', message)
    # Also match directory-style references like src/components/
    candidates += re.findall(r'(?:^|[\s\'"(,])([.\w][\w./\\-]*/)(?:\s|$)', message)

    valid = []
    ws = Path(workspace_path)
    for c in candidates:
        full = ws / c
        if full.exists() and str(full).startswith(str(ws)):
            valid.append(str(full))
    return valid[:5]  # cap at 5 files


class Planner:
    """Conversational planner using Gemini to iterate on task scope."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.conversations: dict[str, list[dict[str, str]]] = {}
        self._config = config or {}
        self._api_key = self._config.get("gemini_api_key", "")
        self._model = self._config.get("gemini_model", "gemini-3.1-pro-preview")

    async def chat(
        self, conversation_id: str, user_message: str,
        workspace_name: str = "", workspace_path: str = "",
        model: str = "",
    ) -> str:
        """Send a message to the planning chat and get a response."""
        if conversation_id not in self.conversations:
            # Load from DB on first access
            self.conversations[conversation_id] = db.get_chat_history(
                conversation_id
            )

        self.conversations[conversation_id].append({
            "role": "user",
            "content": user_message,
        })
        db.save_chat_message(conversation_id, "user", user_message)

        # Gather workspace context if a workspace is selected
        workspace_ctx = ""
        if workspace_path:
            workspace_ctx = await self._get_workspace_context(
                workspace_name, workspace_path, user_message
            )

        system_instruction = self._build_system_prompt(workspace_ctx)

        # Build proper multi-turn contents
        history = self.conversations[conversation_id]
        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        try:
            use_model = model or self._model
            client = _get_genai_client(self._api_key)

            from google import genai  # type: ignore[import-untyped]
            config = genai.types.GenerateContentConfig(
                system_instruction=system_instruction,
            )

            response_obj = client.models.generate_content(
                model=use_model,
                contents=contents,
                config=config,
            )

            # Extract text, filtering out any tool-call parts
            response = ""
            if response_obj.candidates:
                parts = response_obj.candidates[0].content.parts or []
                text_parts = []
                for part in parts:
                    if hasattr(part, 'text') and part.text:
                        text_parts.append(part.text)
                response = "\n".join(text_parts).strip()

            if not response:
                response = (
                    "I received an empty response from the model. "
                    "Please try again or rephrase your request."
                )

            self.conversations[conversation_id].append({
                "role": "assistant",
                "content": response,
            })
            db.save_chat_message(conversation_id, "assistant", response)

            log_event("planner", "chat_response",
                      conversation_id=conversation_id,
                      workspace=workspace_name,
                      msg_count=len(self.conversations[conversation_id]))

            return response

        except Exception as e:
            error_msg = f"Planning error: {e}"
            log_event("planner", "chat_error", level="ERROR",
                      error=str(e))
            return error_msg

    def _build_system_prompt(self, workspace_ctx: str) -> str:
        """Build the system instruction for the planner."""
        return f"""You are Conductor's planning assistant — an expert software engineer that helps design and scope coding tasks.

You have FULL ACCESS to the workspace context below. USE IT. Reference actual files, code, commits, and branches by name. Never say "I don't have access" — you do.

{workspace_ctx}

INSTRUCTIONS:
1. Analyze the user's request using the workspace context above (file tree, git history, file contents, etc.).
2. Reference SPECIFIC files, functions, and code from the context in your response.
3. Ask targeted clarifying questions if the scope is ambiguous.
4. Provide a concrete plan: which files to modify, what changes to make, what tests to add, and a branch name following the project's conventions.
5. Call out risks, edge cases, and dependencies.
6. Give COMPLETE, DETAILED responses. Never truncate your output.
7. Do NOT output the JSON plan block until the user explicitly approves (says "approve", "looks good", "go ahead", "ready", etc.).

SYSTEM EVENTS:
- Messages prefixed with [SYSTEM EVENT] are real-time updates about agents, tasks, and PR lifecycles.
- These are NOT user messages — they are automated status updates. Reference them when discussing progress.
- If you see an agent failure event, proactively suggest next steps: retry, modify the plan, or investigate the error.
- If an agent completed successfully, summarize what was achieved and suggest next steps (prechecks, PR review, etc.).

When approved, output:
```json
{{
  "title": "...",
  "branch": "...",
  "files_to_modify": [...],
  "tests_to_write": [...],
  "description": "..."
}}
```"""

    async def _get_workspace_context(
        self, name: str, path: str, user_message: str = ""
    ) -> str:
        """Gather rich context from a workspace."""
        sections: list[str] = []
        sections.append(f"=== WORKSPACE: {name} ({path}) ===")

        # ── Git branch ──────────────────────────────────────────────────
        branch = _run(["git", "branch", "--show-current"], cwd=path)
        if branch:
            sections.append(f"CURRENT BRANCH: {branch}")

        # ── Git status (uncommitted changes) ────────────────────────────
        status = _run(["git", "status", "--short"], cwd=path)
        if status:
            sections.append(f"UNCOMMITTED CHANGES:\n{status}")

        # ── Recent git log (20 commits, with short stats) ──────────────
        log = _run(
            ["git", "log", "--oneline", "--no-decorate", "-20"],
            cwd=path,
        )
        if log:
            sections.append(f"RECENT COMMITS (newest first):\n{log}")

        # ── Git diff stats (last 5 commits) ────────────────────────────
        diff_stat = _run(
            ["git", "diff", "--stat", "HEAD~5", "HEAD", "--", "."],
            cwd=path,
        )
        if diff_stat:
            sections.append(f"RECENT CHANGES (diff stats, last 5 commits):\n{diff_stat}")

        # ── Open PRs (if gh CLI is available) ──────────────────────────
        prs = _run(
            ["gh", "pr", "list", "--limit", "10", "--state", "open",
             "--json", "number,title,headRefName,state",
             "--template",
             '{{range .}}#{{.number}} [{{.headRefName}}] {{.title}}\n{{end}}'],
            cwd=path, timeout=15,
        )
        if prs:
            sections.append(f"OPEN PULL REQUESTS:\n{prs}")

        # ── Remote branches ────────────────────────────────────────────
        branches = _run(
            ["git", "branch", "-r", "--format=%(refname:short)", "--sort=-committerdate"],
            cwd=path,
        )
        if branches:
            branch_list = branches.split("\n")[:15]
            sections.append("RECENT REMOTE BRANCHES:\n" + "\n".join(branch_list))

        # ── File tree (deep, smart exclusions) ─────────────────────────
        exclude_args: list[str] = []
        for d in _EXCLUDE_DIRS:
            exclude_args.extend(["-not", "-path", f"./{d}/*", "-not", "-path", f"./{d}"])
            exclude_args.extend(["-not", "-path", f"./*/{d}/*", "-not", "-path", f"./*/{d}"])

        tree = _run(
            ["find", ".", "-maxdepth", "4"] + exclude_args,
            cwd=path, timeout=10,
        )
        if tree:
            files = tree.split("\n")
            if len(files) > 150:
                files = files[:150] + [f"... and {len(files) - 150} more files"]
            sections.append("FILE TREE:\n" + "\n".join(files))

        # ── Key file contents ──────────────────────────────────────────
        key_contents: list[str] = []
        for fname in _KEY_FILES:
            content = _read_file_safe(os.path.join(path, fname), max_lines=150)
            if content:
                key_contents.append(f"--- {fname} ---\n{content}")
        if key_contents:
            sections.append("KEY FILE CONTENTS:\n" + "\n\n".join(key_contents))

        # ── User-mentioned files ───────────────────────────────────────
        if user_message:
            mentioned = _extract_file_paths(user_message, path)
            if mentioned:
                mentioned_contents: list[str] = []
                for fpath in mentioned:
                    rel = os.path.relpath(fpath, path)
                    content = _read_file_safe(fpath, max_lines=300)
                    if content:
                        mentioned_contents.append(f"--- {rel} ---\n{content}")
                if mentioned_contents:
                    sections.append(
                        "USER-MENTIONED FILE CONTENTS:\n"
                        + "\n\n".join(mentioned_contents)
                    )

        return "\n\n".join(sections)

    def extract_plan(self, conversation_id: str) -> dict[str, Any] | None:
        """Extract the structured plan from conversation history."""
        # Load from DB if not in memory
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = db.get_chat_history(
                conversation_id
            )
        history = self.conversations.get(conversation_id, [])

        # Look for JSON plan block in assistant messages (newest first)
        for msg in reversed(history):
            if msg["role"] != "assistant":
                continue
            content = msg["content"]
            # Find JSON block
            start = content.find("```json")
            if start == -1:
                start = content.find("{")
                end = content.rfind("}") + 1
            else:
                start = content.find("{", start)
                end = content.find("```", start)
                end = content.rfind("}") + 1 if end == -1 else content.rfind("}", start, end) + 1

            if start != -1 and end > start:
                try:
                    plan = json.loads(content[start:end])
                    log_event("planner", "plan_extracted",
                              conversation_id=conversation_id)
                    return plan
                except json.JSONDecodeError:
                    continue

        return None

    def get_history(self, conversation_id: str) -> list[dict[str, str]]:
        """Get conversation history."""
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = db.get_chat_history(
                conversation_id
            )
        return self.conversations.get(conversation_id, [])

    def clear(self, conversation_id: str) -> None:
        """Clear a conversation."""
        self.conversations.pop(conversation_id, None)
        db.delete_chat_history(conversation_id)
