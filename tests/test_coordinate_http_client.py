"""R2B focused tests: CoordinateHttpRuntimeClient wire contract, retry matrix,
authority uncertainty and factory/consumer construction.

Covers plan §6.4/6.5/6.6/6.7/6.8/6.9/6.10 with an external fake R2A server —
no Coordinate state machine is re-implemented here.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web

from multinexus.adapters.base import AdapterResult
from multinexus.agentd.coordinate_client import (
    _sanitize_request_id,
    CoordinateClaimAuthorityUncertainError,
    CoordinateHttpConfigError,
    CoordinateHttpConflictError,
    CoordinateHttpConnectionError,
    CoordinateHttpError,
    CoordinateHttpMalformedError,
    CoordinateHttpRuntimeClient,
    CoordinateHttpServerError,
    CoordinateHttpTerminalError,
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
    make_coordinate_runtime_client,
)
from multinexus.models import AgentConfig


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(code: str, status: int) -> tuple[int, dict]:
    return status, {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": "static message"},
    }


class FakeServer:
    """Minimal external R2A-shaped listener driven by a scripted handler."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.port = _free_port()
        self.requests: list[dict] = []
        self.handler = None
        self.runner = None

    async def _handle(self, request):
        body = await request.read()
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            payload = None
        self.requests.append(
            {
                "method": request.method,
                "path": request.raw_path,
                "headers": dict(request.headers),
                "body": payload,
            }
        )
        assert self.handler is not None
        return await self.handler(request, self.requests[-1])

    def start(self):
        async def _run():
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", self._handle)
            self.runner = web.AppRunner(app, access_log=None)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", self.port)
            await site.start()

        self.loop.run_until_complete(_run())

    def stop(self):
        if self.runner is not None:
            self.loop.run_until_complete(self.runner.cleanup())
            self.runner = None


class HttpWireTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_file = os.path.join(self.tmp.name, "token")
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("test-token-value\n")
        os.chmod(self.token_file, 0o600)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = FakeServer(self.loop)
        self.server.start()
        self.addCleanup(self.server.stop)

    def _client(self, **overrides):
        kwargs = {
            "base_url": f"http://127.0.0.1:{self.server.port}",
            "client_id": "test-agentd",
            "token_file": self.token_file,
        }
        kwargs.update(overrides)
        return CoordinateHttpRuntimeClient(**kwargs)

    def _script(self, *responses):
        """Return a handler yielding one response per call, last one repeated."""
        calls = {"n": 0}

        async def handler(request, record):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            status, envelope = responses[i]
            return web.json_response(envelope, status=status)

        self.server.handler = handler
        return calls


