"""User permission checks via Discord user ID allowlist.

Only users whose IDs appear in `agents.toml` `allowed_user_ids` will be able
to interact with agents or run commands.

To find your Discord user ID: enable Developer Mode in Discord settings,
then right-click your username → Copy User ID.
"""


class Allowlist:
    """Checks whether a Discord user ID is permitted to use the bot."""

    def __init__(self, allowed_ids: list[int]):
        self._allowed = set(allowed_ids)

    def is_allowed(self, user_id: int) -> bool:
        """Return True if the user ID is in the allowlist."""
        return user_id in self._allowed
