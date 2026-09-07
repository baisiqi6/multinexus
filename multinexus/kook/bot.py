"""KOOK bridge bot: connects to KOOK, routes mentions, submits via coordinate.

This is a bridge in the N+M architecture — it handles KOOK Gateway, WebSocket
events, HTTP polling, and mention routing, but delegates agent calls through
coordinate runtime (bridge -> coordinate -> agentd).
"""

import asyncio
import logging
import re
import time
from collections import deque

from khl import Bot, Message, api

from ..agentd.coordinate_client import (
    CoordinateHttpRuntimeClient,
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
    make_coordinate_runtime_client,
)
from ..context.prompt import build_agent_prompt, build_agent_prompt_with_context
from ..context.store import ChatContextStore
from ..models import AgentConfig
from ..protocol import runtime_job_response_text
from ..sessions.scope import (
    workspace_channel_context_scope,
    workspace_channel_session_scope,
)
from .mentions import KookMentionRouter

log = logging.getLogger(__name__)

HANDOFF_LINE_PATTERN = re.compile(r"(?im)^\s*\[handoff\]\s+")
TRANSIENT_BOT_MESSAGE_PREFIXES = (
    "思考中...",
    "Agent 调用失败",
    "Agent error:",
    "Claude timed out after",
    "Claude timeout:",
    "Claude CLI failed",
    "Claude error:",
    "Codex timed out after",
    "Codex model capacity:",
    "Codex CLI failed",
    "OpenCode timed out after",
    "OpenCode CLI failed",
    "Hermes timed out after",
    "Hermes CLI failed",
)