class WireShapeTests(HttpWireTestBase):
    """Plan §6.4/6.5: exact routes, headers, body and success shapes."""

    def test_runtime_contract_is_read_before_managed_claims(self):
        contract = {
            "contract_version": 1,
            "coordinate_version": "0.3.1",
            "transport": "http",
            "capabilities": {
                "claim_fencing": True,
                "agent_reconcile": True,
                "recoverable_claim": False,
                "managed_lease": True,
                "terminal_report": True,
            },
        }
        self._script((200, _ok(contract)))
        client = self._client()
        result = self.loop.run_until_complete(client.get_runtime_contract())
        self.assertEqual(result["coordinate_version"], "0.3.1")
        self.assertEqual(self.server.requests[0]["path"], "/v1/runtime/contract")
        self.assertEqual(self.server.requests[0]["method"], "GET")

    def test_claim_sends_exact_route_headers_and_returns_inner_data(self):
        expected = {
            "claimed": False,
            "job": None,
            "attempt_token": None,
            "execution_context": None,
            "reason": "queue_empty",
        }
        self._script((200, _ok(expected)))
        client = self._client()
        result = self.loop.run_until_complete(
            client.claim_job(agent_id="test-agentd")
        )
        self.assertEqual(result, expected)
        record = self.server.requests[0]
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["path"], "/v1/jobs/claim")
        self.assertEqual(
            record["headers"].get("X-Coordinate-Client-ID"), "test-agentd"
        )
        self.assertEqual(
            record["headers"].get("Authorization"), "Bearer test-token-value"
        )
        self.assertEqual(record["headers"].get("Content-Type"), "application/json")

    def test_submit_returns_result_shape_and_sends_stable_key(self):
        job = {"id": "request:abc", "status": "pending"}
        self._script((200, _ok(job)))
        client = self._client()
        result = self.loop.run_until_complete(
            client.submit_request(
                target_agent="mac-codex",
                prompt="hello",
                origin_json={"platform": "discord", "destination": "ch"},
                reply_json={"platform": "discord", "destination": "ch"},
                workspace_id="demo",
                message_id="m-1",
            )
        )
        self.assertEqual(result, {"result": job})
        record = self.server.requests[0]
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["path"], "/v1/requests")
        self.assertEqual(record["body"]["workspace_id"], "demo")
        self.assertEqual(record["body"]["target_agent"], "mac-codex")
        self.assertEqual(record["body"]["idempotency_key"], "m-1")

    def test_submit_generates_key_when_caller_supplies_none(self):
        self._script((200, _ok({"job": {"id": "x"}})))
        client = self._client()
        self.loop.run_until_complete(
            client.submit_request(
                target_agent="a",
                prompt="p",
                origin_json={"platform": "discord", "destination": "c"},
                reply_json={"platform": "discord", "destination": "c"},
                workspace_id="demo",
            )
        )
        key = self.server.requests[0]["body"]["idempotency_key"]
        self.assertIsInstance(key, str)
        self.assertTrue(key)

    def test_report_progress_renew_return_result_shape(self):
        report_data = {"job": {"status": "done"}}
        progress_data = {"job": {"status": "running"}, "event": {"id": "e1"}}
        renew_data = {"status": "active", "expires_at": "x", "server_now": "y"}
        self._script(
            (200, _ok(report_data)),
            (200, _ok(progress_data)),
            (200, _ok(renew_data)),
        )
        client = self._client()
        reported = self.loop.run_until_complete(
            client.report_job(
                job_id="job-1",
                agent_id="test-agentd",
                status="done",
                result_json={"response_text": "ok"},
                attempt_token=2,
                lease_id="lease-1",
            )
        )
        progressed = self.loop.run_until_complete(
            client.record_progress(
                job_id="job-1",
                agent_id="test-agentd",
                stage="working",
                attempt_token=2,
                lease_id="lease-1",
            )
        )
        renewed = self.loop.run_until_complete(
            client.renew_lease(
                job_id="job-1",
                agent_id="test-agentd",
                attempt_token=2,
                lease_id="lease-1",
            )
        )
        self.assertEqual(reported, {"result": report_data})
        self.assertEqual(progressed, {"result": progress_data})
        self.assertEqual(renewed, {"result": renew_data})
        self.assertEqual(self.server.requests[0]["path"], "/v1/jobs/job-1/report")
        self.assertEqual(self.server.requests[0]["body"]["attempt_token"], 2)
        self.assertEqual(self.server.requests[0]["body"]["lease_id"], "lease-1")
        self.assertEqual(
            self.server.requests[1]["path"], "/v1/jobs/job-1/progress"
        )
        self.assertEqual(
            self.server.requests[2]["path"], "/v1/jobs/job-1/lease/renew"
        )
        self.assertEqual(self.server.requests[2]["body"]["lease_id"], "lease-1")
        self.assertEqual(self.server.requests[2]["body"]["attempt_token"], 2)

    def test_resolve_channel_workspace_shapes(self):
        self._script(
            (
                200,
                _ok(
                    {
                        "bound": True,
                        "binding": {
                            "platform": "discord",
                            "channel_id": "ch-1",
                            "workspace_id": "demo",
                        },
                    }
                ),
            ),
            (200, _ok({"bound": False, "binding": None})),
        )
        client = self._client()
        bound = self.loop.run_until_complete(
            client.resolve_channel_workspace(platform="Discord", channel_id="ch-1")
        )
        unbound = self.loop.run_until_complete(
            client.resolve_channel_workspace(platform="discord", channel_id="none")
        )
        self.assertEqual(bound, "demo")
        self.assertIsNone(unbound)
        self.assertEqual(
            self.server.requests[0]["path"], "/v1/channel-bindings/discord/ch-1"
        )

    def test_job_get_and_404_none(self):
        job = {"id": "job-1", "status": "pending"}
        self._script((200, _ok(job)), _err("not_found", 404))
        client = self._client()
        got = self.loop.run_until_complete(
            client._get_job("job-1", workspace_id="demo")
        )
        missing = self.loop.run_until_complete(
            client._get_job("job-2", workspace_id="demo")
        )
        self.assertEqual(got, job)
        self.assertIsNone(missing)
        self.assertEqual(
            self.server.requests[0]["path"], "/v1/workspaces/demo/jobs/job-1"
        )

    def test_wait_for_job_result_polls_until_terminal(self):
        pending = {"id": "job-1", "status": "pending"}
        done = {"id": "job-1", "status": "done", "result": {"text": "x"}}
        self._script((200, _ok(pending)), (200, _ok(done)))
        client = self._client()
        result = self.loop.run_until_complete(
            client.wait_for_job_result(
                job_id="job-1", workspace_id="demo", poll_interval=0.01
            )
        )
        self.assertEqual(result, done)
        self.assertEqual(len(self.server.requests), 2)

    def test_channel_path_is_quoted(self):
        self._script((200, _ok({"bound": False, "binding": None})))
        client = self._client()
        self.loop.run_until_complete(
            client.resolve_channel_workspace(platform="discord", channel_id="a/b?c")
        )
        self.assertEqual(
            self.server.requests[0]["path"], "/v1/channel-bindings/discord/a%2Fb%3Fc"
        )

    def test_malformed_envelope_and_content_type_are_bounded(self):
        async def handler(request, record):
            return web.Response(text="not json", content_type="text/plain")

        self.server.handler = handler
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )

    def test_oversize_body_is_bounded(self):
        async def handler(request, record):
            resp = web.StreamResponse()
            resp.content_type = "application/json"
            await resp.prepare(request)
            await resp.write(b'{"ok":' + b" " * (1024 * 1024 + 16) + b"}")
            return resp

        self.server.handler = handler
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )

    def test_status_envelope_mismatch_is_bounded(self):
        # 200 with ok=False is a malformed success, never a silent success.
        self._script(
            (200, {"ok": False, "data": None, "error": {"code": "internal", "message": "x"}})
        )
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )

    def test_repr_and_errors_never_contain_token(self):
        self._script(*_err("internal", 500))
        client = self._client()
        self.assertNotIn("test-token-value", repr(client))
        try:
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
            self.fail("expected error")
        except CoordinateHttpError as exc:
            text = str(exc)
            self.assertNotIn("test-token-value", text)
            self.assertNotIn(self.token_file, text)
            self.assertIn("coordinate HTTP", text)

    def test_trailing_slash_base_url_is_canonical_single_slash_route(self):
        expected = {"claimed": False, "job": None, "reason": "queue_empty"}
        self._script((200, _ok(expected)))
        client = self._client(base_url=f"http://127.0.0.1:{self.server.port}/")
        self.assertEqual(client.base_url, f"http://127.0.0.1:{self.server.port}")
        self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertEqual(
            self.server.requests[0]["path"], "/v1/jobs/claim"
        )

    def test_redirect_is_not_followed_other_listener_gets_nothing(self):
        server_b = FakeServer(self.loop)

        async def b_handler(request, record):
            return web.json_response(_ok({"job": {"status": "done"}}))

        server_b.handler = b_handler
        server_b.start()
        self.addCleanup(server_b.stop)

        location = f"http://127.0.0.1:{server_b.port}/v1/jobs/evil"

        async def handler(request, record):
            return web.json_response(
                {
                    "ok": False,
                    "data": None,
                    "error": {"code": "redirect", "message": "x"},
                },
                status=302,
                headers={"Location": location},
            )

        self.server.handler = handler
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
        # Malformed 3xx is bounded-retried, never followed on any attempt.
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(
            len(server_b.requests), 0, "redirect must not be followed"
        )

    def test_success_envelope_requires_object_data(self):
        # 200 + ok=True + error=None with non-object data is malformed.
        self._script((200, {"ok": True, "data": "not-an-object", "error": None}))
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )

    def test_error_status_with_ok_true_is_malformed_not_terminal(self):
        # Non-200 with ok=True is not an authoritative rejection.
        self._script((400, {"ok": True, "data": {}, "error": None}))
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
        self.assertEqual(
            len(self.server.requests), 3, "malformed is retried, not terminal"
        )

    def test_error_envelope_missing_message_is_malformed(self):
        self._script((400, {"ok": False, "data": None, "error": {"code": "bad"}}))
        client = self._client()
        with self.assertRaises(CoordinateHttpMalformedError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )

    def test_request_id_illegal_or_overlong_is_sanitized(self):
        for raw in ("bad id!<>|", "a" * 65):
            async def handler(request, record, raw=raw):
                resp = web.json_response(
                    _err("internal", 500)[1], status=500
                )
                resp.headers["X-Coordinate-Request-Id"] = raw
                return resp

            self.server.handler = handler
            client = self._client()
            try:
                self.loop.run_until_complete(
                    client.report_job(
                        job_id="j", agent_id="a", status="done", result_json={}
                    )
                )
                self.fail("expected error")
            except CoordinateHttpError as exc:
                self.assertEqual(exc.request_id, "")
                self.assertNotIn(raw[:8], str(exc))

    def test_request_id_allowlisted_value_survives(self):
        async def handler(request, record):
            resp = web.json_response(_err("internal", 500)[1], status=500)
            resp.headers["X-Coordinate-Request-Id"] = "ok-1_AB"
            return resp

        self.server.handler = handler
        client = self._client()
        try:
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
            self.fail("expected error")
        except CoordinateHttpError as exc:
            self.assertEqual(exc.request_id, "ok-1_AB")
            self.assertIn("ok-1_AB", str(exc))

    def test_request_id_trailing_newline_is_rejected(self):
        # ``re.fullmatch`` is intentional: ``$`` alone can match before a
        # final newline, which must never reach an exception or log.
        self.assertEqual(_sanitize_request_id("looks-safe\n"), "")


