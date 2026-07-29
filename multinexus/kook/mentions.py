"""KOOK mention routing: KMarkdown mention format and text mention conversion."""

import re

from ..models import KnownAgentMention


MENTION_PATTERN = re.compile(r"\(met\)(\d+)\(met\)")
ROLE_MENTION_PATTERN = re.compile(r"\(rol\)(\d+)\(rol\)")
TEXT_ROLE_MENTION_PATTERN = re.compile(r"@role:(\d+)")
TEXT_USER_MENTION_PATTERN = re.compile(r"@user:(\d+)")


class KookMentionRouter:
    """KOOK-specific mention routing with KMarkdown format support."""

    @staticmethod
    def is_text_mention(content: str, aliases: set[str]) -> bool:
        return any(f"@{alias}" in content for alias in aliases)

    @staticmethod
    def explicit_mentions(content: str) -> list[tuple[str, str]]:
        mentions: list[tuple[int, str, str]] = []
        mentions.extend(
            (match.start(), "user", match.group(1))
            for match in MENTION_PATTERN.finditer(content)
        )
        mentions.extend(
            (match.start(), "role", match.group(1))
            for match in ROLE_MENTION_PATTERN.finditer(content)
        )
        return [
            (mention_type, mention_id)
            for _, mention_type, mention_id in sorted(mentions, key=lambda item: item[0])
        ]

    @classmethod
    def first_explicit_mention(cls, content: str) -> tuple[str, str] | None:
        mentions = cls.explicit_mentions(content)
        return mentions[0] if mentions else None

    @classmethod
    def is_addressed_to_this_bot(
        cls,
        *,
        content: str,
        mentions: list[str],
        mention_roles: list[str],
        bot_id: str | None,
        bot_role_ids: set[str],
        aliases: set[str],
        first_mention_only: bool = False,
    ) -> bool:
        explicit = cls.explicit_mentions(content)
        if explicit:
            targets = explicit[:1] if first_mention_only else explicit
            for mention_type, mention_id in targets:
                if mention_type == "user" and bot_id and mention_id == bot_id:
                    return True
                if mention_type == "role" and mention_id in bot_role_ids:
                    return True
            return False

        if mentions:
            targets = mentions[:1] if first_mention_only else mentions
            return bool(bot_id and bot_id in {str(t) for t in targets})
        if mention_roles:
            targets = mention_roles[:1] if first_mention_only else mention_roles
            return bool(bot_role_ids.intersection(str(t) for t in mention_roles))
        return cls.is_text_mention(content, aliases)

    @staticmethod
    def clean_for_agent(
        *,
        content: str,
        bot_id: str | None,
        bot_role_ids: set[str],
        aliases: set[str],
        known_user_names: dict[str, str],
        known_role_names: dict[str, str] | None = None,
    ) -> str:
        role_names = known_role_names or {}

        def replace_user_mention(match: re.Match[str]) -> str:
            user_id = match.group(1)
            if bot_id and user_id == bot_id:
                return ""
            name = known_user_names.get(user_id)
            return f"@{name}" if name else f"@user:{user_id}"

        def replace_role_mention(match: re.Match[str]) -> str:
            role_id = match.group(1)
            if role_id in bot_role_ids:
                return ""
            name = role_names.get(role_id)
            return f"@{name}" if name else f"@role:{role_id}"

        text = ROLE_MENTION_PATTERN.sub(
            replace_role_mention,
            MENTION_PATTERN.sub(replace_user_mention, content),
        )
        for alias in sorted(aliases, key=len, reverse=True):
            text = text.replace(f"@{alias}", "")
        return text.strip()

    @staticmethod
    def render_for_context(
        content: str,
        known_user_names: dict[str, str],
        known_role_names: dict[str, str] | None = None,
    ) -> str:
        role_names = known_role_names or {}

        def replace_user_mention(match: re.Match[str]) -> str:
            user_id = match.group(1)
            name = known_user_names.get(user_id)
            return f"@{name}" if name else f"@user:{user_id}"

        def replace_role_mention(match: re.Match[str]) -> str:
            role_id = match.group(1)
            name = role_names.get(role_id)
            return f"@{name}" if name else f"@role:{role_id}"

        return ROLE_MENTION_PATTERN.sub(
            replace_role_mention,
            MENTION_PATTERN.sub(replace_user_mention, content),
        ).strip()

    @staticmethod
    def build_role_names(known_agents: list[KnownAgentMention]) -> dict[str, str]:
        role_names: dict[str, str] = {}
        for agent in known_agents:
            name = agent.primary_name or (max(agent.names, key=len) if agent.names else "")
            if not name:
                continue
            for role_id in agent.role_ids:
                role_names[role_id] = name
        return role_names

    @staticmethod
    def outbound_for_kook(content: str, known_agents: list[KnownAgentMention]) -> str:
        """Convert LLM-friendly mentions back to KOOK native KMarkdown mentions."""
        text = TEXT_USER_MENTION_PATTERN.sub(r"(met)\1(met)", content)
        text = TEXT_ROLE_MENTION_PATTERN.sub(r"(rol)\1(rol)", text)

        name_to_role_id: dict[str, str] = {}
        for agent in known_agents:
            role_id = sorted(agent.role_ids)[0] if agent.role_ids else None
            if not role_id:
                continue
            for name in agent.names:
                name_to_role_id[name] = role_id

        for name in sorted(name_to_role_id, key=len, reverse=True):
            role_id = name_to_role_id[name]
            pattern = re.compile(
                rf"@{re.escape(name)}(?=$|[\s，。,.、:：;；!！?？\n])"
            )
            text = pattern.sub(f"(rol){role_id}(rol)", text)
        return text
