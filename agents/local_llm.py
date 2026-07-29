"""Local LLM agent — calls any OpenAI-compatible HTTP endpoint.

Works with LM Studio, Ollama, vLLM, llama.cpp server, or any other backend
that exposes an OpenAI-compatible /chat/completions endpoint.

Typical base URLs:
  LM Studio:  http://localhost:1234/v1
  Ollama:     http://localhost:11434/v1
  vLLM:       http://localhost:8000/v1
"""

import logging

import aiohttp

from .base import AgentOfflineError, AgentTimeoutError, BaseAgent

log = logging.getLogger(__name__)


class LocalLLMAgent(BaseAgent):
    """Agent that calls any OpenAI-compatible local LLM endpoint.

    This is the direct HTTP variant — use OpenClawRelayAgent if you route
    through an OpenClaw gateway instead.

    Parameters:
        base_url:  The base URL of the OpenAI-compatible API (without trailing slash).
        model:     The model ID to request (required by some backends).
        timeout:   Request timeout in seconds.
        api_key:   Optional API key (passed as Bearer token). Leave None for unauthenticated.
        max_tokens: Maximum tokens to generate (None = backend default).
        temperature: Sampling temperature (None = backend default).
    """

    def __init__(
        self,
        base_url: str,
        model: str = "",
        timeout: int = 120,
        api_key: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ):
        super().__init__(name="LocalLLM", timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
        work_dir: str | None = None,
        timeout: int | None = None,
    ) -> tuple[str, dict]:
        """Call the local LLM with the full message history.

        system_prompt is prepended as a system message if provided.
        mission and workspace are appended to the system message if set.
        work_dir is unused (no subprocess invocation).
        """
        effective_timeout = timeout or self.timeout

        # Build system message
        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if mission:
            system_parts.append(f"\n## MISSION\n{mission}")
        if workspace:
            system_parts.append(f"\n## [Working notes]\n{workspace}")

        full_messages = []
        if system_parts:
            full_messages.append({"role": "system", "content": "\n".join(system_parts)})
        full_messages.extend(messages)

        payload: dict = {"messages": full_messages}
        if self.model:
            payload["model"] = self.model
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=effective_timeout),
            ) as resp:
                if resp.status == 401:
                    raise AgentOfflineError(
                        "Local LLM: authentication failed (check api_key / LMSTUDIO_API_KEY)"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise AgentOfflineError(
                        f"Local LLM returned HTTP {resp.status}: {body[:200]}"
                    )
                data = await resp.json()

                usage = data.get("usage", {})
                log.info(
                    "LocalLLM — model: %s, prompt: %d, completion: %d, total: %d",
                    data.get("model", self.model or "unknown"),
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

        except aiohttp.ClientConnectorError as e:
            raise AgentOfflineError(
                f"Cannot reach local LLM at {self.base_url}: {e}"
            ) from e
        except aiohttp.ServerTimeoutError as e:
            raise AgentTimeoutError(
                f"Local LLM did not respond within {effective_timeout}s"
            ) from e

    async def call_streaming(
        self,
        messages: list[dict],
        system_prompt: str,
        on_chunk=None,
        mission: str = "",
        workspace: str = "",
    ) -> tuple[str, dict]:
        """Call the local LLM with SSE streaming.

        Calls on_chunk(accumulated_text) as content chunks arrive.
        Falls back to non-streaming if the backend doesn't support it.
        """
        import json

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        if mission:
            system_parts.append(f"\n## MISSION\n{mission}")
        if workspace:
            system_parts.append(f"\n## [Working notes]\n{workspace}")

        full_messages = []
        if system_parts:
            full_messages.append({"role": "system", "content": "\n".join(system_parts)})
        full_messages.extend(messages)

        payload: dict = {"messages": full_messages, "stream": True}
        if self.model:
            payload["model"] = self.model
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        accumulated: list[str] = []
        usage: dict = {}

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise AgentOfflineError(
                        f"Local LLM returned HTTP {resp.status}: {body[:200]}"
                    )

                async for raw_line in resp.content:
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

        except aiohttp.ClientConnectorError as e:
            raise AgentOfflineError(
                f"Cannot reach local LLM at {self.base_url}: {e}"
            ) from e

        full_text = "".join(accumulated)
        metadata = {
            "tokens_input": usage.get("prompt_tokens"),
            "tokens_output": usage.get("completion_tokens"),
        }
        return full_text, metadata

    async def health_check(self) -> dict:
        """Probe the /models endpoint to verify the server is reachable."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/models",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {"status": "offline", "error": f"HTTP {resp.status}"}
                data = await resp.json()
                models = data.get("data", [])
                model_ids = [m.get("id", "") for m in models[:3]]
                model_str = ", ".join(model_ids) if model_ids else "unknown"
                return {"status": "ok", "model": f"LocalLLM ({model_str})"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}