class RetryMatrixTests(HttpWireTestBase):
    """Plan §6.7: bounded retry with identical body/key; 4xx/409 never retried."""

    def test_read_retries_503_then_succeeds(self):
        calls = self._script(
            _err("unavailable", 503),
            (
                200,
                _ok(
                    {
                        "bound": True,
                        "binding": {
                            "platform": "discord",
                            "channel_id": "c",
                            "workspace_id": "demo",
                        },
                    }
                ),
            ),
        )
        client = self._client()
        data = self.loop.run_until_complete(
            client.resolve_channel_workspace(platform="discord", channel_id="c")
        )
        self.assertEqual(data, "demo")
        self.assertEqual(len(self.server.requests), 2)

    def test_submit_retries_with_identical_body_and_key(self):
        calls = self._script(
            _err("unavailable", 503), (200, _ok({"job": {"id": "x"}}))
        )
        client = self._client()
        self.loop.run_until_complete(
            client.submit_request(
                target_agent="a",
                prompt="p",
                origin_json={"platform": "discord", "destination": "c"},
                reply_json={"platform": "discord", "destination": "c"},
                workspace_id="demo",
                idempotency_key="stable-key",
            )
        )
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(
            self.server.requests[0]["body"], self.server.requests[1]["body"]
        )
        self.assertEqual(
            self.server.requests[1]["body"]["idempotency_key"], "stable-key"
        )

    def test_4xx_is_terminal_no_retry(self):
        calls = self._script(_err("unauthorized", 401))
        client = self._client()
        with self.assertRaises(CoordinateHttpTerminalError):
            self.loop.run_until_complete(
                client.resolve_channel_workspace(platform="discord", channel_id="c")
            )
        self.assertEqual(len(self.server.requests), 1)

    def test_report_409_conflict_no_retry_and_not_success(self):
        calls = self._script(_err("conflict", 409))
        client = self._client()
        with self.assertRaises(CoordinateHttpConflictError):
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
        self.assertEqual(len(self.server.requests), 1)

    def test_500_with_conflict_code_is_server_error_not_conflict(self):
        # Conflict is decided solely by HTTP 409; a body-level code=conflict
        # on a 500 stays an ordinary retryable server error.
        calls = self._script(_err("conflict", 500))
        client = self._client()
        with self.assertRaises(CoordinateHttpServerError) as ctx:
            self.loop.run_until_complete(
                client.report_job(
                    job_id="j", agent_id="a", status="done", result_json={}
                )
            )
        self.assertNotIsInstance(
            ctx.exception, CoordinateHttpConflictError
        )
        self.assertEqual(
            len(self.server.requests), 3, "5xx stays bounded-retryable"
        )

    def test_progress_retries_with_identical_body(self):
        calls = self._script(
            _err("unavailable", 503),
            (200, _ok({"job": {"status": "running"}})),
        )
        client = self._client()
        self.loop.run_until_complete(
            client.record_progress(
                job_id="j", agent_id="a", stage="s", attempt_token=1, lease_id="l"
            )
        )
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(
            self.server.requests[0]["body"], self.server.requests[1]["body"]
        )

    def test_renew_is_limited_to_two_attempts_and_stays_in_budget(self):
        calls = self._script(_err("unavailable", 503))
        client = self._client()
        start = time.monotonic()
        with self.assertRaises(CoordinateHttpServerError):
            self.loop.run_until_complete(
                client.renew_lease(
                    job_id="j", agent_id="a", attempt_token=1, lease_id="l"
                )
            )
        elapsed = time.monotonic() - start
        self.assertEqual(len(self.server.requests), 2)
        self.assertLess(elapsed, 4.0, "renew budget must stay below 5s margin")

    def test_exhausted_retries_raise_bounded_error(self):
        calls = self._script(
            _err("unavailable", 503),
            _err("unavailable", 503),
            _err("unavailable", 503),
        )
        client = self._client()
        with self.assertRaises(CoordinateHttpServerError):
            self.loop.run_until_complete(
                client.resolve_channel_workspace(platform="discord", channel_id="c")
            )
        self.assertEqual(len(self.server.requests), 3)


