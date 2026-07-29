"""Client for submitting AgentRequests to a local agentd."""

from __future__ import annotations

import aiohttp
import logging

from ..protocol import AgentRequest, AgentResponse

log = logging.getLogger(__name__)


class AgentdClient:
    """HTTP client for submitting requests to a local agentd."""

    def __init__(self, base_url: str = "http://127.0.0.1"):
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def submit(self, request: AgentRequest, *, port: int, timeout: float = 1800) -> AgentResponse:
        session = await self._get_session()
        url = f"{self.base_url}:{port}/request"
        try:
            async with session.post(url, data=request.to_json(), timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return AgentResponse(
                        request_id=request.request_id,
                        agent_id=request.agent_id,
                        success=False,
                        error=f"agentd HTTP {resp.status}: {body[:500]}",
                    )
                return AgentResponse.from_json(body)
        except aiohttp.ClientError as exc:
            log.error("agentd client error: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                agent_id=request.agent_id,
                success=False,
                error=f"agentd connection error: {exc}",
            )
        except Exception as exc:
            log.error("agentd unexpected error: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                agent_id=request.agent_id,
                success=False,
                error=f"agentd error: {exc}",
            )

    async def health(self, *, port: int) -> dict:
        session = await self._get_session()
        url = f"{self.base_url}:{port}/health"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
        except Exception as exc:
            return {"error": str(exc), "available": False}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
