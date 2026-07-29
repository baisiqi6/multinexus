"""Button confirmation views for destructive operations.

Provides ConfirmView — a discord.ui.View with Confirm/Cancel buttons
and a 15-minute auto-cancel timeout.

Provides PrivateWikiPromoteView — Promote/Reject buttons for private wiki drafts.
"""

from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 900  # 15 minutes


class ConfirmView(discord.ui.View):
    """Two-button confirmation prompt with auto-cancel timeout.

    Usage:
        view = ConfirmView(author_id=ctx.author.id, action="merge")
        msg = await ctx.send("Merge this branch?", view=view)
        result = await view.wait_for_result()
        if result:
            # user confirmed
        else:
            # user cancelled or timed out
    """

    def __init__(self, *, author_id: int, action: str = "action"):
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.author_id = author_id
        self.action = action
        self.result: bool | None = None
        self._event = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who triggered this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self._event.set()
        self.stop()
        await interaction.response.edit_message(
            content=f"**{self.action.capitalize()}** confirmed.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self._event.set()
        self.stop()
        await interaction.response.edit_message(
            content=f"**{self.action.capitalize()}** cancelled.",
            view=None,
        )

    async def on_timeout(self):
        self.result = False
        self._event.set()
        log.info("Confirm view for %s timed out after %ds", self.action, CONFIRM_TIMEOUT)

    async def wait_for_result(self) -> bool:
        """Block until the user responds or timeout expires. Returns True if confirmed."""
        await self._event.wait()
        return self.result is True


class PrivateWikiPromoteView(discord.ui.View):
    """Promote / Reject buttons for a private wiki draft.

    Sent as a follow-up message after the local agent writes a private draft,
    so the user can approve or discard with one click.
    """

    def __init__(self, *, page_name: str, wiki, author_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.page_name = page_name
        self.wiki = wiki
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who triggered this can respond.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Promote", style=discord.ButtonStyle.success)
    async def promote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        if self.wiki and await self.wiki.promote_private_page(self.page_name):
            await interaction.response.edit_message(
                content=f"✅ Promoted private page `{self.page_name}` to published.", view=None
            )
            log.info("wiki-private: promoted %s via button", self.page_name)
        else:
            await interaction.response.edit_message(
                content=f"❌ No draft found for `{self.page_name}`.", view=None
            )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        if self.wiki and await self.wiki.reject_private_page(self.page_name):
            await interaction.response.edit_message(
                content=f"🗑️ Rejected and deleted draft `{self.page_name}`.", view=None
            )
            log.info("wiki-private: rejected %s via button", self.page_name)
        else:
            await interaction.response.edit_message(
                content=f"❌ No draft found for `{self.page_name}`.", view=None
            )

    async def on_timeout(self):
        log.info("wiki-private promote view for %s timed out", self.page_name)