class ClaimAuthorityTests(HttpWireTestBase):
    """Plan §6.8/6.9: claim never retries; uncertainty latches; recoverable and
    reap fail closed over HTTP."""

    def test_claim_5xx_is_authority_uncertain_single_attempt(self):
        calls = self._script(_err("internal", 500))
        client = self._client()
        with self.assertRaises(CoordinateClaimAuthorityUncertainError):
            self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertEqual(len(self.server.requests), 1)

    def test_claim_timeout_is_authority_uncertain_single_attempt(self):
        async def handler(request, record):
            await asyncio.sleep(5)

        self.server.handler = handler
        client = self._client()
        with patch(
            "multinexus.agentd.coordinate_client._CLAIM_TIMEOUT_SECONDS", 0.1
        ):
            with self.assertRaises(CoordinateClaimAuthorityUncertainError):
                self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertEqual(len(self.server.requests), 1)

    def test_claim_malformed_is_authority_uncertain(self):
        async def handler(request, record):
            return web.Response(text="<html>", content_type="text/html")

        self.server.handler = handler
        client = self._client()
        with self.assertRaises(CoordinateClaimAuthorityUncertainError):
            self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertEqual(len(self.server.requests), 1)

    def test_claim_401_is_terminal_not_latch(self):
        calls = self._script(_err("unauthorized", 401))
        client = self._client()
        with self.assertRaises(CoordinateHttpTerminalError) as ctx:
            self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertNotIsInstance(
            ctx.exception, CoordinateClaimAuthorityUncertainError
        )
        self.assertEqual(len(self.server.requests), 1)

    def test_claim_pre_send_config_errors_do_not_send(self):
        client = self._client()
        with self.assertRaises(CoordinateHttpConfigError):
            self.loop.run_until_complete(
                client.claim_job(agent_id="a", recoverable=True)
            )
        with self.assertRaises(CoordinateHttpConfigError):
            self.loop.run_until_complete(
                client.claim_job(
                    agent_id="a", recovery_reason="x"
                )
            )
        with self.assertRaises(CoordinateRuntimeError):
            self.loop.run_until_complete(
                client.claim_job(agent_id="a", reap_mode="bogus", reap_reason="x")
            )
        self.assertEqual(len(self.server.requests), 0)

    def test_claim_connection_refused_is_not_authority_uncertain(self):
        # A closed loopback port: the request is never sent, so claim keeps
        # the ordinary bounded poll (e.g. tunnel not ready yet) instead of
        # latching the worker.
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        client = self._client(base_url=f"http://127.0.0.1:{dead_port}")
        with self.assertRaises(CoordinateHttpConnectionError) as ctx:
            self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertNotIsInstance(
            ctx.exception, CoordinateClaimAuthorityUncertainError
        )
        self.assertEqual(len(self.server.requests), 0)

    def test_claim_server_disconnect_is_authority_uncertain_single_attempt(self):
        # The request was sent and the server dropped the connection: the
        # server may have acted on it, so this latches after exactly one
        # attempt.
        async def handler(request, record):
            request.transport.abort()
            return web.Response()

        self.server.handler = handler
        client = self._client()
        with self.assertRaises(CoordinateClaimAuthorityUncertainError):
            self.loop.run_until_complete(client.claim_job(agent_id="a"))
        self.assertEqual(len(self.server.requests), 1)

    def test_claim_none_reap_sends_reap_fields(self):
        claimed = {"claimed": False, "job": None, "reason": "queue_empty"}
        self._script((200, _ok(claimed)))
        client = self._client()
        self.loop.run_until_complete(
            client.claim_job(
                agent_id="a", reap_mode="none", reap_reason="p9-3c1-test"
            )
        )
        body = self.server.requests[0]["body"]
        self.assertEqual(body["reap_mode"], "none")
        self.assertEqual(body["reap_reason"], "p9-3c1-test")

    def test_reap_leases_fail_closed(self):
        client = self._client()
        with self.assertRaises(CoordinateHttpConfigError) as ctx:
            self.loop.run_until_complete(client.reap_leases())
        self.assertIn("CLI", str(ctx.exception))
        self.assertEqual(len(self.server.requests), 0)


