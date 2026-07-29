"""Mention routing: @mention detection, handoff parsing, Discord mention resolution."""

import re

from ..models import AgentConfig, KnownAgentMention

# [handoff] followed by a target: <@123456>, <@!123456>, or @AgentName
# For Discord mentions: group(1)=id, group(2)=task text
# For text mentions: group(3)=name+task (need alias parsing to split)
_HANDOFF_RE = re.compile(
    r"^\[handoff\]\s*"
    r"(?:"
    r"<@!?(\d+)>\s*(.*)"    # Discord mention + task
    r"|"
    r"@([^\n]+)"              # Text @mention + task (entire rest of line)
    r")"
    r"$",
    re.MULTILINE,
)


def _normalize_target(raw_id: str | None, raw_name: str | None) -> str:
    """Extract a lookup key from a handoff target match group."""
    if raw_id:
        return f"__uid__{raw_id}"
    return (raw_name or "").strip().lower().rstrip(",")


class MentionRouter:
    """Resolves agent names and Discord user IDs to agents, formats handoff mentions."""

    def __init__(self, config: AgentConfig):
        self.config = config
        # name/alias -> KnownAgentMention
        self._name_map: dict[str, KnownAgentMention] = {}
        # discord_user_id (str) -> KnownAgentMention
        self._uid_map: dict[str, KnownAgentMention] = {}
        for agent in config.known_agents:
            self._name_map[agent.id.lower()] = agent
            for name in agent.names:
                self._name_map[name.lower()] = agent
            if agent.discord_user_id is not None:
                self._uid_map[str(agent.discord_user_id)] = agent

    def _resolve_text_mention(self, raw_text: str) -> tuple[KnownAgentMention | None, str]:
        """Try to match a known alias (longest first) from text after @.

        Returns (agent, remaining_task_text) or (None, full_text).
        """
        # Sort aliases by length (longest first) for greedy match
        candidates = sorted(self._name_map.keys(), key=len, reverse=True)
        text_lower = raw_text.strip().lower()
        for candidate in candidates:
            if text_lower.startswith(candidate):
                rest = raw_text.strip()[len(candidate):].strip()
                return self._name_map[candidate], rest
        return None, raw_text

    def _resolve_match(self, match: re.Match) -> tuple[KnownAgentMention | None, str]:
        """Resolve a regex match to (agent, task_text)."""
        raw_id = match.group(1)       # Discord <@id>
        uid_task = (match.group(2) or "").strip()  # Task after Discord mention
        raw_name = match.group(3)     # Text @Name task

        if raw_id:
            agent = self._uid_map.get(raw_id)
            if agent:
                return agent, uid_task

        if raw_name:
            agent, task = self._resolve_text_mention(raw_name)
            return agent, task

        return None, ""

    def is_handoff_message(self, content: str) -> bool:
        """Check if content contains a [handoff] line targeting this agent."""
        for match in _HANDOFF_RE.finditer(content):
            agent, _ = self._resolve_match(match)
            if agent and agent.id == self.config.id:
                return True
        return False

    def extract_handoff_target(self, content: str) -> tuple[str, str] | None:
        """Extract the first handoff targeting this agent."""
        for match in _HANDOFF_RE.finditer(content):
            agent, task_text = self._resolve_match(match)
            if agent and agent.id == self.config.id:
                return (agent.id, task_text.strip())
        return None

    def resolve_handoff_mentions(self, text: str) -> str:
        """Replace @AgentName in handoff lines with Discord <@USER_ID> mentions."""

        def _replace_handoff_line(match: re.Match) -> str:
            raw_id = match.group(1)
            raw_name = match.group(3)

            # Already a Discord mention — leave as-is
            if raw_id:
                return match.group(0)

            # Text mention — try to resolve longest alias match
            agent, task_text = self._resolve_text_mention(raw_name)
            if agent and agent.discord_user_id:
                mention = f"<@{agent.discord_user_id}>"
            elif agent:
                mention = f"@{agent.primary_name}"
            else:
                mention = f"@{raw_name.strip()}"
            task = f" {task_text.strip()}" if task_text.strip() else ""
            return f"[handoff] {mention}{task}"

        return _HANDOFF_RE.sub(_replace_handoff_line, text)

    def extract_handoffs_from_response(
        self, text: str, source_agent_id: str
    ) -> list[tuple[str, str]]:
        """Extract all handoff targets from an agent's response."""
        results = []
        for match in _HANDOFF_RE.finditer(text):
            agent, task_text = self._resolve_match(match)
            if agent and agent.id != source_agent_id:
                results.append((agent.id, task_text.strip()))
        return results

    def matches_bang_command(self, content: str) -> bool:
        """Check if content starts with a !bang alias for this agent."""
        text = content.strip()
        if not text.startswith("!"):
            return False
        parts = text[1:].split(None, 1)
        if not parts:
            return False
        cmd = parts[0].lower()
        if cmd == self.config.id.lower():
            return True
        return cmd in {a.lower() for a in self.config.aliases}

    def strip_bang_prefix(self, content: str) -> str:
        """Remove the !bang prefix and return the remaining text."""
        text = content.strip()
        parts = text.split(None, 1)
        if len(parts) > 1:
            return parts[1].strip()
        return ""

    def update_discord_user_ids(self, id_map: dict[str, int]) -> None:
        """Update discord_user_id for known agents from a {agent_id: user_id} map."""
        for agent in self.config.known_agents:
            uid = id_map.get(agent.id)
            if uid is not None:
                agent.discord_user_id = uid
                self._uid_map[str(uid)] = agent
