"""DiscordClient: one discord.Client per agent, with two-layer message filtering.

In agentd_mode, acts as a bridge: Gateway/mention handling only, adapter calls
go through the local agentd via HTTP. In legacy mode, calls adapters directly.

``DiscordBridge`` (below) hosts N ``DiscordClient`` instances in 1 process and
broadcasts their bot user_ids to build a single shared mention map.
"""

import asyncio
import collections.abc
import logging
import os
import time

import discord
from discord import app_commands

from .adapters.base import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    AdapterResult,
    is_error_text,
    safe_exception_diagnostic,
)
from .adapters.factory import make_adapter
from .agentd.coordinate_client import CoordinateRuntimeClient, CoordinateRuntimeError
from .config import load_config
from .coordinator_handoff import CoordinatorHandoffMixin
from .context.prompt import build_agent_prompt
from .context.store import ChatContextStore
from .models import AgentConfig
from .message_chunks import (
    MAX_DISCORD_MSG_LEN as _MAX_DISCORD_MSG_LEN,
    chunk_message as _chunk_message,
)
from .protocol import AgentRequest, Platform, PlatformDestination, PlatformOrigin
from .routing.mentions import MentionRouter
from .sessions.store import SessionStore
from .sessions.scope import (
    is_thread_channel,
    legacy_scope_for_channel_id,
    scope_for_channel,
    task_scope,
    workspace_channel_context_scope,
    workspace_channel_session_scope,
    workspace_discord_thread_scope,
)

log = logging.getLogger(__name__)

from .commands import (
    can_run_operator_command,
    handle_operator_command,
    parse_operator_command,
)
from .embeds import build_agents_embed, build_health_embed, build_session_status_embed
from .handoff import split_handoff_lines
from .handoff_handler import (
    CoordinatorHandoff,
    build_agent_report,
    build_handoff_prompt,
    build_review_handoff_prompt,
    bootstrap_text_from_accept_output,
    contains_execution_agent_report,
    execute_assignment_accept,
    parse_coordinator_handoff,
    parse_coordinator_lifecycle,
    read_bootstrap,
    resolve_workspace_path,
    split_agent_report_lines,
)


