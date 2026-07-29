"""OpenClaw relay agent — routes requests through the OpenClaw gateway HTTP endpoint.

OPTIONAL: Only needed if you run an OpenClaw gateway server.
OpenClaw is a self-hosted AI orchestration platform. If you don't use it,
use LocalLLMAgent (local_llm.py) to connect directly to LM Studio or Ollama.

OpenClaw gateway documentation: https://github.com/openclaw/openclaw
"""

import asyncio
import json
import logging
import time

import aiohttp

from .base import AgentOfflineError, AgentTimeoutError, BaseAgent

log = logging.getLogger(__name__)


class OpenClawRelayAgent(BaseAgent):
    """Relays requests through the OpenClaw gateway's OpenAI-compatible HTTP endpoint.

    The system_prompt parameter is intentionally ignored — OpenClaw builds its
    own system prompt from workspace files (SOUL.md, etc.) configured on the server.
    Conversation history is replayed on every call; OpenClaw session state is not used.

    Parameters:
        base_url:    OpenClaw gateway URL, e.g. "http://localhost:18789/v1"
        agent_id:    Workspace/agent ID in your OpenClaw config (default: "main")
        timeout:     Request timeout in seconds
        auth_token:  Bearer token for gateway auth (from OPENCLAW_GATEWAY_TOKEN env var)
        circuit_breaker: Optional CircuitBreaker instance for failure tracking
    """

    def __init__(
        self,
        base_url: str,
        agent_id: str = "main",
        timeout: int = 120,
        auth_token: str | None = None,
        circuit_breaker=None,
    ):
        super().__init__(name="LocalAgent", timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.auth_token = auth_token
        self._circuit = circuit_breaker
        self._gateway_url = self.base_url.removesuffix("/v1").removesuffix("/api")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Content-Type": "application/json",
                "x-openclaw-agent-id": self.agent_id,
            }
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def kill(self) -> None:
        """Abort any in-flight HTTP request by closing the session."""
        await self.close()

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
    ) -> tuple[str, dict]:
        """Call the OpenClaw gateway (non-streaming).

        system_prompt is ignored — OpenClaw owns the agent personality.
        """
        if self._circuit and not self._circuit.is_available():
            remaining = self._circuit.cooldown_seconds - (
                time.monotonic() - self._circuit._opened_at
            )
            raise AgentOfflineError(
                f"Circuit breaker open — retry in {max(0, remaining):.0f}s"
            )

        try:
            result = await self._call_openclaw(messages)
            if self._circuit:
                await self._circuit.record_success()
            return result

        except aiohttp.ClientConnectorError as e:
            if self._circuit:
                await self._circuit.record_failure()
            raise AgentOfflineError(f"Cannot reach OpenClaw gateway: {e}") from e
        except asyncio.TimeoutError as e:
            if self._circuit:
                await self._circuit.record_failure()
            raise AgentTimeoutError(
                f"{self.name} did not respond within {self.timeout}s"
            ) from e

    async def call_streaming(
        self,
        messages: list[dict],
        system_prompt: str,
        on_chunk=None,
        mission: str = "",
        workspace: str = "",
    ) -> tuple[str, dict]:
        """Call OpenClaw with SSE streaming. Calls on_chunk(accumulated_text) as chunks arrive.

        system_prompt is ignored — OpenClaw owns agent personality via SOUL.md.
        """
        if self._circuit and not self._circuit.is_available():
            remaining = self._circuit.cooldown_seconds - (
                time.monotonic() - self._circuit._opened_at
            )
            raise AgentOfflineError(
                f"Circuit breaker open — retry in {max(0, remaining):.0f}s"
            )
        try:
            result = await self._call_openclaw_streaming(messages, on_chunk)
            if self._circuit:
                await self._circuit.record_success()
            return result
        except aiohttp.ClientConnectorError as e:
            if self._circuit:
                await self._circuit.record_failure()
            raise AgentOfflineError(f"Cannot reach OpenClaw gateway: {e}") from e
        except asyncio.TimeoutError as e:
            if self._circuit:
                await self._circuit.record_failure()
            raise AgentTimeoutError(
                f"{self.name} did not respond within {self.timeout}s"
            ) from e

    async def _call_openclaw(self, messages: list[dict]) -> tuple[str, dict]:
        """Execute the non-streaming HTTP request to the OpenClaw gateway."""
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/chat/completions",
            json={"messages": messages},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as resp:
            if resp.status == 401:
                raise AgentOfflineError(
                    "OpenClaw gateway: authentication failed (check OPENCLAW_GATEWAY_TOKEN)"
                )
            if resp.status != 200:
                body = await resp.text()
                raise AgentOfflineError(
                    f"OpenClaw gateway returned {resp.status}: {body[:200]}"
                )
            data = await resp.json()

            usage = data.get("usage", {})
            log.info(
                "OpenClaw relay — model: %s, prompt: %d, completion: %d, total: %d",
                data.get("model", "unknown"),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

            response_text = data["choices"][0]["message"]["content"]
            metadata = {
                "tokens_input": usage.get("prompt_tokens"),
                "tokens_output": usage.get("completion_tokens"),
            }
            return response_text, metadata

    # Seconds of SSE silence before treating the connection as hung.
    _ACTIVITY_TIMEOUT = 90

    async def _call_openclaw_streaming(
        self,
        messages: list[dict],
        on_chunk=None,
    ) -> tuple[str, dict]:
        """Execute streaming HTTP request to the OpenClaw gateway via SSE."""
        session = await self._get_session()
        accumulated: list[str] = []
        usage: dict = {}

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        last_activity = loop.time()

        async with session.post(
            f"{self.base_url}/chat/completions",
            json={"messages": messages, "stream": True},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        ) as resp:
            if resp.status == 401:
                raise AgentOfflineError(
                    "OpenClaw gateway: authentication failed (check OPENCLAW_GATEWAY_TOKEN)"
                )
            if resp.status != 200:
                body = await resp.text()
                raise AgentOfflineError(
                    f"OpenClaw gateway returned {resp.status}: {body[:200]}"
                )

            while True:
                now = loop.time()
                if now >= deadline:
                    raise AgentTimeoutError(
                        f"{self.name} exceeded total timeout of {self.timeout}s"
                    )
                read_timeout = min(self._ACTIVITY_TIMEOUT, deadline - now)
                try:
                    raw_line = await asyncio.wait_for(
                        resp.content.readline(), timeout=read_timeout
                    )
                except asyncio.TimeoutError:
                    if loop.time() - last_activity >= self._ACTIVITY_TIMEOUT:
                        raise AgentTimeoutError(
                            f"{self.name} stopped responding (no activity for {self._ACTIVITY_TIMEOUT}s)"
                        )
                    continue
                if not raw_line:
                    break
                last_activity = loop.time()
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content") or ""
                    )
                    if delta:
                        accumulated.append(delta)
                        if on_chunk is not None:
                            await on_chunk("".join(accumulated))
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

        full_text = "".join(accumulated)
        log.info(
            "OpenClaw relay (stream) — prompt: %d, completion: %d, total: %d",
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        metadata = {
            "tokens_input": usage.get("prompt_tokens"),
            "tokens_output": usage.get("completion_tokens"),
        }
        return full_text, metadata

    async def health_check(self) -> dict:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._gateway_url}/healthz",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {"status": "offline", "error": f"HTTP {resp.status}"}
                return {
                    "status": "ok",
                    "backend": "openclaw",
                    "agent_id": self.agent_id,
                }
        except Exception as e:
            return {"status": "offline", "error": str(e)}
