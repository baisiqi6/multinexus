import asyncio
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import acp
from acp import schema

from multinexus.adapters.acp import ACPAdapter, _AcpClient, _SessionSink
from multinexus.adapters.factory import make_adapter
from multinexus.config import _load_toml_agent
from multinexus.models import AgentConfig


def _config(**overrides):
    values = {
        "id": "mac-kimi",
        "token": "",
        "adapter": "acp",
        "acp_command": "kimi",
        "acp_args": ["acp"],
        "work_dir": ".",
        "timeout": 5,
    }
    values.update(overrides)
    return AgentConfig(**values)


def _init_response(protocol_version=1, *, resume=False, load_session=False, info=True):
    session_caps = schema.SessionCapabilities(
        resume=schema.SessionResumeCapabilities() if resume else None
    )
    caps = schema.AgentCapabilities(
        load_session=load_session, session_capabilities=session_caps
    )
    agent_info = schema.Implementation(name="fake-agent", version="1.2.3") if info else None
    return schema.InitializeResponse(
        protocol_version=protocol_version,
        agent_capabilities=caps,
        agent_info=agent_info,
        auth_methods=[],
    )


def _text_chunk(text):
    return schema.AgentMessageChunk(
        content=schema.TextContentBlock(type="text", text=text),
        session_update="agent_message_chunk",
    )


class _FakeConnection:
    """Stands in for the SDK ClientSideConnection at the MultiNexus boundary.

    Records calls and replays scripted session updates; it does not implement
    JSON-RPC. Tests assert on the external behaviour of the adapter only.
    """

    def __init__(
        self,
        init_response=None,
        updates=None,
        stop_reason="end_turn",
        session_id="sess-1",
        prompt_error=None,
        hang=False,
    ):
        self._init_response = init_response or _init_response()
        self._updates = list(updates or [])
        self._stop_reason = stop_reason
        self._session_id = session_id
        self._prompt_error = prompt_error
        self._hang = hang
        self.cancelled_session_ids = []
        self.prompt_calls = []
        self.new_session_calls = 0
        self.resume_session_calls = 0
        self.load_session_calls = 0
        self.closed = False
        self._client = None

    def set_client(self, client):
        self._client = client

    async def initialize(self, protocol_version, client_capabilities=None, **kw):
        return self._init_response

    async def new_session(self, cwd, **kw):
        self.new_session_calls += 1
        return schema.NewSessionResponse(session_id=self._session_id)

    async def resume_session(self, session_id, cwd, **kw):
        self.resume_session_calls += 1
        return schema.ResumeSessionResponse()

    async def load_session(self, cwd, session_id, **kw):
        self.load_session_calls += 1
        return schema.LoadSessionResponse()

    async def prompt(self, session_id, prompt, **kw):
        self.prompt_calls.append({"session_id": session_id, "prompt": prompt})
        if self._hang:
            await asyncio.Event().wait()
        if self._prompt_error is not None:
            raise self._prompt_error
        for update in self._updates:
            if self._client is not None:
                await self._client.session_update(session_id, update)
        return schema.PromptResponse(stop_reason=self._stop_reason)

    async def cancel(self, session_id, **kw):
        self.cancelled_session_ids.append(session_id)

    async def close(self):
        self.closed = True


class _FakeStdin:
    """Stands in for asyncio StreamWriter (proc.stdin); never actually used."""

    def write(self, data):
        self.data = data

    async def drain(self):
        return None

    def close(self):
        return None