def _resolve_bridge_proxy_url() -> str | None:
    return (
        os.environ.get("MULTINEXUS_HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or None
    )


class DiscordClient(CoordinatorHandoffMixin, discord.Client):
    """One agent = one DiscordClient instance.

    In agentd_mode, acts as a bridge: submits requests to agentd instead of
    calling adapters directly. In legacy mode, manages adapters inline.
    """

    def __init__(self, config: AgentConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        proxy_url = _resolve_bridge_proxy_url()
        super().__init__(intents=intents, proxy=proxy_url)
        self.agent_config = config
        self.context_store = ChatContextStore(config.context_db_path)
        self.mention_router = MentionRouter(config)
        self._bot_user_id_map: dict[str, int] = {}
        self.tree = app_commands.CommandTree(self)
        self._commands_synced = False

        # Bridge mode: submit via coordinate runtime
        self._agentd_mode = config.agentd_mode
        self._coordinate_client: CoordinateRuntimeClient | None = None

        if config.agentd_mode:
            if not config.coordinator_cli_path:
                raise SystemExit(
                    "agentd_mode requires coordinator_cli_path. "
                    "Set it in agents.toml or the [defaults] section."
                )
            self._coordinate_client = CoordinateRuntimeClient(
                cli_path=config.coordinator_cli_path,
                db_path=config.coordinator_db_path,
            )
            self.adapter = None
            self.session_store = None
        else:
            self.adapter = make_adapter(config)
            self.session_store = SessionStore(config.context_db_path)

    async def setup_hook(self):
        self._register_slash_commands()

    async def on_ready(self):
        log.info(
            "DiscordClient ready: agent=%s bot=%s#%s agentd_mode=%s",
            self.agent_config.id,
            self.user.name,
            self.user.discriminator,
            self._agentd_mode,
        )
        self._bot_user_id_map[self.agent_config.id] = self.user.id
        self.mention_router.update_discord_user_ids(self._bot_user_id_map)

        bridge = getattr(self, "_bridge", None)
        if bridge is not None:
            try:
                await bridge._on_client_ready(self)
            except Exception:
                log.warning("bridge _on_client_ready failed", exc_info=True)

        # One-time guild-scoped slash command sync
        if not self._commands_synced:
            guild = None
            if self.agent_config.channels:
                ch = self.get_channel(self.agent_config.channels[0])
                if ch:
                    guild = ch.guild
            if not guild and self.guilds:
                guild = self.guilds[0]
            if guild:
                try:
                    self.tree.copy_global_to(guild=discord.Object(id=guild.id))
                    await self.tree.sync(guild=discord.Object(id=guild.id))
                    self._commands_synced = True
                    log.info("Synced slash commands to guild %s", guild.id)
                except Exception:
                    log.warning("Slash command sync failed", exc_info=True)

    def register_peer_bot(self, agent_id: str, user_id: int) -> None:
        """Called when another agent's bot comes online to build the mention map."""
        self._bot_user_id_map[agent_id] = user_id
        self.mention_router.update_discord_user_ids(self._bot_user_id_map)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Only react to edits from OTHER bots that now contain a handoff to us
        if not after.author.bot:
            return
        if after.author.id == self.user.id:
            return
        channel_id = self._resolve_channel_id(after)
        if self.agent_config.channels and channel_id not in self.agent_config.channels:
            return
        if not self.agent_config.respond_to_bots:
            return
        addressed = self._is_addressed_to_me(after)
        handoff = self.mention_router.is_handoff_message(after.content)
        if not addressed or not handoff:
            return

        # Resolve workspace for managed mode (bound, unbound, or error) or
        # fall through with resolved_workspace=None for legacy non-agentd path.
        if self._agentd_mode:
            resolved_workspace = await self._resolve_or_notify(
                after, channel_id=channel_id, addressed=True
            )
            if resolved_workspace is None:
                return
        else:
            resolved_workspace = None

        log.info(
            "[handoff-edit] agent=%s received edited handoff from=%s",
            self.agent_config.id, after.author,
        )
        self._record_message(after, resolved_workspace=resolved_workspace)
        await self._handle_request(after, resolved_workspace=resolved_workspace)

    async def on_message(self, message: discord.Message):
        # Layer 0: never respond to own messages
        if message.author.id == self.user.id:
            return

        # Layer 1: channel allowlist check (before any processing)
        channel_id = self._resolve_channel_id(message)
        if self.agent_config.channels and channel_id not in self.agent_config.channels:
            return

        addressed = self._is_addressed_to_me(message)

        # Layer 2: bot message filtering
        if message.author.bot:
            if not self.agent_config.respond_to_bots:
                return
            handoff = self.mention_router.is_handoff_message(message.content)
            coordinator_lifecycle = False
            if (
                self.agent_config.coordinator_bot_id
                and message.author.id == self.agent_config.coordinator_bot_id
            ):
                coordinator_lifecycle = (
                    parse_coordinator_lifecycle(
                        message.content,
                        my_discord_user_id=self.user.id,
                    )
                    is not None
                )
            log.debug(
                "[handoff-check] agent=%s from=%s addressed=%s handoff=%s lifecycle=%s mentions=%s content=%.200s",
                self.agent_config.id, message.author, addressed, handoff,
                coordinator_lifecycle,
                [u.id for u in message.mentions], message.content,
            )
            if not addressed:
                return
            if not handoff and not coordinator_lifecycle:
                return

            # Managed mode: resolve workspace before any context/handling/submit.
            if self._agentd_mode:
                resolved_workspace = await self._resolve_or_notify(
                    message, channel_id=channel_id, addressed=True
                )
                if resolved_workspace is None:
                    # None covers both unbound and lookup error; error already logged.
                    return
            else:
                resolved_workspace = None

            # Valid handoff: persist and handle
            self._record_message(message, resolved_workspace=resolved_workspace)

            # Coordinator handoff auto-accept
            if (
                self.agent_config.coordinator_bot_id
                and message.author.id == self.agent_config.coordinator_bot_id
            ):
                handled = await self._try_coordinator_lifecycle(message)
                if handled:
                    return
                handled = await self._try_coordinator_handoff(
                    message,
                    resolved_workspace=resolved_workspace,
                )
                if handled:
                    return

            await self._handle_request(message, resolved_workspace=resolved_workspace)
            return

        # Layer 3: human message
        if self.agent_config.allowed_user_ids and message.author.id not in self.agent_config.allowed_user_ids:
            return

        if not addressed:
            # Managed mode must resolve even not-addressed inbound before writing context.
            if self._agentd_mode:
                resolved_workspace = await self._resolve_silent(channel_id=channel_id)
                if resolved_workspace is not None:
                    self._record_message(message, resolved_workspace=resolved_workspace)
                else:
                    log.info(
                        "Discord not-addressed dropped (unbound/error): agent=%s channel=%s",
                        self.agent_config.id, channel_id,
                    )
            else:
                self._record_message(message)
            return

        # Addressed human: resolve workspace before operator commands / context / submit.
        if self._agentd_mode:
            resolved_workspace = await self._resolve_or_notify(
                message, channel_id=channel_id, addressed=True
            )
            if resolved_workspace is None:
                return
        else:
            resolved_workspace = None

        # Operator command interception
        prompt_text = self._get_prompt_text(message)
        op_cmd = parse_operator_command(prompt_text)
        if op_cmd:
            deny = can_run_operator_command(self.agent_config, message.author.id, op_cmd)
            if deny:
                await message.channel.send(
                    deny,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self._record_message(message, resolved_workspace=resolved_workspace)
            response = await handle_operator_command(
                op_cmd,
                self,
                message.channel.id,
                is_thread=is_thread_channel(message.channel),
            )
            chunks = _chunk_message(response)
            for chunk in chunks:
                try:
                    await message.channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    break
            return

        self._record_message(message, resolved_workspace=resolved_workspace)
        await self._handle_request(message, resolved_workspace=resolved_workspace)

    async def _resolve_or_notify(
        self,
        message: discord.Message,
        *,
        channel_id: int,
        addressed: bool,
    ) -> str | None:
        """Resolve workspace and notify visible on unbound/lookup error when addressed.

        Returns workspace id on bound, None otherwise. Never raises.
        """
        if not self._agentd_mode:
            return None
        try:
            resolved_workspace = await self._resolve_channel_workspace(
                platform="discord", channel_id=channel_id
            )
        except CoordinateRuntimeError as exc:
            log.error(
                "Discord channel workspace lookup failed: agent=%s channel=%s error=%s",
                self.agent_config.id, channel_id, exc,
            )
            if addressed:
                try:
                    await message.channel.send(
                        "频道 workspace 查询失败，请检查 coordinate 状态。",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
            return None

        if resolved_workspace is None:
            log.info(
                "Discord inbound dropped (unbound): agent=%s channel=%s",
                self.agent_config.id, channel_id,
            )
            if addressed:
                try:
                    await message.channel.send(
                        "此频道未绑定 workspace，无法处理请求。",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass
            return None

        return resolved_workspace

    async def _resolve_silent(self, *, channel_id: int) -> str | None:
        """Resolve workspace without any visible output; return None on any failure."""
        if not self._agentd_mode:
            return None
        try:
            return await self._resolve_channel_workspace(
                platform="discord", channel_id=channel_id
            )
        except CoordinateRuntimeError as exc:
            log.info(
                "Discord silent workspace lookup failed: agent=%s channel=%s error=%s",
                self.agent_config.id, channel_id, exc,
            )
            return None

    def _is_addressed_to_me(self, message: discord.Message) -> bool:
        """Check if message @mentions this bot or uses a matching !bang command."""
        if self.user in message.mentions:
            return True
        return self.mention_router.matches_bang_command(message.content)

    @staticmethod
    def _resolve_channel_id(message: discord.Message) -> int:
        """For threads, return the parent channel ID; otherwise the channel ID."""
        if is_thread_channel(message.channel):
            return message.channel.parent_id
        return message.channel.id

    @staticmethod
    def _extract_completed_display_text(completed: dict) -> str:
        """Turn a coord-runtime terminal-job dict into a Discord display string.

        ``coord runtime job report --result-json '{"response_text": ...}'``
        stores the payload in ``jobs.result_json`` (a JSON string in the
        SQLite row). The CLI deserializes that field on read as ``result``
        (dict) — see ``row_to_dict`` in coord/db.py. Older
        ``coord job get`` / direct-SQL callers historically exposed the raw
        ``result_json`` string. Accept both shapes so the bridge does
        not fall back to ``"Job done"`` when a real reply is present.
        """
        result_data = completed.get("result")
        if result_data is None:
            result_json_str = completed.get("result_json")
            if result_json_str:
                try:
                    import json
                    result_data = json.loads(result_json_str)
                except (json.JSONDecodeError, TypeError):
                    result_data = None
        if isinstance(result_data, dict):
            return (
                result_data.get("response_text")
                or result_data.get("text")
                or "(empty response)"
            )
        return f"Job {completed.get('status', 'unknown')}"

    def _record_message(self, message: discord.Message, *, resolved_workspace: str | None = None) -> None:
        """Persist message to context store using workspace-qualified context scope when managed.

        Fail closed: in managed mode a missing resolved_workspace means we must
        not silently fall back to raw channel scope.
        """
        channel_id = self._resolve_channel_id(message)
        if self._agentd_mode:
            if not resolved_workspace:
                raise CoordinateRuntimeError(
                    "managed mode cannot record message without resolved workspace"
                )
            context_scope = workspace_channel_context_scope(
                workspace_id=resolved_workspace,
                platform="discord",
                channel_id=channel_id,
            )
        else:
            context_scope = str(channel_id)
        self.context_store.record_message(
            message_id=str(message.id),
            channel_id=context_scope,
            author_id=str(message.author.id),
            author_name=message.author.display_name,
            author_is_bot=message.author.bot,
            content=message.content,
            created_at_ms=int(message.created_at.timestamp() * 1000),
            source="discord",
            ttl_seconds=self.agent_config.context_ttl_seconds,
        )

    def _get_prompt_text(self, message: discord.Message) -> str:
        """Extract the actual prompt text, stripping @mentions and !bang prefix."""
        content = message.content
        # Strip @mentions of this bot
        content = content.replace(f"<@{self.user.id}>", "").strip()
        content = content.replace(f"<@!{self.user.id}>", "").strip()
        # Strip !bang prefix if present
        if self.mention_router.matches_bang_command(content):
            content = self.mention_router.strip_bang_prefix(content)
        return content.strip()

    async def _run_with_heartbeat(self, placeholder: discord.Message | None, coro, progress_state: dict | None = None):
        """Run an async coroutine while updating a placeholder with elapsed time or partial output."""
        if placeholder is None:
            return await coro

        start = time.time()
        task = asyncio.create_task(coro)
        try:
            while not task.done():
                elapsed = int(time.time() - start)
                mins, secs = divmod(elapsed, 60)
                label = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                # If adapter has produced partial output, show it instead of just "thinking"
                partial = progress_state.get("partial", "") if progress_state else ""
                if partial:
                    preview = partial[:1800]
                    if len(partial) > 1800:
                        preview += "\n..."
                    content = f"\U0001f4dd ({label})\n{preview}"
                else:
                    content = f"\U0001f504 thinking... ({label})"
                try:
                    await placeholder.edit(content=content)
                except discord.HTTPException:
                    pass
                done, _ = await asyncio.wait({task}, timeout=15)
                if done:
                    break
            return task.result()
        except Exception:
            task.cancel()
            raise

    def _register_slash_commands(self):
        """Register /agents, /health, /session status, /session reset."""
        cfg = self.agent_config

        def _is_channel_allowed(interaction: discord.Interaction) -> bool:
            if not cfg.channels:
                return True
            ch = interaction.channel
            ch_id = ch.parent_id if is_thread_channel(ch) else ch.id
            return ch_id in cfg.channels

        @self.tree.command(name="agents", description="列出所有已知 agent")
        async def slash_agents(interaction: discord.Interaction):
            if not _is_channel_allowed(interaction):
                await interaction.response.send_message("此频道不可用。", ephemeral=True)
                return
            deny = can_run_operator_command(cfg, interaction.user.id, "agents")
            if deny:
                await interaction.response.send_message(deny, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            embed = build_agents_embed(cfg)
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True,
            )

        @self.tree.command(name="health", description="检查 adapter 健康状态")
        async def slash_health(interaction: discord.Interaction):
            if not _is_channel_allowed(interaction):
                await interaction.response.send_message("此频道不可用。", ephemeral=True)
                return
            deny = can_run_operator_command(cfg, interaction.user.id, "health")
            if deny:
                await interaction.response.send_message(deny, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            try:
                if self._agentd_mode:
                    health = {
                        "adapter": cfg.adapter, "bin": "(agentd)",
                        "available": True, "agentd_mode": True,
                    }
                else:
                    health = await self.adapter.health_check()
            except Exception as exc:
                health = {
                    "adapter": cfg.adapter, "bin": "?",
                    "available": False, "error": str(exc),
                }
            embed = build_health_embed(cfg, health)
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True,
            )

        session_group = app_commands.Group(name="session", description="会话管理")

        @session_group.command(name="status", description="显示会话状态")
        async def slash_session_status(interaction: discord.Interaction):
            if not _is_channel_allowed(interaction):
                await interaction.response.send_message("此频道不可用。", ephemeral=True)
                return
            deny = can_run_operator_command(cfg, interaction.user.id, "session status")
            if deny:
                await interaction.response.send_message(deny, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            embed = build_session_status_embed(
                self,
                interaction.channel_id,
                is_thread=is_thread_channel(interaction.channel),
            )
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True,
            )

        @session_group.command(name="reset", description="重置当前会话")
        async def slash_session_reset(interaction: discord.Interaction):
            if not _is_channel_allowed(interaction):
                await interaction.response.send_message("此频道不可用。", ephemeral=True)
                return
            deny = can_run_operator_command(cfg, interaction.user.id, "session reset")
            if deny:
                await interaction.response.send_message(deny, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            response = await handle_operator_command(
                "session reset",
                self,
                interaction.channel_id,
                is_thread=is_thread_channel(interaction.channel),
            )
            await interaction.followup.send(
                response, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True,
            )

        self.tree.add_command(session_group)

    def _make_progress_callback(self, progress_state: dict) -> collections.abc.Callable:
        """Create an on_progress callback that writes partial output to a shared dict."""
        def _on_progress(partial_text):
            if isinstance(partial_text, dict):
                progress_state["partial"] = partial_text.get("summary", "")
                if partial_text.get("session_id"):
                    progress_state["session_id"] = partial_text["session_id"]
                return
            progress_state["partial"] = partial_text

        return _on_progress

    async def _run_adapter_for_scope(
        self,
        prompt: str,
        *,
        session_scope_id: str,
        legacy_scope_ids: tuple[str, ...],
        placeholder: discord.Message | None,
        progress_state: dict,
    ) -> AdapterResult:
        """Run call/resume for a canonical scope, with legacy scope fallback."""
        progress_cb = self._make_progress_callback(progress_state)
        existing = self.session_store.get_first_active(
            scope_ids=(session_scope_id, *legacy_scope_ids),
            agent_id=self.agent_config.id,
        )

        try:
            if existing:
                current_work_dir = self.agent_config.work_dir
                if (
                    current_work_dir
                    and existing.get("work_dir")
                    and current_work_dir != existing["work_dir"]
                ):
                    log.info(
                        "Session work_dir mismatch (had=%s now=%s), marking stale for agent=%s scope=%s",
                        existing["work_dir"],
                        current_work_dir,
                        self.agent_config.id,
                        existing["scope_id"],
                    )
                    self.session_store.mark_stale(
                        scope_id=existing["scope_id"],
                        agent_id=self.agent_config.id,
                    )
                    existing = None

            if existing:
                log.info(
                    "Resuming session %s for agent=%s scope=%s",
                    existing["session_id"],
                    self.agent_config.id,
                    existing["scope_id"],
                )
                result: AdapterResult = await self._run_with_heartbeat(
                    placeholder,
                    self.adapter.resume(
                        existing["session_id"],
                        prompt,
                        work_dir=self.agent_config.work_dir,
                        on_progress=progress_cb,
                    ),
                    progress_state=progress_state,
                )
                if result.effective_outcome() != OUTCOME_SUCCESS:
                    allow_fresh_fallback = getattr(
                        self.adapter,
                        "allow_fresh_fallback_after_resume_error",
                        True,
                    )
                    is_internal_error = (
                        getattr(result, "error_category", None) == "internal_error"
                    )
                    log.warning(
                        "Resume failed for session %s (fresh fallback allowed=%s)",
                        existing["session_id"],
                        allow_fresh_fallback,
                    )
                    self.session_store.mark_stale(
                        scope_id=existing["scope_id"],
                        agent_id=self.agent_config.id,
                    )
                    existing = None
                    if allow_fresh_fallback and not is_internal_error:
                        progress_state["partial"] = ""
                        result = await self._run_with_heartbeat(
                            placeholder,
                            self.adapter.call(
                                prompt,
                                work_dir=self.agent_config.work_dir,
                                on_progress=progress_cb,
                            ),
                            progress_state=progress_state,
                        )
                    else:
                        result.session_id = None
            else:
                result = await self._run_with_heartbeat(
                    placeholder,
                    self.adapter.call(
                        prompt,
                        work_dir=self.agent_config.work_dir,
                        on_progress=progress_cb,
                    ),
                    progress_state=progress_state,
                )
        except Exception as exc:
            # Orchestration boundary: an unexpected exception settles the
            # request as an explicit internal_error with a bounded diagnostic.
            # The user-visible text is a safe summary, never the raw exception.
            log.exception("Adapter call failed for agent %s", self.agent_config.id)
            result = AdapterResult(
                text="Agent error: internal adapter failure",
                outcome=OUTCOME_FAILED,
                error_category="internal_error",
                diagnostic=safe_exception_diagnostic(exc),
            )

        if result.session_id:
            self.session_store.upsert(
                scope_id=session_scope_id,
                agent_id=self.agent_config.id,
                adapter=self.agent_config.adapter,
                session_id=result.session_id,
                work_dir=self.agent_config.work_dir,
            )
            if existing and existing["scope_id"] != session_scope_id:
                self.session_store.mark_stale(
                    scope_id=existing["scope_id"],
                    agent_id=self.agent_config.id,
                )

        return result

    async def _handle_request(self, message: discord.Message, *, resolved_workspace: str | None = None):
        """Process an addressed message: call adapter or submit via coordinate."""
        channel = message.channel
        # Session scope: regular messages use channel/thread scope.
        session_scope_id = self._session_scope_for_message(message, resolved_workspace=resolved_workspace)
        legacy_scope_ids = () if self._agentd_mode else (legacy_scope_for_channel_id(channel.id),)
        # Context scope: always use resolved channel id (parent for threads)
        context_channel_id = self._context_scope_for_message(message, resolved_workspace=resolved_workspace)

        # 1. Send placeholder
        placeholder = None
        try:
            placeholder = await channel.send("\U0001f504 thinking...")
        except discord.HTTPException:
            pass

        # 2. Build prompt with context
        prompt_text = self._get_prompt_text(message)
        prompt = build_agent_prompt(
            context_store=self.context_store,
            config=self.agent_config,
            bot_id=self.user.id,
            channel_id=context_channel_id,
            message_id=str(message.id),
            current_text=prompt_text,
        )

        progress_state: dict = {"partial": ""}

        if self._agentd_mode:
            await self._handle_via_agentd(
                prompt, message, placeholder,
                session_scope_id=session_scope_id,
                legacy_scope_ids=legacy_scope_ids,
                context_channel_id=context_channel_id,
                resolved_workspace=resolved_workspace,
            )
            return

        result = await self._run_adapter_for_scope(
            prompt,
            session_scope_id=session_scope_id,
            legacy_scope_ids=legacy_scope_ids,
            placeholder=placeholder,
            progress_state=progress_state,
        )

        await self._send_adapter_response(
            result.text, channel, placeholder,
            context_channel_id=context_channel_id,

            is_error=result.effective_outcome() != OUTCOME_SUCCESS,
        )

    def _session_scope_for_message(
        self,
        message: discord.Message,
        *,
        resolved_workspace: str | None = None,
    ) -> str:
        channel = message.channel
        if self._agentd_mode and resolved_workspace:
            if is_thread_channel(channel):
                return workspace_discord_thread_scope(
                    workspace_id=resolved_workspace,
                    thread_id=channel.id,
                )
            return workspace_channel_session_scope(
                workspace_id=resolved_workspace,
                platform="discord",
                channel_id=channel.id,
            )
        return scope_for_channel(channel)

    def _context_scope_for_message(
        self,
        message: discord.Message,
        *,
        resolved_workspace: str | None = None,
    ) -> str:
        channel_id = self._resolve_channel_id(message)
        if self._agentd_mode and resolved_workspace:
            return workspace_channel_context_scope(
                workspace_id=resolved_workspace,
                platform="discord",
                channel_id=channel_id,
            )
        return str(channel_id)

    async def _handle_via_agentd(
        self,
        prompt: str,
        message: discord.Message,
        placeholder: discord.Message | None,
        *,
        session_scope_id: str,
        legacy_scope_ids: tuple[str, ...],
        context_channel_id: str,
        resolved_workspace: str | None = None,
    ) -> None:
        """Submit a request via coordinate runtime, poll for result, send to Discord."""
        channel = message.channel
        channel_id = str(self._resolve_channel_id(message))
        thread_id = str(message.channel.id) if is_thread_channel(message.channel) else None
        message_id = str(message.id)

        if resolved_workspace is None:
            log.error("agentd submit without resolved workspace: agent=%s channel=%s", self.agent_config.id, channel_id)
            text = "Agent error: channel workspace not resolved"
            if placeholder:
                try:
                    await placeholder.edit(content=text)
                except discord.HTTPException:
                    await channel.send(text)
            return

        origin = {
            "platform": "discord",
            "destination": thread_id or channel_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "session_scope_id": session_scope_id,
            "legacy_scope_ids": list(legacy_scope_ids),
        }
        # agentd_mode: bridge posts the response itself via the agent's
        # DiscordClient. Set reply platform to "none" so coordinate creates
        # a delivery record (for audit) but daemon does not pump it
        # (daemon only pumps discord_webhook). This prevents the coordinator
        # bot from duplicate-posting the agent's response.
        reply_platform = "none" if self._agentd_mode else "discord"
        reply = {
            "platform": reply_platform,
            "destination": thread_id or channel_id,
        }

        submit_result = await self._coordinate_client.submit_request(
            target_agent=self.agent_config.id,
            prompt=prompt,
            origin_json=origin,
            reply_json=reply,
            workspace_id=resolved_workspace,
            message_id=f"discord:{message_id}",
        )

        submit_error = submit_result.get("error")
        if submit_error:
            text = f"Agent error: coordinate submit failed: {submit_error}"
            if placeholder:
                try:
                    await placeholder.edit(content=text)
                except discord.HTTPException:
                    await channel.send(text)
            return

        job_data = submit_result.get("result", {}).get("job")
        job_id = job_data.get("id") if job_data else None
        if not job_id:
            if placeholder:
                try:
                    await placeholder.edit(content="(no job created)")
                except discord.HTTPException:
                    pass
            return

        # Poll coordinate until agentd processes the job
        completed = await self._run_with_heartbeat(
            placeholder,
            self._coordinate_client.wait_for_job_result(
                job_id=job_id,
                workspace_id=resolved_workspace,
                timeout=self.agent_config.timeout,
            ),
        )

        if completed is None:
            display_text = "Agent timed out (no response from agentd)."
        else:
            display_text = self._extract_completed_display_text(completed)

        display_text = self.mention_router.resolve_handoff_mentions(display_text)
        await self._send_text_chunks(channel, placeholder, display_text, [], [])

        if completed and completed.get("status") == "done":
            self.context_store.record_message(
                message_id=f"response:{int(time.time() * 1000)}:{self.agent_config.id}",
                channel_id=context_channel_id,
                author_id=str(self.user.id),
                author_name=self.agent_config.display_name or self.agent_config.id,
                author_is_bot=True,
                content=display_text[:2000],
                created_at_ms=int(time.time() * 1000),
                source="discord",
                ttl_seconds=self.agent_config.context_ttl_seconds,
            )

    async def _resolve_channel_workspace(self, *, platform: str, channel_id: int) -> str | None:
        """Resolve the workspace binding for a managed inbound channel.

        Returns the workspace id, None when unbound, or raises CoordinateRuntimeError
        on Coordinate failures. Legacy non-agentd mode never calls this.
        """
        if not self._agentd_mode or self._coordinate_client is None:
            return None
        try:
            return await self._coordinate_client.resolve_channel_workspace(
                platform=platform,
                channel_id=str(channel_id),
            )
        except CoordinateRuntimeError:
            raise
        except Exception as exc:
            raise CoordinateRuntimeError(f"channel workspace resolve failed: {exc}") from exc

    async def _send_adapter_response(
        self,
        response_text: str,
        channel,
        placeholder: discord.Message | None,
        *,
        context_channel_id: str,
        is_error: bool,
    ) -> None:
        """Send adapter response to Discord channel (legacy mode).

        ``is_error`` comes from the AdapterResult machine outcome; context
        persistence must never reclassify the user-visible text.
        """
        response_text = self.mention_router.resolve_handoff_mentions(response_text)
        handoff_lines, display_text = split_handoff_lines(response_text)

        chunks = _chunk_message(display_text) if display_text else []
        if not chunks and not handoff_lines:
            if placeholder:
                try:
                    await placeholder.edit(content="(no response)")
                except discord.HTTPException:
                    pass
            return

        if chunks:
            if placeholder:
                try:
                    await placeholder.edit(content=chunks[0])
                except discord.HTTPException:
                    await channel.send(chunks[0])
            else:
                await channel.send(chunks[0])
            for chunk in chunks[1:]:
                try:
                    await channel.send(chunk)
                except discord.HTTPException:
                    break
        elif placeholder:
            try:
                await placeholder.edit(content="✅ done")
            except discord.HTTPException:
                pass

        for handoff in handoff_lines:
            try:
                await channel.send(handoff)
            except discord.HTTPException:
                pass

        if not is_error:
            self.context_store.record_message(
                message_id=f"response:{int(time.time() * 1000)}:{self.agent_config.id}",
                channel_id=context_channel_id,
                author_id=str(self.user.id),
                author_name=self.agent_config.display_name or self.agent_config.id,
                author_is_bot=True,
                content=response_text[:2000],
                created_at_ms=int(time.time() * 1000),
                source="discord",
                ttl_seconds=self.agent_config.context_ttl_seconds,
            )

    @staticmethod
    async def _send_text_chunks(
        channel,
        placeholder: discord.Message | None,
        display_text: str,
        handoff_lines: list[str],
        report_lines: list[str],
    ) -> None:
        """Send display text, handoff lines, and report lines to Discord."""
        chunks = _chunk_message(display_text) if display_text else []
        if not chunks and not handoff_lines and not report_lines:
            if placeholder:
                try:
                    await placeholder.edit(content="(no response)")
                except discord.HTTPException:
                    pass
            return

        if chunks:
            if placeholder:
                try:
                    await placeholder.edit(content=chunks[0])
                except discord.HTTPException:
                    await channel.send(chunks[0])
            else:
                await channel.send(chunks[0])
            for chunk in chunks[1:]:
                try:
                    await channel.send(chunk)
                except discord.HTTPException:
                    break
        elif placeholder:
            try:
                await placeholder.edit(content="✅ done")
            except discord.HTTPException:
                pass

        for hl in handoff_lines:
            try:
                await channel.send(hl)
            except discord.HTTPException:
                pass

        for rl in report_lines:
            try:
                await channel.send(rl, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    @classmethod
    def _is_error_response(cls, text: str) -> bool:
        # Legacy/external compatibility seam for plain-text inputs only.
        # AdapterResult consumers must read effective_outcome() instead.
        return is_error_text(text)


class DiscordBridge:
    """Single-process bridge hosting N ``DiscordClient`` instances (1 per agent).

    Each ``DiscordClient`` owns its own Discord gateway connection (per-agent
    bot token). The bridge builds a single in-process mention map by listening
    to each client's ``on_ready`` event and calling ``register_peer_bot`` on
    the others, so mention parsing works without cross-process DB sync.

    Topology: 1 process per platform. Compare to legacy ``DiscordClient``
    which is 1 process per agent.
    """

    def __init__(self, configs: list[AgentConfig]):
        if not configs:
            raise SystemExit("DiscordBridge requires at least one agent config.")
        self.configs = configs
        self.clients: list[DiscordClient] = []
        self._ready_event = asyncio.Event()
        self._all_ready = False

        for cfg in configs:
            client = DiscordClient(cfg)
            client._bridge = self  # type: ignore[attr-defined]
            self.clients.append(client)

    @property
    def agent_ids(self) -> list[str]:
        return [c.agent_config.id for c in self.clients]

    async def _on_client_ready(self, client: DiscordClient) -> None:
        """Called by each client after its on_ready hook runs.

        Propagates the freshly-observed user_id to every other client so the
        shared mention map converges.
        """
        user = client.user
        user_id = getattr(user, "id", None)
        if user_id is None:
            return
        for peer in self.clients:
            if peer is client:
                continue
            try:
                peer.register_peer_bot(client.agent_config.id, user_id)
            except Exception:
                log.warning(
                    "register_peer_bot failed: %s -> %s",
                    client.agent_config.id, peer.agent_config.id, exc_info=True,
                )
        log.info(
            "DiscordBridge ready: agent=%s user_id=%s (peers notified)",
            client.agent_config.id, user_id,
        )

    def is_all_ready(self) -> bool:
        """True if all clients have a bot user_id populated."""
        return all(
            c.user is not None and c.user.id for c in self.clients
        )

    async def start(self) -> None:
        """Start all clients concurrently. Blocks until cancelled or all close."""
        if len(self.clients) == 1:
            await self.clients[0].start(self.clients[0].agent_config.token)
            return
        await asyncio.gather(
            *(c.start(c.agent_config.token) for c in self.clients),
            return_exceptions=True,
        )

    async def close(self) -> None:
        """Close all clients."""
        await asyncio.gather(
            *(c.close() for c in self.clients),
            return_exceptions=True,
        )
