"""Fake discord.Message adapter for calling prefix command handlers from slash commands.

Slash interactions don't provide a discord.Message object, but many handler
functions were written expecting one. This minimal adapter shims the fields
those handlers actually use so they work from both prefix commands and slash
commands without modification.
"""

from __future__ import annotations

import types


class FakeMessage:
    """Minimal discord.Message substitute for slash command context.

    Only implements the attributes and methods that handler functions
    actually access. add_reaction / remove_reaction are no-ops in slash
    context (there is no source message to react to).
    """

    def __init__(
        self,
        content: str,
        channel,
        author_id: int,
        guild=None,
        display_name: str = "user",
    ):
        self.content = content
        self.channel = channel
        self.author = types.SimpleNamespace(id=author_id, display_name=display_name)
        self.guild = guild

    async def add_reaction(self, emoji: str) -> None:
        """No-op: slash commands have no source message to react to."""

    async def remove_reaction(self, emoji: str, member=None) -> None:
        """No-op: slash commands have no source message to react to."""
