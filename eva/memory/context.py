
"""Manage EVA's short-term conversation memory."""


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
