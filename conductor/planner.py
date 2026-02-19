"""Planner — conversational planning via Gemini for task/PR design."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from logger import log_event


class Planner:
    """Conversational planner using Gemini to iterate on task scope."""

    def __init__(self) -> None:
        self.conversations: dict[str, list[dict[str, str]]] = {}

    async def chat(
        self, conversation_id: str, user_message: str,
    ) -> str:
        """Send a message to the planning chat and get a response."""
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = []

        self.conversations[conversation_id].append({
            "role": "user",
            "content": user_message,
        })

        # Build full prompt with conversation history
        history = self.conversations[conversation_id]
        context = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Conductor'}: {m['content']}"
            for m in history
        )

        prompt = f"""You are Conductor's planning assistant. Help the user design their task.

Based on the conversation, provide:
1. A clear scope of work
2. Files likely to be modified
3. Tests to write/update
4. A suggested branch name
5. Any risks or considerations

When the user approves, output a JSON plan block:
```json
{{
  "title": "...",
  "branch": "...",
  "files_to_modify": [...],
  "tests_to_write": [...],
  "description": "..."
}}
```

Conversation:
{context}

Conductor:"""

        try:
            # Use gemini CLI for planning
            proc = await asyncio.create_subprocess_exec(
                "gemini", "-p", prompt, "--output-format", "text",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            response = stdout.decode("utf-8", errors="replace").strip()

            if not response:
                response = "I'm ready to help plan your task. What would you like to build or fix?"

            self.conversations[conversation_id].append({
                "role": "assistant",
                "content": response,
            })

            log_event("planner", "chat_response",
                      conversation_id=conversation_id,
                      msg_count=len(self.conversations[conversation_id]))

            return response

        except Exception as e:
            error_msg = f"Planning error: {e}"
            log_event("planner", "chat_error", level="ERROR",
                      error=str(e))
            return error_msg

    def extract_plan(self, conversation_id: str) -> dict[str, Any] | None:
        """Extract the structured plan from conversation history."""
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
                if end == -1:
                    end = content.rfind("}") + 1
                else:
                    end = content.rfind("}", start, end) + 1

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
        return self.conversations.get(conversation_id, [])

    def clear(self, conversation_id: str) -> None:
        """Clear a conversation."""
        self.conversations.pop(conversation_id, None)