class WorkerClaimLatchTests(HttpWireTestBase):
    """Plan §6.8/6.10: AgentdWorker runs through the factory; a claim with
    authority-uncertain outcome performs one agent-scoped reconcile and then
    latches if authority cannot be proven; terminal errors latch immediately."""

    def _make_worker(self, http_client):
        from multinexus.agentd.worker import AgentdWorker

        cfg = AgentConfig(
            id="test-agent",
            token="fake-token",
            adapter="claude",
            context_db_path=os.path.join(self.tmp.name, "ctx.sqlite3"),
            agentd_mode=True,
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/db.sqlite3",
            coordinate_transport="http",
            coordinate_http_base_url=f"http://127.0.0.1:{self.server.port}",
            coordinate_http_client_id="test-agentd",
            coordinate_http_token_file=self.token_file,
        )
        with patch("multinexus.agentd.worker.make_adapter") as mock_factory:
            adapter = MagicMock()
            adapter.startup_check = AsyncMock(return_value=None)
            adapter.call = AsyncMock(
                return_value=AdapterResult(text="ok", session_id=None)
            )
            mock_factory.return_value = adapter
            with patch(
                "multinexus.agentd.worker.make_coordinate_runtime_client",
                return_value=http_client,
            ):
                worker = AgentdWorker(cfg)
        return worker, adapter

    def test_claim_uncertain_latches_until_stop_no_adapter_no_reclaim(self):
        async def handler(request, record):
            return web.json_response(
                {"ok": False, "data": None, "error": {"code": "internal", "message": "x"}},
                status=500,
            )

        self.server.handler = handler
        client = self._client()
        worker, adapter = self._make_worker(client)

        async def scenario():
            task = asyncio.create_task(worker.run(poll_interval=0.01))
            for _ in range(500):
                if self.server.requests:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.server.requests, "claim was never sent")
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.server.requests), 2)
            self.assertEqual(self.server.requests[0]["path"], "/v1/jobs/claim")
            self.assertEqual(
                self.server.requests[1]["path"],
                "/v1/agents/test-agent/reconcile",
            )
            adapter.call.assert_not_called()
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        self.loop.run_until_complete(scenario())

    def test_claim_terminal_error_latches_without_polling(self):
        async def handler(request, record):
            return web.json_response(
                {"ok": False, "data": None, "error": {"code": "unauthorized", "message": "x"}},
                status=401,
            )

        self.server.handler = handler
        client = self._client()
        worker, adapter = self._make_worker(client)

        async def scenario():
            task = asyncio.create_task(worker.run(poll_interval=0.01))
            for _ in range(500):
                if self.server.requests:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(self.server.requests), 1)
            self.assertEqual(self.server.requests[0]["path"], "/v1/jobs/claim")
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.server.requests), 1)
            adapter.call.assert_not_called()
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        self.loop.run_until_complete(scenario())

    def test_worker_constructed_through_factory_only(self):
        from multinexus.agentd.worker import AgentdWorker

        cfg = AgentConfig(
            id="test-agent",
            token="fake-token",
            adapter="claude",
            context_db_path=os.path.join(self.tmp.name, "ctx.sqlite3"),
            agentd_mode=True,
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/db.sqlite3",
        )
        with patch("multinexus.agentd.worker.make_adapter") as mock_factory:
            mock_factory.return_value = MagicMock()
            with patch(
                "multinexus.agentd.worker.make_coordinate_runtime_client"
            ) as mock_make:
                mock_make.return_value = MagicMock()
                AgentdWorker(cfg)
        mock_make.assert_called_once_with(cfg)