class _FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process.

    Only the attributes the adapter touches are provided. ``stdin``/``stdout``
    are placeholders because connect_to_agent is patched at the boundary.
    """

    def __init__(self, returncode=0):
        self.stdin = _FakeStdin()
        self.stdout = object()
        self.stderr = object()
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


class _Harness:
    """Patches the subprocess + connect_to_agent + cleanup boundaries.

    Production code passes ``proc.stdin``/``proc.stdout`` straight to
    ``acp.connect_to_agent``; we patch that one call and the process spawn and
    the process-group terminator. No connect_read_pipe/StreamWriter patching
    is needed because the adapter no longer calls them.
    """

    def __init__(self, conn, proc=None, connect_error=None):
        self.conn = conn
        self.proc = proc or _FakeProcess()
        self.connect_error = connect_error
        self.cleanup_calls = []
        self.captured = {}
        self._patches = []

    def _wire(self, client, stdin, stdout):
        if self.connect_error is not None:
            raise self.connect_error
        self.conn.set_client(client)
        return self.conn

    def __enter__(self):
        async def fake_create(*args, **kwargs):
            self.captured["cmd"] = args
            self.captured["kwargs"] = kwargs
            return self.proc

        async def default_cleanup(proc):
            self.cleanup_calls.append(proc)
            proc.kill()
            await proc.wait()

        self._patches = [
            patch("multinexus.adapters.acp.asyncio.create_subprocess_exec", fake_create),
            patch("multinexus.adapters.acp.acp.connect_to_agent", self._wire),
            patch(
                "multinexus.adapters.acp.terminate_owned_process_group",
                new=default_cleanup,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False

    def assert_cleanup_exactly_once(self, test_case):
        test_case.assertEqual(
            self.cleanup_calls,
            [self.proc],
            "process-group cleanup must run exactly once",
        )


class ACPConfigTests(unittest.TestCase):
    def test_toml_reads_acp_command_and_args(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "agents.toml"
            path.write_text(
                '[[agents]]\n'
                'id = "mac-kimi"\n'
                'adapter = "acp"\n'
                'token = "x"\n'
                'acp_command = "kimi"\n'
                'acp_args = ["acp", "--verbose"]\n',
                encoding="utf-8",
            )
            config = _load_toml_agent(path, "mac-kimi")
        self.assertEqual(config.acp_command, "kimi")
        self.assertEqual(config.acp_args, ["acp", "--verbose"])

    def test_toml_acp_defaults_empty(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "agents.toml"
            path.write_text(
                '[[agents]]\nid = "a"\nadapter = "acp"\ntoken = "x"\n',
                encoding="utf-8",
            )
            config = _load_toml_agent(path, "a")
        self.assertEqual(config.acp_command, "")
        self.assertEqual(config.acp_args, [])


class ACPFactoryTests(unittest.TestCase):
    def test_factory_constructs_acp_adapter(self):
        adapter = make_adapter(_config())
        self.assertIsInstance(adapter, ACPAdapter)
        self.assertEqual(adapter.name, "acp")


class ACPCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_call_success_and_text_accumulation(self):
        conn = _FakeConnection(
            updates=[_text_chunk("Hello, "), _text_chunk("world")],
            stop_reason="end_turn",
            session_id="sess-abc",
        )
        with _Harness(conn) as h:
            result = await ACPAdapter(_config()).call("hello")
        self.assertEqual(result.text, "Hello, world")
        self.assertEqual(result.session_id, "sess-abc")
        self.assertFalse(result.resumed)
        self.assertEqual(result.metadata["adapter"], "acp")
        self.assertEqual(result.metadata["protocol_version"], 1)
        self.assertEqual(result.metadata["agent_name"], "fake-agent")
        self.assertEqual(result.metadata["agent_version"], "1.2.3")
        self.assertEqual(result.metadata["stop_reason"], "end_turn")
        self.assertEqual(conn.new_session_calls, 1)
        self.assertTrue(conn.closed)
        # spawned command is the configured generic command + args
        self.assertEqual(h.captured["cmd"], ("kimi", "acp"))
        # success path also cleans up the process group exactly once
        h.assert_cleanup_exactly_once(self)

    async def test_system_prompt_concatenation(self):
        conn = _FakeConnection(updates=[])
        with _Harness(conn):
            await ACPAdapter(_config(system_prompt="SYS")).call("hello")
        prompt_blocks = conn.prompt_calls[0]["prompt"]
        self.assertEqual(prompt_blocks[0].text, "SYS\n\nUSER: hello")

    async def test_safe_progress_and_thought_not_leaked(self):
        events = []
        thought_text = "SECRET-THOUGHT-XYZ"
        conn = _FakeConnection(
            updates=[
                schema.AgentThoughtChunk(
                    content=schema.TextContentBlock(type="text", text=thought_text),
                    session_update="agent_thought_chunk",
                ),
                _text_chunk("answer"),
                schema.ToolCallStart(
                    tool_call_id="t1", title="Read file", session_update="tool_call"
                ),
            ]
        )
        with _Harness(conn):
            result = await ACPAdapter(_config()).call("hi", on_progress=events.append)
        self.assertEqual(result.text, "answer")
        self.assertNotIn(thought_text, repr(events))
        self.assertNotIn(thought_text, result.text)
        self.assertNotIn(thought_text, repr(result.metadata))
        stages = {e["stage"] for e in events}
        self.assertIn("stream", stages)
        self.assertIn("tool", stages)

    async def test_permission_request_default_denied(self):
        events = []
        conn = _FakeConnection()
        with _Harness(conn):
            task = asyncio.create_task(
                ACPAdapter(_config()).call("hi", on_progress=events.append)
            )
            while not conn.prompt_calls:
                await asyncio.sleep(0.01)
            resp = await conn._client.request_permission(
                "sess-1", tool_call=None, options=[]
            )
            await task
        self.assertIsInstance(resp, schema.RequestPermissionResponse)
        self.assertIsInstance(resp.outcome, schema.DeniedOutcome)
        self.assertEqual(resp.outcome.outcome, "cancelled")
        self.assertIn("permission", {e["stage"] for e in events})

    async def test_protocol_version_mismatch_fails_closed_and_cleans_up(self):
        conn = _FakeConnection(init_response=_init_response(protocol_version=2))
        with _Harness(conn) as h:
            result = await ACPAdapter(_config()).call("hi")
        self.assertIn("protocol mismatch", result.text)
        self.assertEqual(conn.new_session_calls, 0)
        h.assert_cleanup_exactly_once(self)

    async def test_missing_command_fails_cleanly(self):
        result = await ACPAdapter(_config(acp_command="")).call("hi")
        self.assertIn("acp_command is not configured", result.text)

    async def test_client_capabilities_fail_closed(self):
        conn = _FakeConnection()
        captured = {}

        real_initialize = conn.initialize

        async def spy_initialize(protocol_version, client_capabilities=None, **kw):
            captured["caps"] = client_capabilities
            return await real_initialize(protocol_version, client_capabilities, **kw)

        conn.initialize = spy_initialize
        with _Harness(conn):
            await ACPAdapter(_config()).call("hi")
        caps = captured["caps"]
        self.assertIsNotNone(caps)
        # filesystem capability is not declared at all
        self.assertIsNone(caps.fs)
        self.assertFalse(caps.terminal)
        self.assertIsNotNone(caps.auth)
        self.assertFalse(caps.auth.terminal)
        # no other client capability surfaces
        self.assertIsNone(caps.session)
        self.assertIsNone(caps.plan)
        self.assertIsNone(caps.elicitation)


class ACPResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_prefers_formal_resume_and_cleans_up(self):
        conn = _FakeConnection(
            init_response=_init_response(resume=True, load_session=True),
            session_id="sess-prev",
        )
        with _Harness(conn) as h:
            result = await ACPAdapter(_config()).resume("sess-prev", "continue")
        self.assertTrue(result.resumed)
        self.assertEqual(conn.resume_session_calls, 1)
        self.assertEqual(conn.load_session_calls, 0)
        self.assertEqual(conn.new_session_calls, 0)
        h.assert_cleanup_exactly_once(self)

    async def test_resume_falls_back_to_legacy_load(self):
        conn = _FakeConnection(
            init_response=_init_response(resume=False, load_session=True),
            session_id="sess-prev",
        )
        with _Harness(conn):
            result = await ACPAdapter(_config()).resume("sess-prev", "continue")
        self.assertTrue(result.resumed)
        self.assertEqual(conn.load_session_calls, 1)
        self.assertEqual(conn.resume_session_calls, 0)

    async def test_resume_fails_closed_without_capability(self):
        conn = _FakeConnection(init_response=_init_response(resume=False, load_session=False))
        with _Harness(conn):
            result = await ACPAdapter(_config()).resume("sess-prev", "continue")
        self.assertFalse(result.resumed)
        self.assertIn("failed closed", result.text)
        self.assertEqual(conn.new_session_calls, 0)
        self.assertEqual(conn.prompt_calls, [])


class ACPLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_sends_cancel_and_cleans_up_once(self):
        conn = _FakeConnection(hang=True, session_id="sess-t")
        with _Harness(conn) as h:
            result = await ACPAdapter(_config(timeout=1)).call("hi")
        self.assertIn("timeout", result.text.lower())
        self.assertEqual(conn.cancelled_session_ids, ["sess-t"])
        h.assert_cleanup_exactly_once(self)
        self.assertTrue(h.proc.killed)
        self.assertTrue(conn.closed)

    async def test_caller_cancellation_cleans_up_once_and_reraises(self):
        conn = _FakeConnection(hang=True, session_id="sess-c")
        with _Harness(conn) as h:
            task = asyncio.create_task(ACPAdapter(_config(timeout=60)).call("hi"))
            while not conn.prompt_calls:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        h.assert_cleanup_exactly_once(self)
        self.assertTrue(h.proc.killed)
        self.assertTrue(conn.closed)

    async def test_sdk_error_returns_clean_message_and_cleans_up_once(self):
        conn = _FakeConnection(prompt_error=RuntimeError("boom-SENTINEL-exc"))
        with _Harness(conn) as h:
            result = await ACPAdapter(_config()).call("hi")
        # stable error category, no raw exception detail
        self.assertEqual(result.text, "ACP error: agent prompt failed")
        self.assertNotIn("boom-SENTINEL-exc", result.text)
        self.assertNotIn("boom-SENTINEL-exc", repr(result.metadata))
        h.assert_cleanup_exactly_once(self)

    async def test_connection_construction_failure_cleans_up_once(self):
        conn = _FakeConnection()
        with _Harness(conn, connect_error=TypeError("bad stream")) as h:
            result = await ACPAdapter(_config()).call("hi")
        self.assertEqual(result.text, "ACP error: agent prompt failed")
        self.assertNotIn("bad stream", result.text)
        h.assert_cleanup_exactly_once(self)


class ACPSecretSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_exception_not_in_result_text_or_metadata(self):
        sentinel = "RAW-EXC-SECRET-7f3a"
        conn = _FakeConnection(prompt_error=RuntimeError(f"failure {sentinel}"))
        with self.assertLogs("multinexus.adapters.acp", level="WARNING") as logs:
            with _Harness(conn):
                result = await ACPAdapter(_config()).call("hi")
        self.assertNotIn(sentinel, result.text)
        self.assertNotIn(sentinel, repr(result.metadata))
        self.assertNotIn(sentinel, "\n".join(logs.output))

    def test_health_check_does_not_echo_args(self):
        secret_arg = "--token=SUPER-SECRET-ARG-99"
        status = asyncio.run(
            ACPAdapter(_config(acp_args=[secret_arg, "acp"])).health_check()
        )
        self.assertNotIn(secret_arg, repr(status))
        self.assertNotIn("args", status)
        self.assertEqual(status["arg_count"], 2)


class ACPHealthCheckTests(unittest.TestCase):
    def test_health_check_command_present(self):
        with patch("shutil.which", return_value="/usr/bin/kimi"):
            status = asyncio.run(ACPAdapter(_config()).health_check())
        self.assertTrue(status["available"])
        self.assertEqual(status["command"], "kimi")
        self.assertEqual(status["protocol_version"], 1)

    def test_health_check_command_missing(self):
        with patch("shutil.which", return_value=None):
            status = asyncio.run(ACPAdapter(_config()).health_check())
        self.assertFalse(status["available"])
        self.assertIsNone(status["path"])

    def test_health_check_no_command(self):
        status = asyncio.run(ACPAdapter(_config(acp_command="")).health_check())
        self.assertFalse(status["available"])


class ACPRealSdkContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real SDK constructor against a real, harmless subprocess.

    Uses ``cat`` (no side effects, short-lived, easy to clean up) to obtain a
    genuine ``StreamWriter``/``StreamReader`` pair, then constructs a real
    ``acp.connect_to_agent`` connection. Proves the constructor accepts the
    raw subprocess streams without TypeError/RuntimeWarning. No agent prompt
    is ever sent; the connection is closed and the process group cleaned up.
    """

    async def test_real_connect_to_agent_accepts_subprocess_streams(self):
        proc = await asyncio.create_subprocess_exec(
            "cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.assertIsNotNone(proc.stdin)
        self.assertIsNotNone(proc.stdout)
        sink = _SessionSink(None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            conn = acp.connect_to_agent(_AcpClient(sink), proc.stdin, proc.stdout)
        runtime_warnings = [
            w for w in caught if issubclass(w.category, RuntimeWarning)
        ]
        self.assertEqual(runtime_warnings, [], f"RuntimeWarnings: {runtime_warnings}")
        self.assertIsNotNone(conn)
        # bounded close + process-group cleanup, no prompt sent
        await asyncio.wait_for(conn.close(), timeout=5.0)
        from multinexus.adapters.utils import terminate_owned_process_group

        await terminate_owned_process_group(proc)
        self.assertIsNotNone(proc.returncode)


if __name__ == "__main__":
    unittest.main()