class KookBridge:
    """KOOK bridge that submits requests through coordinate runtime.

    In agentd_mode, submits via coordinate CLI to be claimed by standalone agentd.
    In legacy mode, calls adapters directly for backward compatibility.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.bot = Bot(token=config.token)
        self.context_store = ChatContextStore(config.context_db_path)
        self.router = KookMentionRouter()
        self.started_at_ms = int(time.time() * 1000)
        self.bot_id: str | None = None
        self.bot_role_ids: set[str] = set(config.role_ids)
        self.aliases: set[str] = set(config.aliases)
        self.known_user_names: dict[str, str] = {}
        self.known_role_names: dict[str, str] = self.router.build_role_names(config.known_agents)
        self.seen_message_ids: set[str] = set()
        self.seen_message_order: deque[str] = deque()
        self.poll_error_keys: set[str] = set()
        self.poll_error_last_logged: dict[str, float] = {}
        self._reply_recovery_task: asyncio.Task | None = None

        # Coordinate runtime integration
        self._coordinate_client: CoordinateRuntimeClient | CoordinateHttpRuntimeClient | None = None

        if config.agentd_mode:
            # Single transport factory: cli (default) or http; fail closed on
            # unknown transport or missing fields.
            self._coordinate_client = make_coordinate_runtime_client(config)
        else:
            from ..adapters.factory import make_adapter
            self._adapter = make_adapter(config)

        purged = self.context_store.purge_bot_messages_by_prefixes(
            TRANSIENT_BOT_MESSAGE_PREFIXES
        )
        if purged:
            log.info("Purged %s transient bot messages", purged)
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.on_startup
        async def on_startup(_bot: Bot):
            me = await _bot.client.fetch_me()
            self.bot_id = str(me.id)
            username = getattr(me, "username", "")
            nickname = getattr(me, "nickname", "")
            self.known_user_names[self.bot_id] = nickname or username or self.bot_id
            self.aliases.update(a for a in (username, nickname) if a)
            log.info(
                "KOOK bridge ready: agent=%s id=%s roles=%s agentd_mode=%s",
                self.config.id, me.id, sorted(self.bot_role_ids), self.config.agentd_mode,
            )
            self.bot_role_ids = await self._discover_bot_role_ids(_bot)
            self._start_reply_recovery()
            asyncio.create_task(self._poll_messages(_bot))

        @self.bot.on_message()
        async def on_message(msg: Message):
            await self._handle_text_message(
                message_id=str(msg.id),
                channel_id=str(msg.ctx.channel.id),
                author_id=str(msg.author.id),
                author_name=(
                    getattr(msg.author, "nickname", "")
                    or getattr(msg.author, "username", "")
                    or str(msg.author.id)
                ),
                author_is_bot=bool(msg.author.bot),
                created_at_ms=int(time.time() * 1000),
                content=msg.content,
                mentions=[str(v) for v in getattr(msg, "mention", [])],
                mention_roles=[str(v) for v in getattr(msg, "mention_roles", [])],
                source="ws",
            )

    def run(self) -> None:
        log.info("KOOK bridge starting: agent=%s", self.config.id)
        self.bot.run()

    async def send_channel_message(self, channel_id: str, content: str, quote: str | None = None):
        content = self.router.outbound_for_kook(content, self.config.known_agents)
        params = {"target_id": channel_id, "content": content, "type": 9}
        if quote:
            params["quote"] = quote
        try:
            return await self.bot.client.gate.exec_req(api.Message.create(**params))
        except Exception:
            if not quote:
                raise
            log.warning("Quote reply failed, retrying without quote: channel=%s", channel_id, exc_info=True)
            params.pop("quote", None)
            return await self.bot.client.gate.exec_req(api.Message.create(**params))

    @staticmethod
    def is_transient_bot_message(content: str) -> bool:
        normalized = " ".join(content.strip().split())
        if not normalized:
            return True
        return any(normalized.startswith(p) for p in TRANSIENT_BOT_MESSAGE_PREFIXES)

    @staticmethod
    def is_handoff_message(content: str) -> bool:
        return bool(HANDOFF_LINE_PATTERN.search(content))

    @staticmethod
    def strip_handoff_markers(content: str) -> str:
        return HANDOFF_LINE_PATTERN.sub("", content).strip()

    async def _handle_text_message(
        self,
        *,
        message_id: str,
        channel_id: str,
        author_id: str,
        author_name: str,
        author_is_bot: bool,
        created_at_ms: int,
        content: str,
        mentions: list[str],
        mention_roles: list[str],
        source: str,
    ):
        # Dedup
        if message_id in self.seen_message_ids:
            return
        self._remember_message_id(message_id)

        log.info(
            "KOOK msg[%s]: agent=%s id=%s ch=%s author=%s bot=%s content=%s",
            source, self.config.id, message_id, channel_id, author_id, author_is_bot, content[:200],
        )

        # Filter: skip own messages
        if self.bot_id and author_id == self.bot_id:
            return
        if author_is_bot and not self.config.respond_to_bots:
            return
        if author_is_bot and not self.is_handoff_message(content):
            return

        # Addressed check before any managed resolution or prompt building.
        mentioned = self.router.is_addressed_to_this_bot(
            content=content,
            mentions=mentions,
            mention_roles=mention_roles,
            bot_id=self.bot_id,
            bot_role_ids=self.bot_role_ids,
            aliases=self.aliases,
        )

        # Resolve workspace first so errors can be bounded below.
        resolved_workspace: str | None = None
        if self.config.agentd_mode and self._coordinate_client:
            try:
                resolved_workspace = await self._resolve_channel_workspace(
                    platform="kook", channel_id=channel_id
                )
            except CoordinateRuntimeError as exc:
                log.error(
                    "KOOK channel workspace lookup failed: agent=%s channel=%s error=%s",
                    self.config.id, channel_id, exc,
                )
                if mentioned:
                    await self.send_channel_message(
                        channel_id, "频道 workspace 查询失败，请检查 coordinate 状态。",
                        quote=message_id,
                    )
                return

        if self.config.agentd_mode and resolved_workspace is None:
            log.info(
                "KOOK inbound dropped (unbound): agent=%s channel=%s",
                self.config.id, channel_id,
            )
            if mentioned:
                await self.send_channel_message(
                    channel_id, "此频道未绑定 workspace，无法处理请求。",
                    quote=message_id,
                )
            return

        context_scope = channel_id
        session_scope_id = f"channel:{channel_id}"
        if self.config.agentd_mode and resolved_workspace:
            context_scope = workspace_channel_context_scope(
                workspace_id=resolved_workspace,
                platform="kook",
                channel_id=channel_id,
            )
            session_scope_id = workspace_channel_session_scope(
                workspace_id=resolved_workspace,
                platform="kook",
                channel_id=channel_id,
            )

        rendered_content = self.router.render_for_context(
            content, self.known_user_names, self.known_role_names,
        )
        if not (author_is_bot and self.is_transient_bot_message(content)):
            self.context_store.record_message(
                message_id=message_id,
                channel_id=context_scope,
                author_id=author_id,
                author_name=author_name or author_id,
                author_is_bot=author_is_bot,
                content=rendered_content,
                created_at_ms=created_at_ms,
                source=source,
                ttl_seconds=self.config.context_ttl_seconds,
            )

        if not mentioned:
            return

        # Handoff dedup uses workspace-qualified context scope when managed.
        if (
            author_is_bot
            and self.is_handoff_message(content)
            and self.context_store.has_recent_message(
                channel_id=context_scope,
                author_id=author_id,
                content=rendered_content,
                exclude_message_id=message_id,
                within_seconds=self.config.handoff_dedupe_seconds,
            )
        ):
            return

        # Clean content for agent
        text = self.router.clean_for_agent(
            content=content,
            bot_id=self.bot_id,
            bot_role_ids=self.bot_role_ids,
            aliases=self.aliases,
            known_user_names=self.known_user_names,
            known_role_names=self.known_role_names,
        )
        text = self.strip_handoff_markers(text)
        if not text:
            await self.send_channel_message(channel_id, "你好！@我并说点什么吧~", quote=message_id)
            return

        # Send thinking placeholder
        await self.send_channel_message(channel_id, "思考中...", quote=message_id)

        # Build prompt using workspace-qualified context scope when managed.
        context_envelope = None
        try:
            if self.config.agentd_mode:
                prompt, context_envelope = build_agent_prompt_with_context(
                    context_store=self.context_store,
                    config=self.config,
                    bot_id=self.bot_id,
                    channel_id=context_scope,
                    message_id=message_id,
                    current_text=text,
                    scope_id=context_scope,
                    session_scope_id=session_scope_id,
                    recipient={"id": self.config.id, "name": self.config.display_name},
                )
            else:
                prompt = build_agent_prompt(
                    context_store=self.context_store,
                    config=self.config,
                    bot_id=self.bot_id,
                    channel_id=context_scope,
                    message_id=message_id,
                    current_text=text,
                )
        except Exception:
            log.exception("Prompt build failed: agent=%s", self.config.id)
            return

        # Submit to coordinate or call adapter directly
        managed_job_id: str | None = None
        if self.config.agentd_mode and self._coordinate_client:
            origin = {
                "platform": "kook",
                "destination": channel_id,
                "message_id": message_id,
                "role_id": next(iter(self.bot_role_ids), None),
                "session_scope_id": session_scope_id,
                "legacy_scope_ids": [],
            }
            if isinstance(context_envelope, dict):
                origin["context"] = context_envelope
            reply_json = {
                # The bridge is the visible reply owner, matching Discord.
                # Do not also create a Coordinate delivery for the same result.
                "platform": "none",
                "destination": channel_id,
                "quote_message_id": message_id,
            }
            submit_result = await self._coordinate_client.submit_request(
                target_agent=self.config.id,
                prompt=prompt,
                origin_json=origin,
                reply_json=reply_json,
                workspace_id=resolved_workspace,
                message_id=f"kook:{message_id}",
            )
            submit_error = submit_result.get("error")
            if submit_error:
                await self.send_channel_message(
                    channel_id, f"Agent error: coordinate submit failed: {submit_error}",
                    quote=message_id,
                )
                return

            job_data = submit_result.get("result", {}).get("job")
            job_id = job_data.get("id") if job_data else None
            if not job_id:
                return
            managed_job_id = job_id

            self.context_store.record_runtime_reply(
                job_id=job_id,
                workspace_id=resolved_workspace,
                platform="kook",
                destination_id=channel_id,
                quote_message_id=message_id,
            )

            # Poll coordinate until agentd processes the job
            completed = await self._coordinate_client.wait_for_job_result(
                job_id=job_id,
                workspace_id=resolved_workspace,
                timeout=self.config.timeout,
            )

            if completed is None:
                reply = "Agent timed out (no response from agentd)."
                self._start_reply_recovery()
            else:
                reply = runtime_job_response_text(completed)
        else:
            try:
                reply = await self._adapter.ask(prompt)
            except Exception:
                log.exception("Adapter call failed: agent=%s", self.config.id)
                await self.send_channel_message(channel_id, "Agent 调用失败。", quote=message_id)
                return

        # Send reply in chunks
        for i in range(0, len(reply), 2000):
            await self.send_channel_message(channel_id, reply[i:i + 2000], quote=message_id)
        if managed_job_id is not None and completed is not None:
            self.context_store.mark_runtime_reply_sent(job_id=managed_job_id)

    async def _recover_runtime_replies(self) -> None:
        """Replay KOOK replies left in the bridge-owned local outbox."""
        if not self._coordinate_client:
            return
        pending = self.context_store.pending_runtime_replies(platform="kook")
        for item in pending:
            job_id = item["job_id"]
            self.context_store.mark_runtime_reply_attempt(
                job_id=job_id, error_code="recovery_started"
            )
            try:
                completed = await self._coordinate_client.wait_for_job_result(
                    job_id=job_id,
                    workspace_id=item["workspace_id"],
                    timeout=self.config.timeout,
                )
                if completed is None:
                    continue
                reply = runtime_job_response_text(completed)
                for offset in range(0, len(reply), 2000):
                    await self.send_channel_message(
                        item["destination_id"],
                        reply[offset : offset + 2000],
                        quote=item["quote_message_id"] or None,
                    )
                self.context_store.mark_runtime_reply_sent(job_id=job_id)
            except Exception:
                self.context_store.mark_runtime_reply_attempt(
                    job_id=job_id, error_code="recovery_failed"
                )
                log.warning(
                    "KOOK runtime reply recovery failed: agent=%s job=%s",
                    self.config.id,
                    job_id,
                    exc_info=True,
                )

    def _start_reply_recovery(self) -> None:
        """Schedule one bridge-owned reply replay task at a time."""
        if not self.config.agentd_mode:
            return
        if self._reply_recovery_task is None or self._reply_recovery_task.done():
            self._reply_recovery_task = asyncio.create_task(
                self._recover_runtime_replies()
            )

    def _remember_message_id(self, message_id: str) -> None:
        if message_id in self.seen_message_ids:
            return
        self.seen_message_ids.add(message_id)
        self.seen_message_order.append(message_id)
        while len(self.seen_message_order) > 2000:
            old_id = self.seen_message_order.popleft()
            self.seen_message_ids.discard(old_id)

    async def _resolve_channel_workspace(
        self, *, platform: str, channel_id: str
    ) -> str | None:
        """Resolve (platform, channel_id) -> workspace_id via Coordinate.

        Legacy non-agentd mode never calls this. Coordinate errors raise
        CoordinateRuntimeError and are not downgraded to unbound.
        """
        if not self.config.agentd_mode or self._coordinate_client is None:
            return None
        try:
            return await self._coordinate_client.resolve_channel_workspace(
                platform=platform, channel_id=channel_id
            )
        except Exception as exc:
            raise CoordinateRuntimeError(
                f"KOOK channel workspace resolve failed: {exc}"
            ) from exc

    async def _discover_text_channels(self, _bot: Bot) -> list[str]:
        """Return configured poll channel ids when available, otherwise discover text channels."""
        poll_ids = self.config.kook_poll_channel_ids or []
        if poll_ids:
            return [str(cid) for cid in poll_ids]

        channel_ids: list[str] = []
        guilds = await _bot.client.fetch_guild_list()
        for guild in guilds:
            channels = await _bot.client.gate.exec_req(api.Channel.list(guild.id))
            items = channels.get("items", channels if isinstance(channels, list) else [])
            for channel in items:
                if channel.get("type") == 1:
                    channel_ids.append(str(channel["id"]))
        return channel_ids

    async def _discover_bot_role_ids(self, _bot: Bot) -> set[str]:
        role_ids: set[str] = set(self.config.role_ids)
        if not self.bot_id:
            return role_ids

        guilds = await _bot.client.fetch_guild_list()
        for guild in guilds:
            try:
                data = await _bot.client.gate.exec_req(api.Guild.userList(guild.id))
            except Exception:
                log.exception("Failed to discover bot roles for guild=%s", guild.id)
                continue
            items = data.get("items", data if isinstance(data, list) else [])
            for user in items:
                user_id = str(user.get("id", ""))
                if user_id:
                    self.known_user_names[user_id] = (
                        user.get("nickname") or user.get("username") or user_id
                    )
                if str(user.get("id")) == self.bot_id:
                    role_ids.update(str(rid) for rid in user.get("roles", []))
        return role_ids

    async def _poll_messages(self, _bot: Bot):
        await asyncio.sleep(2)
        channel_ids = await self._discover_text_channels(_bot)
        log.info("KOOK polling enabled for channels: %s", channel_ids)

        while True:
            for channel_id in channel_ids:
                try:
                    data = await _bot.client.gate.request(
                        "GET",
                        "message/list",
                        params={"target_id": channel_id, "page_size": self.config.kook_poll_page_size},
                    )
                except Exception as exc:
                    error_key = f"{channel_id}:{type(exc).__name__}:{str(exc)[:160]}"
                    if error_key not in self.poll_error_keys:
                        self.poll_error_keys.add(error_key)
                        self.poll_error_last_logged[error_key] = time.time()
                        log.exception("Polling failed: channel=%s", channel_id)
                    else:
                        now = time.time()
                        if now - self.poll_error_last_logged.get(error_key, 0) >= 60:
                            self.poll_error_last_logged[error_key] = now
                            log.warning("Polling still failing: agent=%s ch=%s", self.config.id, channel_id)
                    continue

                items = data.get("items", [])
                for item in reversed(items):
                    msg_id = item.get("id")
                    if not msg_id:
                        continue
                    if item.get("create_at", 0) < self.started_at_ms:
                        self._remember_message_id(str(msg_id))
                        continue
                    author = item.get("author", {})
                    await self._handle_text_message(
                        message_id=str(msg_id),
                        channel_id=str(channel_id),
                        author_id=str(author.get("id", "")),
                        author_name=(
                            author.get("nickname")
                            or author.get("username")
                            or str(author.get("id", ""))
                        ),
                        author_is_bot=bool(author.get("bot")),
                        created_at_ms=int(item.get("create_at") or time.time() * 1000),
                        content=item.get("content", ""),
                        mentions=[str(v) for v in item.get("mention", [])],
                        mention_roles=[str(v) for v in item.get("mention_roles", [])],
                        source="poll",
                    )

            await asyncio.sleep(self.config.kook_poll_interval_seconds)