class ConsumerConstructionTests(unittest.TestCase):
    """Plan §6.10: Discord and KOOK bridge agentd_mode construction only goes
    through the factory; HTTP profile no longer requires coordinator_cli_path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_file = os.path.join(self.tmp.name, "token")
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("secret-token\n")
        os.chmod(self.token_file, 0o600)

    def _config(self, **overrides):
        defaults = {
            "id": "test-agent",
            "token": "fake-token",
            "adapter": "claude",
            "context_db_path": os.path.join(self.tmp.name, "ctx.sqlite3"),
            "agentd_mode": True,
            "coordinator_cli_path": "/usr/bin/true",
            "coordinator_db_path": "/tmp/db.sqlite3",
            "coordinate_transport": "http",
            "coordinate_http_base_url": "http://127.0.0.1:8765",
            "coordinate_http_client_id": "discord-bridge",
            "coordinate_http_token_file": self.token_file,
        }
        defaults.update(overrides)
        return AgentConfig(**defaults)

    @patch("multinexus.client.make_adapter")
    def test_discord_agentd_mode_builds_http_client_without_cli_path(self, mock_make):
        from multinexus.client import DiscordClient

        cfg = self._config(coordinator_cli_path="")
        with patch(
            "multinexus.client.make_coordinate_runtime_client"
        ) as mock_make_client:
            mock_make_client.return_value = MagicMock()
            client = DiscordClient(cfg)
        mock_make_client.assert_called_once_with(cfg)
        self.assertIsNone(client.adapter)
        self.assertIsNone(client.session_store)
        mock_make.assert_not_called()

    def test_discord_agentd_mode_http_factory_integration(self):
        from multinexus.client import DiscordClient

        cfg = self._config(coordinator_cli_path="")
        with patch("multinexus.client.make_adapter") as mock_make:
            client = DiscordClient(cfg)
        self.assertIsInstance(
            client._coordinate_client, CoordinateHttpRuntimeClient
        )
        self.assertNotIn("secret-token", repr(client._coordinate_client))
        mock_make.assert_not_called()

    @patch("multinexus.client.make_adapter")
    def test_kook_agentd_mode_builds_http_client_without_cli_path(self, mock_make):
        from multinexus.kook.bot import KookBridge

        cfg = self._config(coordinator_cli_path="", kook_poll_channel_ids=[1])
        with patch(
            "multinexus.kook.bot.make_coordinate_runtime_client"
        ) as mock_make_client:
            mock_make_client.return_value = MagicMock()
            bridge = KookBridge(cfg)
        mock_make_client.assert_called_once_with(cfg)

    def test_discord_legacy_direct_config_ignores_coordinate_fields(self):
        from multinexus.client import DiscordClient

        cfg = self._config(
            agentd_mode=False,
            coordinate_transport="http",
            coordinate_http_base_url="",
        )
        with patch("multinexus.client.make_adapter") as mock_make:
            mock_make.return_value = MagicMock()
            client = DiscordClient(cfg)
        self.assertIsNotNone(client.adapter)
        self.assertIsNone(client._coordinate_client)


class FactoryTests(unittest.TestCase):
    """Plan §6.1/6.10: single factory, CLI default, HTTP profile, fail closed."""

    def _config(self, **overrides):
        defaults = {
            "id": "test-agent",
            "token": "fake-token",
            "adapter": "claude",
            "coordinator_cli_path": "/usr/local/bin/coord-local",
            "coordinator_db_path": "/tmp/db.sqlite3",
        }
        defaults.update(overrides)
        return AgentConfig(**defaults)

    def test_default_transport_builds_cli_client(self):
        client = make_coordinate_runtime_client(self._config())
        self.assertIsInstance(client, CoordinateRuntimeClient)

    def test_http_transport_builds_http_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, "token")
            with open(token_file, "w", encoding="utf-8") as f:
                f.write("secret\n")
            os.chmod(token_file, 0o600)
            cfg = self._config(
                coordinate_transport="http",
                coordinate_http_base_url="http://127.0.0.1:8765",
                coordinate_http_client_id="discord-bridge",
                coordinate_http_token_file=token_file,
            )
            client = make_coordinate_runtime_client(cfg)
        self.assertIsInstance(client, CoordinateHttpRuntimeClient)

    def test_cli_missing_path_fails_closed(self):
        with self.assertRaises(SystemExit) as ctx:
            make_coordinate_runtime_client(
                self._config(coordinator_cli_path="")
            )
        self.assertIn("coordinator_cli_path", str(ctx.exception))

    def test_unknown_transport_fails_closed(self):
        with self.assertRaises(SystemExit) as ctx:
            make_coordinate_runtime_client(
                self._config(coordinate_transport="grpc")
            )
        self.assertIn("'cli' or 'http'", str(ctx.exception))

    def test_http_missing_fields_fail_closed(self):
        with self.assertRaises(CoordinateHttpConfigError):
            make_coordinate_runtime_client(
                self._config(
                    coordinate_transport="http",
                    coordinate_http_base_url="http://127.0.0.1:8765",
                    coordinate_http_client_id="",
                    coordinate_http_token_file="",
                )
            )
        with self.assertRaises(CoordinateHttpConfigError):
            make_coordinate_runtime_client(
                self._config(
                    coordinate_transport="http",
                    coordinate_http_base_url="",
                    coordinate_http_client_id="c",
                    coordinate_http_token_file="",
                )
            )


if __name__ == "__main__":
    unittest.main()
