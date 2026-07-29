"""Web Researcher Agent — wraps OpenClawRelayAgent with agent_id='researcher'.

OPTIONAL: Requires an OpenClaw gateway with a researcher workspace configured.
The researcher workspace should have web search tools enabled in your OpenClaw config.

If you don't use OpenClaw, you can implement your own ResearcherAgent by subclassing
BaseAgent and calling a web search API (e.g. Tavily, Perplexity, Brave Search).
"""

import logging
import re

from .openclaw_relay import OpenClawRelayAgent
from security.filter import scan_output

log = logging.getLogger(__name__)

# HTML sanitization patterns — strip script/style blocks and tags
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
# Cap tag length to avoid catastrophic backtracking on malformed HTML
_HTML_TAG_RE = re.compile(r"<[^>]{0,200}>")


class ResearcherAgent(OpenClawRelayAgent):
    """OpenClaw researcher subagent with HTML sanitization.

    Routes to agent_id='researcher' in the OpenClaw gateway, which should have
    its own workspace configured at ~/.openclaw/workspace/researcher/ with
    web search tools enabled.

    Adds HTML sanitization on top of the relay to prevent injection from
    web-sourced content reaching Discord.
    """

    def __init__(
        self,
        base_url: str,
        timeout: int,
        auth_token: str | None = None,
        circuit_breaker=None,
    ):
        super().__init__(
            base_url=base_url,
            agent_id="researcher",
            timeout=timeout,
            auth_token=auth_token,
            circuit_breaker=circuit_breaker,
        )
        # Override the default name set in OpenClawRelayAgent.__init__
        self.name = "Researcher"

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
    ) -> tuple[str, dict]:
        """Call the researcher agent and sanitize the response."""
        raw, metadata = await super().call(
            messages, system_prompt, mission=mission, workspace=workspace
        )
        return self.sanitize(raw), metadata

    def sanitize(self, text: str) -> str:
        """Strip HTML/script tags and scan for leaked secrets."""
        text = _SCRIPT_RE.sub("", text)
        text = _STYLE_RE.sub("", text)
        text = _HTML_TAG_RE.sub("", text)
        return scan_output(text).strip()

    # health_check() is inherited from OpenClawRelayAgent and already
    # returns agent_id="researcher" via self.agent_id. No override needed.
