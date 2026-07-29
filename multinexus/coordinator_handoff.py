"""Coordinator handoff and lifecycle orchestration for Discord clients."""
from __future__ import annotations

import asyncio
import logging
import time

import discord

from .handoff import split_handoff_lines
from .handoff_handler import (
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
from .sessions.scope import (
    is_thread_channel,
    task_scope,
    workspace_channel_context_scope,
)

log = logging.getLogger(__name__)


def _chunk_handoff_message(text: str) -> list[str]:
    """Resolve through the historical client chunking hook."""
    from . import client as client_facade

    return client_facade._chunk_message(text)


class CoordinatorHandoffMixin:
    def _resolve_bootstrap_workspace_path(self, handoff) -> str:
        """Return the workspace path used to read the bootstrap file.

        In managed agentd_mode, this uses ONLY the v1 handoff advisory
        workspace_path. It must never fall back to the configured workspace path
        or open the coordinator SQLite database. Legacy non-agentd mode may still
        use the coordinator DB fallback.
        """
        cfg = self.agent_config
        if self._agentd_mode:
            return handoff.workspace_path

        from . import client as client_facade
        return client_facade.resolve_workspace_path(
            db_path=cfg.coordinator_db_path,
            workspace_id=handoff.workspace_id,
            fallback_workspace_path=cfg.coordinator_workspace_path,
        )

    @staticmethod
    def _has_v1_handoff_authority(handoff) -> bool:
        """True when the handoff carries a valid v1 execution context."""
        return (
            handoff.context_version == 1
            and bool(handoff.workspace_path)
            and bool(handoff.harness_root)
        )

    async def _try_coordinator_handoff(
        self,
        message: discord.Message,
        *,
        resolved_workspace: str | None = None,
    ) -> bool:
        """Auto-accept a coordinator handoff. Returns True if handled."""
        # Resolve these through the historical client module so existing
        # operator/test injection hooks remain valid after extraction.
        from . import client as client_facade

        cfg = self.agent_config
        handoff = client_facade.parse_coordinator_handoff(
            message.content,
            my_discord_user_id=self.user.id,
        )
        if handoff is None and self._agentd_mode:
            # In managed mode, a malformed/partial v1 handoff must still be
            # recognized as a candidate so that the authority gate below can
            # emit exactly one visible blocker. The diagnostic parser never
            # treats malformed path/version fields as authority.
            handoff = client_facade.parse_coordinator_handoff(
                message.content,
                my_discord_user_id=self.user.id,
                diagnostic=True,
            )
        if handoff is None:
            return False

        log.info("Coordinator handoff: task=%s agent=%s", handoff.task_id, cfg.id)
        parent_channel_id = self._resolve_channel_id(message)

        # Managed mode: resolved workspace is mandatory before any mutation.
        if self._agentd_mode:
            if not self._has_v1_handoff_authority(handoff):
                error_msg = client_facade.build_agent_report(
                    "blocker",
                    handoff,
                    reason="managed mode requires a complete v1 handoff authority",
                )
                await message.channel.send(
                    error_msg,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log.error(
                    "Managed handoff rejected: incomplete v1 authority for task=%s",
                    handoff.task_id,
                )
                return True
            if resolved_workspace is None:
                error_msg = client_facade.build_agent_report(
                    "blocker",
                    handoff,
                    reason="managed handoff requires a resolved channel workspace",
                )
                await message.channel.send(
                    error_msg,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log.error(
                    "Managed handoff rejected: missing resolved workspace for task=%s",
                    handoff.task_id,
                )
                return True
            if handoff.workspace_id != resolved_workspace:
                error_msg = client_facade.build_agent_report(
                    "blocker",
                    handoff,
                    reason=(
                        f"handoff workspace_id={handoff.workspace_id} does not match "
                        f"channel binding workspace_id={resolved_workspace}"
                    ),
                )
                await message.channel.send(
                    error_msg,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log.error(
                    "Managed handoff rejected: workspace mismatch task=%s handoff=%s binding=%s",
                    handoff.task_id, handoff.workspace_id, resolved_workspace,
                )
                return True
            context_channel_id = workspace_channel_context_scope(
                workspace_id=resolved_workspace,
                platform="discord",
                channel_id=parent_channel_id,
            )
        else:
            context_channel_id = str(parent_channel_id)

        session_scope_id = task_scope(handoff.workspace_id, handoff.task_id)

        # Review handoff: skip assignment.accept, use review.begin flow
        if handoff.action == "review.begin":
            return await self._handle_review_handoff(
                message, handoff, cfg, context_channel_id, session_scope_id,
                resolved_workspace=resolved_workspace,
            )
        # Execute assignment accept
        success, output = await asyncio.to_thread(
            client_facade.execute_assignment_accept,
            cli_path=cfg.coordinator_cli_path,
            db_path=cfg.coordinator_db_path,
            workspace_id=handoff.workspace_id,
            task_id=handoff.task_id,
            agent_name=cfg.id,
        )

        if not success:
            error_msg = client_facade.build_agent_report(
                "blocker",
                handoff,
                reason=f"assignment accept failed: {output[:500]}",
            )
            await message.channel.send(
                error_msg,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log.error("Assignment accept failed: %s", output)
            return True

        # Prefer bootstrap_text returned by coordinate assignment accept. This
        # avoids reading target-agent bootstrap through the bridge deploy path.
        bootstrap_content = client_facade.bootstrap_text_from_accept_output(output)
        if bootstrap_content is None and handoff.bootstrap_path:
            bootstrap_workspace_path = self._resolve_bootstrap_workspace_path(handoff)
            if bootstrap_workspace_path:
                bootstrap_content = await asyncio.to_thread(
                    client_facade.read_bootstrap,
                    bootstrap_workspace_path,
                    handoff.bootstrap_path,
                )

        # Managed mode must have readable bootstrap content after assignment-accept
        # output/file resolution. Without it, fail closed instead of falling back
        # to configured paths or coordinator SQLite.
        if self._agentd_mode:
            if bootstrap_content is None:
                error_msg = client_facade.build_agent_report(
                    "blocker",
                    handoff,
                    reason="managed mode requires bootstrap content",
                )
                await message.channel.send(
                    error_msg,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log.error("Managed handoff rejected: missing bootstrap content for task=%s", handoff.task_id)
                return True

        # Build prompt and call adapter
        prompt = client_facade.build_handoff_prompt(
            handoff,
            bootstrap_content,
            agent_name=cfg.id,
            accept_output=output,
        )

        # Confirm acceptance
        await message.channel.send(
            client_facade.build_agent_report(
                "accept",
                handoff,
                summary=f"auto accepted by {cfg.id}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        # Call adapter with bootstrap prompt
        channel = message.channel

        placeholder = None
        try:
            placeholder = await channel.send("\U0001f504 working on task...")
        except discord.HTTPException:
            pass

        if self._agentd_mode:
            response_text, is_error = await self._run_handoff_via_agentd(
                handoff,
                prompt,
                message,
                placeholder,
                session_scope_id=session_scope_id,
                resolved_workspace=resolved_workspace,
            )
        else:
            progress_state: dict = {"partial": ""}
            result = await self._run_adapter_for_scope(
                prompt,
                session_scope_id=session_scope_id,
                legacy_scope_ids=(),
                placeholder=placeholder,
                progress_state=progress_state,
            )
            response_text = result.text
            is_error = self._is_error_response(result.text)

        # Send response
        response_text = self.mention_router.resolve_handoff_mentions(response_text)
        report_lines, response_without_reports = client_facade.split_agent_report_lines(
            response_text
        )
        handoff_lines, display_text = client_facade.split_handoff_lines(
            response_without_reports
        )

        chunks = _chunk_handoff_message(display_text) if display_text else []
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

        if not self._agentd_mode:
            for report_line in report_lines:
                try:
                    await channel.send(
                        report_line,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

        if not is_error:
            self.context_store.record_message(
                message_id=f"response:{int(time.time() * 1000)}:{cfg.id}",
                channel_id=context_channel_id,
                author_id=str(self.user.id),
                author_name=cfg.display_name or cfg.id,
                author_is_bot=True,
                content=response_text[:2000],
                created_at_ms=int(time.time() * 1000),
                source="discord",
                ttl_seconds=cfg.context_ttl_seconds,
            )

        await self._send_missing_report_fallback(
            channel,
            handoff,
            response_text=response_text,
            is_error=is_error,
        )
        return True

    async def _handle_review_handoff(
        self,
        message: discord.Message,
        handoff,
        cfg,
        context_channel_id: str,
        session_scope_id: str,
        *,
        resolved_workspace: str | None = None,
    ) -> bool:
        """Handle a review.begin handoff — reviewer reads plan, does NOT claim ownership.

        The authoritative workspace gate lives in ``_try_coordinator_handoff``;
        ``resolved_workspace`` reaching this helper is already validated.
        """
        from . import client as client_facade

        log.info("Review handoff: task=%s reviewer=%s", handoff.task_id, cfg.id)

        # Managed mode requires a complete v1 handoff authority before any
        # bootstrap read, SQLite fallback, or reviewer adapter/agentd invocation.
        if self._agentd_mode and not self._has_v1_handoff_authority(handoff):
            error_msg = client_facade.build_agent_report(
                "blocker",
                handoff,
                reason="managed mode requires a complete v1 handoff authority",
            )
            await message.channel.send(
                error_msg,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log.error(
                "Managed review handoff rejected: incomplete v1 authority for task=%s",
                handoff.task_id,
            )
            return True

        # Read bootstrap directly (no assignment accept)
        bootstrap_workspace_path = self._resolve_bootstrap_workspace_path(handoff)

        bootstrap_content = None
        if handoff.bootstrap_path and bootstrap_workspace_path:
            bootstrap_content = await asyncio.to_thread(
                client_facade.read_bootstrap,
                bootstrap_workspace_path,
                handoff.bootstrap_path,
            )

        # Managed review handoffs must resolve readable bootstrap content. Without
        # it, fail closed instead of invoking the reviewer adapter.
        if self._agentd_mode:
            if bootstrap_content is None:
                error_msg = client_facade.build_agent_report(
                    "blocker",
                    handoff,
                    reason="managed review handoff requires bootstrap content",
                )
                await message.channel.send(
                    error_msg,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                log.error("Managed review handoff rejected: missing bootstrap content for task=%s", handoff.task_id)
                return True

        # Build review-specific prompt
        prompt = client_facade.build_review_handoff_prompt(
            handoff,
            bootstrap_content,
            agent_name=cfg.id,
        )

        # Confirm review acceptance
        await message.channel.send(
            client_facade.build_agent_report(
                "accept",
                handoff,
                summary=f"review begun by {cfg.id}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        channel = message.channel
        placeholder = None
        try:
            placeholder = await channel.send("\U0001f504 reviewing...")
        except discord.HTTPException:
            pass

        if self._agentd_mode:
            response_text, is_error = await self._run_handoff_via_agentd(
                handoff,
                prompt,
                message,
                placeholder,
                session_scope_id=session_scope_id,
                resolved_workspace=resolved_workspace,
            )
        else:
            progress_state: dict = {"partial": ""}
            result = await self._run_adapter_for_scope(
                prompt,
                session_scope_id=session_scope_id,
                legacy_scope_ids=(),
                placeholder=placeholder,
                progress_state=progress_state,
            )
            response_text = result.text
            is_error = self._is_error_response(result.text)

        response_text = self.mention_router.resolve_handoff_mentions(response_text)
        report_lines, response_without_reports = client_facade.split_agent_report_lines(
            response_text
        )
        handoff_lines, display_text = client_facade.split_handoff_lines(
            response_without_reports
        )

        chunks = _chunk_handoff_message(display_text) if display_text else []
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
                await placeholder.edit(content="\u2705 review complete")
            except discord.HTTPException:
                pass

        for hl in handoff_lines:
            try:
                await channel.send(hl)
            except discord.HTTPException:
                pass

        if not self._agentd_mode:
            for report_line in report_lines:
                try:
                    await channel.send(
                        report_line,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

        if not is_error:
            self.context_store.record_message(
                message_id=f"review:{int(time.time() * 1000)}:{cfg.id}",
                channel_id=context_channel_id,
                author_id=str(self.user.id),
                author_name=cfg.display_name or cfg.id,
                author_is_bot=True,
                content=response_text[:2000],
                created_at_ms=int(time.time() * 1000),
                source="discord",
                ttl_seconds=cfg.context_ttl_seconds,
            )

        await self._send_missing_report_fallback(
            channel,
            handoff,
            response_text=response_text,
            is_error=is_error,
        )
        return True

    async def _run_handoff_via_agentd(
        self,
        handoff: CoordinatorHandoff,
        prompt: str,
        message: discord.Message,
        placeholder: discord.Message | None,
        *,
        session_scope_id: str,
        resolved_workspace: str | None = None,
    ) -> tuple[str, bool]:
        """Submit a coordinator handoff prompt through the agentd runtime path."""
        if self._coordinate_client is None:
            return "Agent error: coordinate runtime client is not configured", True

        if resolved_workspace is None:
            return "Agent error: channel workspace not resolved", True

        channel_id = str(self._resolve_channel_id(message))
        thread_id = str(message.channel.id) if is_thread_channel(message.channel) else None
        message_id = str(message.id)
        destination = thread_id or channel_id

        origin = {
            "platform": "discord",
            "destination": destination,
            "message_id": message_id,
            "thread_id": thread_id,
            "session_scope_id": session_scope_id,
            "legacy_scope_ids": [],
            "handoff": {
                "workspace_id": handoff.workspace_id,
                "task_id": handoff.task_id,
            },
        }
        reply = {
            "platform": "none",
            "destination": destination,
        }

        submit_result = await self._coordinate_client.submit_request(
            target_agent=self.agent_config.id,
            prompt=prompt,
            origin_json=origin,
            reply_json=reply,
            workspace_id=resolved_workspace,
            task_id=handoff.task_id,
            message_id=f"discord-handoff:{message_id}",
        )
        submit_error = submit_result.get("error")
        if submit_error:
            return f"Agent error: coordinate submit failed: {submit_error}", True

        job_data = submit_result.get("result", {}).get("job")
        job_id = job_data.get("id") if job_data else None
        if not job_id:
            return "Agent error: coordinate submit did not create a job", True

        completed = await self._run_with_heartbeat(
            placeholder,
            self._coordinate_client.wait_for_job_result(
                job_id=job_id,
                workspace_id=resolved_workspace,
                timeout=self.agent_config.timeout,
            ),
        )
        if completed is None:
            return "Agent timed out (no response from agentd).", True

        display_text = self._extract_completed_display_text(completed)
        is_error = completed.get("status") != "done" or self._is_error_response(display_text)
        return display_text, is_error

    async def _send_missing_report_fallback(
        self,
        channel,
        handoff: CoordinatorHandoff,
        *,
        response_text: str,
        is_error: bool,
    ) -> None:
        """Emit a structured report if the adapter forgot to include one."""
        from . import client as client_facade

        if self._agentd_mode:
            return
        if client_facade.contains_execution_agent_report(response_text):
            return
        if is_error:
            report = client_facade.build_agent_report(
                "blocker",
                handoff,
                reason="adapter returned an error without a structured agent report",
            )
        else:
            report = client_facade.build_agent_report(
                "progress",
                handoff,
                summary=(
                    "adapter completed without a structured agent-report; "
                    "operator should inspect the visible response"
                ),
            )
        try:
            await channel.send(
                report,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("Failed to send missing agent-report fallback", exc_info=True)

    async def _try_coordinator_lifecycle(self, message: discord.Message) -> bool:
        """Archive local task-scoped sessions after coordinator closeout/done notices."""
        from . import client as client_facade

        cfg = self.agent_config
        event = client_facade.parse_coordinator_lifecycle(
            message.content,
            my_discord_user_id=self.user.id,
        )
        if event is None:
            return False

        if self.session_store is None:
            # agentd_mode bridge has no local session store; skip archive.
            log.info(
                "Lifecycle event handled (no local session store): task=%s agent=%s action=%s",
                event.task_id, cfg.id, event.action,
            )
            return True

        archived = self.session_store.mark_task_archived(
            workspace_id=event.workspace_id,
            task_id=event.task_id,
            agent_id=cfg.id,
        )
        handoff = client_facade.CoordinatorHandoff(
            workspace_id=event.workspace_id,
            task_id=event.task_id,
            bootstrap_path="",
            action=event.action,
        )
        await message.channel.send(
            client_facade.build_agent_report(
                "progress",
                handoff,
                summary=(
                    f"archived {archived} task session(s) for {cfg.id} "
                    f"after {event.action}"
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info(
            "Archived %s task session(s): task=%s agent=%s action=%s",
            archived, event.task_id, cfg.id, event.action,
        )
        return True
