"""Private transport tests: only temporary owner-only sockets and fake credentials."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from multinexus.desktop.bindings import DesktopBinding


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="mn-host-", dir=Path(tempfile.gettempdir()).resolve()) as directory:
        yield Path(directory)


@pytest.fixture
def binding():
    return DesktopBinding('receiver', 'dsh', 'fixture-agent', 'session-11111111-1111-4111-8111-111111111111', '/fixture/workspace', 'r1')


def test_methods_use_fixed_versioned_authenticated_flat_protocol(tmp_path, binding):
    from multinexus.desktop.host_transport import DSHHostTransport

    async def exercise():
        tmp_path.chmod(0o700)
        token = tmp_path / 'worker.token'
        token.write_text('fixture-secret'); token.chmod(0o600)
        socket = tmp_path / 'host.sock'
        seen = []
        async def serve(reader, writer):
            seen.append(json.loads(await reader.readline()))
            writer.write(b'{"version":1,"ok":true,"result":{"state":"queued"}}\n')
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_unix_server(serve, path=str(socket))
        socket.chmod(0o600)
        scope = dict(job_id='j', attempt_token='a', lease_id='l', native_request_id='r', binding_digest=binding.digest, payload_digest='a' * 64, native_content_digest='b' * 64, recipient='receiver')
        client = DSHHostTransport(socket, token)
        try:
            await client.inspect(binding)
            await client.register_scope(scope)
            await client.queue(scope, 'private body')
            await client.query(scope)
            await client.close_scope(scope)
        finally:
            server.close(); await server.wait_closed()
        assert [r['op'] for r in seen] == ['inspect', 'register', 'queue', 'query', 'close']
        assert all(r['version'] == 1 and r['worker_token'] == 'fixture-secret' for r in seen)
        assert seen[0]['binding'] == binding.as_dict()
        assert seen[2]['scope'] == scope
        assert seen[2]['confirmed_latch'] == scope
        assert seen[2]['content'] == 'private body'
    asyncio.run(exercise())


def test_unsafe_files_and_codex_binding_rejected_before_connect(tmp_path, binding):
    from multinexus.desktop.host_transport import DSHHostTransport, DSHHostError

    async def exercise():
        tmp_path.chmod(0o700)
        token = tmp_path / 'token'; token.write_text('secret'); token.chmod(0o644)
        client = DSHHostTransport(tmp_path / 'missing.sock', token)
        with pytest.raises(DSHHostError, match='Desktop host unavailable'):
            await client.inspect(binding)
        token.chmod(0o600)
        link = tmp_path / 'link'; link.symlink_to(token)
        with pytest.raises(DSHHostError):
            await DSHHostTransport(tmp_path / 'missing.sock', link).inspect(binding)
        with pytest.raises(DSHHostError):
            await client.inspect(DesktopBinding('receiver', 'codex', 'a', '11111111-1111-4111-8111-111111111111', '/fixture', '1'))
    asyncio.run(exercise())


@pytest.mark.parametrize('response', [b'{"version":1,"ok":false,"error":"private provider body"}\n', b'{"version":2,"ok":true,"result":{}}\n', b'not json\n', b'x' * 70000 + b'\n', b'{"version":1,"ok":true,"result":{}}', b'[' * 30000 + b'0' + b']' * 30000 + b'\n'], ids=['remote-error', 'version', 'json', 'oversize', 'truncated', 'depth'])
def test_remote_errors_malformed_oversize_and_truncated_frames_are_static(tmp_path, binding, response):
    from multinexus.desktop.host_transport import DSHHostTransport, DSHHostError

    async def exercise():
        tmp_path.chmod(0o700)
        token = tmp_path / 'token'; token.write_text('secret'); token.chmod(0o600)
        path = tmp_path / 'host.sock'
        async def serve(reader, writer):
            await reader.readline(); writer.write(response); await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_unix_server(serve, path=str(path)); path.chmod(0o600)
        try:
            with pytest.raises(DSHHostError) as error:
                await DSHHostTransport(path, token).inspect(binding)
            assert str(error.value) == 'Desktop host unavailable.'
        finally:
            server.close(); await server.wait_closed()
    asyncio.run(exercise())


def test_python_client_to_real_node_host_uses_fake_sdk_and_cancels_only_socket(tmp_path, binding):
    """Catch divergence between the two actual wire implementations."""
    from multinexus.desktop.host_transport import DSHHostTransport
    import hashlib

    async def exercise():
        tmp_path.chmod(0o700)
        token = tmp_path / 'worker.token'; token.write_text('fixture-secret'); token.chmod(0o600)
        profile = tmp_path / 'profile.json'
        profile.write_text(json.dumps(dict(version=2, local_alias='receiver', sender_expectations={'receiver': {'client_id': 'private-dsh', 'agent_id': 'example-dsh-agent', 'discord_user_id': '100000000000000102'}}, workspace_id='w', channel_id='c', mcp_url='https://fixture.invalid/mcp', channel_profiles={'receiver': str(tmp_path / 'channel.json')}, lifecycle_profile=str(tmp_path / 'lifecycle.json'), bindings={'receiver': {k: v for k, v in binding.as_dict().items() if k != 'alias'}}))); profile.chmod(0o600)
        config = tmp_path / 'config.json'
        path = tmp_path / 'host.sock'
        config.write_text(json.dumps(dict(version=1, profile_file=str(profile), socket_path=str(path), worker_token_file=str(token)))); config.chmod(0o600)
        host_module = (Path(__file__).resolve().parents[1] / 'integrations/dsh-desktop-host/host.js').as_uri()
        script = '''
import { loadHostConfig, startHost } from %s;
const config = await loadHostConfig(process.argv[1]);
const binding = config.bindings.receiver;
let calls = 0;
const controller = {
  inspect: async (id, signal) => {
    if (id !== binding.session_id || !(signal instanceof AbortSignal)) throw Error('fixture API mismatch');
    return {meta:{id:binding.session_id,cwd:binding.cwd},inheritedEventCount:0,events:[]};
  },
  prompt: async (request, signal) => {
    if (Object.keys(request).sort().join(',') !== 'content,mode,requestId,sessionId' || request.mode !== 'queue' || !(signal instanceof AbortSignal)) throw Error('fixture API mismatch');
    calls++; return {accepted:true};
  },
  cancel: () => { throw Error('native cancel forbidden'); },
};
const host = await startHost({...config,controller});
console.log('ready');
process.stdin.once('data', async () => {await host.close();console.log(calls);});
''' % json.dumps(host_module)
        process = await asyncio.create_subprocess_exec('node', '--input-type=module', '-e', script, str(config), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            assert await asyncio.wait_for(process.stdout.readline(), 5) == b'ready\n'
            client = DSHHostTransport(path, token)
            assert (await client.inspect(binding))['ready'] is True
            content = 'fixture complete native envelope'
            scope = dict(job_id='j', attempt_token='a', lease_id='l', native_request_id='r', binding_digest=binding.digest, payload_digest=hashlib.sha256(b'source').hexdigest(), native_content_digest=hashlib.sha256(content.encode()).hexdigest(), recipient='receiver')
            assert (await client.register_scope(scope))['scope'] == scope
            assert (await client.queue(scope, content))['state'] == 'queued'
            assert (await client.queue(scope, content))['state'] == 'queued'
            assert (await client.query(scope))['state'] == 'queued'
            assert await client.close_scope(scope) == {'closed': True}
            stdout, stderr = await asyncio.wait_for(process.communicate(b'close\n'), 5)
            assert process.returncode == 0 and stderr == b''
            assert stdout == b'1\n'
        finally:
            if process.returncode is None:
                process.kill(); await process.wait()
    asyncio.run(exercise())
