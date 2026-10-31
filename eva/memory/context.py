
"""Manage EVA's short-term conversation memory."""
from __future__ import annotations

import json
from pathlib import Path


class ConversationMemory:
    """Store recent user and assistant messages."""

    def __init__(self, max_turns=8):
        self.max_turns = max_turns
        self.messages = []

    def add_message(self, role, content):
        """Save a message to conversation history."""

        if role not in ("user", "assistant"):
            raise ValueError("Invalid conversation role.")

        content = content.strip()

        if not content:
            return

        self.messages.append({
            "role": role,
            "content": content
        })

        self._limit_history()

    def _limit_history(self):
        """Keep only the latest conversation messages."""

        max_messages = self.max_turns * 2

        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]

    def get_messages(self, system_prompt):
        """Prepare the complete message list for the AI."""

        return [
            {
                "role": "system",
                "content": system_prompt
            },
            *self.messages
        ]

    def reset(self):
        """Clear the current conversation."""

        self.messages.clear()

    def message_count(self):
        """Return the number of stored messages."""

        return len(self.messages)

    def save_to_file(self, path: str | Path) -> None:
        """Persist conversation history to a JSON file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            json.dump(self.messages, f, ensure_ascii=False, indent=2)

    def load_from_file(self, path: str | Path) -> None:
        """Load conversation history from a JSON file."""
        source = Path(path)
        if not source.exists():
            return
        with source.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            self.messages = [
                m for m in loaded
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
            ]
            self._limit_history()
