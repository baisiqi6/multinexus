import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, writeFile, chmod, lstat, mkdir, symlink, rm, readFile, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { realpathSync } from 'node:fs'
import net from 'node:net'

const sha = text => createHash('sha256').update(text).digest('hex')
const binding = { alias: 'receiver', kind: 'dsh', agent_id: 'agent-fixture', session_id: 'session-11111111-1111-4111-8111-111111111111', cwd: '/fixture/workspace', revision: 'r1' }
const digest = sha(JSON.stringify(binding, Object.keys(binding).sort()))
const text = 'fixture private body'
const scope = { job_id: 'job-1', attempt_token: 'attempt-1', lease_id: 'lease-1', native_request_id: 'rpc-fixture-1', binding_digest: digest, payload_digest: sha('source channel body'), native_content_digest: sha(text), recipient: 'receiver' }
const msg = { id: 'message-1', content: [{ type: 'text', text }], source: { kind: 'user', rpcId: scope.native_request_id } }
const event = (seq, type, data, extra = {}) => ({ seq, type, data, ...extra })
const inserted = () => event(0, 'agent/inbox/spliced', { target: 'next-turn', start: 0, inserted: [msg] })
async function setup(events = []) {
  const { DesktopHost } = await import('./host.js')
  const calls = []; const view = { meta: { id: binding.session_id, cwd: binding.cwd }, inheritedEventCount: 0, events }
  const controller = {
    inspect: async (id, signal) => { assert.equal(id, binding.session_id); assert.ok(signal instanceof AbortSignal); return view },
    prompt: async (request, signal) => { assert.ok(signal instanceof AbortSignal); calls.push(request); return { accepted: true } },
    cancel: () => assert.fail('never cancel a native turn'),
    follow: () => assert.fail('cold inspection must not follow'),
  }
  const host = new DesktopHost({ controller, bindings: { receiver: binding }, workerToken: 'fixture-worker-token' })
  const request = (op, fields = {}) => host.handle({ version: 1, op, worker_token: 'fixture-worker-token', ...fields })
  return { host, request, calls, view }
}

test('queue is fenced by exact active scope and latch, uses only real SDK queue arguments', async () => {
  const { request, calls } = await setup()
  await assert.rejects(request('queue', { scope, content: text, confirmed_latch: scope }))
  await request('register', { scope })
  await assert.rejects(request('queue', { scope, content: text }))
  await assert.rejects(request('queue', { scope, content: text, confirmed_latch: { ...scope, lease_id: 'wrong' } }))
  await assert.rejects(request('queue', { scope, content: 'changed', confirmed_latch: scope }))
  assert.deepEqual(await request('queue', { scope, content: text, confirmed_latch: scope }), { state: 'queued', native_request_id: scope.native_request_id })
  assert.deepEqual(calls, [{ requestId: scope.native_request_id, sessionId: binding.session_id, mode: 'queue', content: [{ type: 'text', text }] }])
  await request('queue', { scope, content: text, confirmed_latch: scope })
  assert.equal(calls.length, 1)
  await request('close', { scope })
  await assert.rejects(request('query', { scope }))
})

test('allowlist exact identity and auth fail closed without exposing bodies', async () => {
  const { host, request, calls, view } = await setup()
  for (const change of [{ recipient: 'unknown' }, { binding_digest: '0'.repeat(64) }]) await assert.rejects(request('register', { scope: { ...scope, ...change } }))
  await assert.rejects(host.handle({ version: 1, op: 'inspect', worker_token: 'wrong', binding }))
  await assert.rejects(request('inspect', { binding: { ...binding, cwd: '/wrong' } }))
  await assert.rejects(request('inspect', { binding: { ...binding, kind: 'codex' } }))
  assert.deepEqual(await request('inspect', { binding }), { ready: true, binding_digest: digest, session_id: binding.session_id, cwd: binding.cwd })
  for (const meta of [{ ...view.meta, id: 'other' }, { ...view.meta, cwd: '/other' }, { ...view.meta, parentSession: 'parent' }, { ...view.meta, origin: 'subagent' }]) {
    view.meta = meta; await assert.rejects(request('inspect', { binding }))
  }
  assert.equal(calls.length, 0)
})

test('durable inbox claim gap and duplicate native evidence never requeue', async () => {
  for (const events of [
    [inserted()],
    [inserted(), event(1, 'turn/start', { turn: 1 }), event(2, 'agent/inbox/spliced', { target: 'next-turn', start: 0, removedCount: 1, inserted: [] })],
    [inserted(), event(2, 'turn/start', { turn: 1 })],
    [inserted(), event(1, 'agent/inbox/spliced', { target: 'next-turn', start: 1, inserted: [{ ...msg, id: 'message-2' }] })],
  ]) {
    const { request, calls } = await setup(events)
    await request('register', { scope })
    const result = await request('queue', { scope, content: text, confirmed_latch: scope })
    assert.ok(['queued', 'unknown'].includes(result.state))
    assert.equal(calls.length, 0)
    assert.equal(JSON.stringify(result).includes(text), false)
  }
})

test('busy previous turn completion cannot complete own queued rpc, query returns no history', async () => {
  const { request, view, calls } = await setup([inserted(), event(1, 'turn/start', { turn: 1 }), event(2, 'step/start', { turn: 1, step: 1 }), event(3, 'assistant/message', { turn: 1, step: 1, message: { content: [{ type: 'text', text: 'unrelated secret' }] } }, { surfaceOp: 'append' }), event(4, 'step/end', { turn: 1, step: 1 }), event(5, 'turn/end', { turn: 1, reason: { kind: 'completed' } })])
  await request('register', { scope })
  assert.equal((await request('query', { scope })).state, 'queued')
  view.events.push(event(6, 'turn/start', { turn: 2 }), event(7, 'agent/inbox/spliced', { target: 'next-turn', start: 0, removedCount: 1, inserted: [] }), event(8, 'step/start', { turn: 2, step: 1 }), event(9, 'user/message', msg, { surfaceOp: 'append' }))
  assert.equal((await request('query', { scope })).state, 'admitted')
  view.events.push(event(10, 'assistant/message', { turn: 2, step: 1, message: { content: [{ type: 'text', text: 'private answer' }] } }, { surfaceOp: 'append' }), event(11, 'step/end', { turn: 2, step: 1 }), event(12, 'turn/end', { turn: 2, reason: { kind: 'completed' } }))
  const result = await request('query', { scope })
  assert.equal(result.state, 'native_turn_completed'); assert.equal(result.turn, 2)
  assert.equal(JSON.stringify(result).includes('private'), false)
  assert.equal(calls.length, 0)
})

test('private config and socket lifecycle reject unsafe paths and existing listeners', async t => {
  const { startHost, loadHostConfig } = await import('./host.js')
  const dir = await mkdtemp(realpathSync(tmpdir()) + '/mn-host-'); t.after(() => rm(dir, { recursive: true, force: true }))
  await chmod(dir, 0o700)
  const profileFile = dir + '/profile.json', tokenFile = dir + '/worker.token', configFile = dir + '/host.json', socketPath = dir + '/host.sock'
  await writeFile(profileFile, JSON.stringify({ version: 2, local_alias: 'receiver', sender_expectations: { receiver: { client_id: 'private-dsh', agent_id: 'example-dsh-agent', discord_user_id: '100000000000000102' } }, workspace_id: 'w', channel_id: 'c', mcp_url: 'https://fixture.invalid/mcp', channel_profiles: { receiver: dir + '/channel.json' }, lifecycle_profile: dir + '/lifecycle.json', bindings: { receiver: Object.fromEntries(Object.entries(binding).filter(([k]) => k !== 'alias')) } }), { mode: 0o600 })
  await writeFile(tokenFile, 'fixture-worker-token', { mode: 0o600 })
  await writeFile(configFile, JSON.stringify({ version: 1, profile_file: profileFile, worker_token_file: tokenFile, socket_path: socketPath }), { mode: 0o600 })
  const config = await loadHostConfig(configFile)
  const fixture = await setup()
  const listener = await startHost({ ...config, controller: { inspect: async () => fixture.view } })
  t.after(() => listener.close())
  assert.equal((await lstat(socketPath)).mode & 0o777, 0o600)
  await assert.rejects(startHost({ ...config, controller: {} }))
  await listener.close(); await assert.rejects(lstat(socketPath))
  await symlink(tokenFile, socketPath); await assert.rejects(startHost({ ...config, controller: {} })); await rm(socketPath)
  await chmod(tokenFile, 0o644); await assert.rejects(loadHostConfig(configFile)); await chmod(tokenFile, 0o600)
  await mkdir(dir + '/unsafe', { mode: 0o755 }); await assert.rejects(startHost({ ...config, socketPath: dir + '/unsafe/a.sock', controller: {} }))
})

test('real SDK assistant envelope supports only exact own turn with a final message', async () => {
  const { request } = await setup(ownCompletionEvents())
  await request('register', { scope })
  assert.deepEqual(await request('query', { scope }), { state: 'native_turn_completed', native_request_id: scope.native_request_id, turn: 3, admitted_seq: 4, end_seq: 7 })
})

test('prompt exception is unknown on retry and cannot duplicate native enqueue', async () => {
  const { host, request, calls } = await setup()
  host.controller.prompt = async request => { calls.push(request); throw new Error('private provider body') }
  await request('register', { scope })
  await assert.rejects(request('queue', { scope, content: text, confirmed_latch: scope }), error => error.message === 'Desktop host unavailable.')
  assert.equal((await request('queue', { scope, content: text, confirmed_latch: scope })).state, 'unknown')
  assert.equal(calls.length, 1)
  await request('close', { scope })
})

test('plugin default is disabled and has no controller or file activity', async () => {
  const { apply } = await import('./index.js')
  await apply(new Proxy({}, { get: () => assert.fail('disabled plugin must do nothing') }))
})

async function temporaryListener(t, controller, cleanupMayFail = false) {
  const { startHost } = await import('./host.js')
  const dir = await mkdtemp(realpathSync(tmpdir()) + '/mn-wire-'); await chmod(dir, 0o700)
  t.after(() => rm(dir, { recursive: true, force: true }))
  const socketPath = dir + '/host.sock'
  const host = await startHost({ controller, bindings: { receiver: binding }, workerToken: 'fixture-worker-token', socketPath })
  t.after(async () => {
    try { await host.close() } catch (error) { if (!cleanupMayFail || error.code !== 'socket_replaced') throw error }
  })
  return { host, socketPath }
}
function exchange(socketPath, bytes) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath); let response = ''
    socket.on('connect', () => socket.write(bytes)); socket.on('data', data => { response += data }); socket.on('end', () => resolve(response)); socket.on('error', reject)
  })
}

test('actual socket accepts only one bounded authenticated fixed operation and static errors', async t => {
  const { socketPath } = await temporaryListener(t, { inspect: async () => ({ meta: { id: binding.session_id, cwd: binding.cwd }, inheritedEventCount: 0, events: [] }) })
  const valid = JSON.stringify({ version: 1, op: 'inspect', worker_token: 'fixture-worker-token', binding }) + '\n'
  assert.equal(JSON.parse(await exchange(socketPath, valid)).result.ready, true)
  for (const bytes of ['{"private body":true}\n', valid.replace('fixture-worker-token', 'wrong'), valid.replace('inspect', 'shell'), valid.replace('"version":1', '"version":2')]) {
    assert.deepEqual(JSON.parse(await exchange(socketPath, bytes)), { version: 1, ok: false, error: 'Desktop host unavailable.' })
  }
  assert.equal(await exchange(socketPath, 'x'.repeat(65537)), '')
  assert.equal(await exchange(socketPath, valid + valid), '')
})

test('teardown never deletes a replacement inode or invokes controller.cancel', async t => {
  const { host, socketPath } = await temporaryListener(t, { cancel: () => assert.fail('must not cancel') })
  await rm(socketPath); await writeFile(socketPath, 'replacement fixture', { mode: 0o600 })
  const before = await lstat(socketPath)
  assert.equal((await host.close()).socket_replaced, true)
  assert.equal((await lstat(socketPath)).ino, before.ino)
  assert.equal(await readFile(socketPath, 'utf8'), 'replacement fixture')
})

test('ambiguous native history, different content, mixed input, and canceled claim stay unknown', async () => {
  for (const events of [
    [event(0, 'agent/inbox/spliced', { target: 'next-turn', start: 0, inserted: [{ ...msg, content: [{ type: 'text', text: 'different' }] }] })],
    [inserted(), event(1, 'turn/start', { turn: 1 }), event(2, 'agent/inbox/spliced', { target: 'next-turn', start: 0, removedCount: 1, inserted: [], outcome: 'canceled' })],
    [inserted(), event(1, 'turn/start', { turn: 1 }), event(2, 'step/start', { turn: 1, step: 1 }), event(3, 'user/message', msg, { surfaceOp: 'append' }), event(4, 'user/message', { ...msg, id: 'human', source: { kind: 'user', rpcId: 'other' } }, { surfaceOp: 'append' }), event(5, 'assistant/message', { turn: 1, step: 1, message: { content: [{ type: 'text', text: 'mixed answer' }] } }, { surfaceOp: 'append' }), event(6, 'step/end', { turn: 1, step: 1 }), event(7, 'turn/end', { turn: 1, reason: { kind: 'completed' } })],
  ]) {
    const { request, calls } = await setup(events); await request('register', { scope })
    assert.equal((await request('queue', { scope, content: text, confirmed_latch: scope })).state, 'unknown')
    assert.equal(calls.length, 0)
  }
})

test('disconnect and Host teardown only stop observation, preserving native prompt signal', async t => {
  let release, promptSignal, started
  const reachedPrompt = new Promise(resolve => { started = resolve })
  const { host, socketPath } = await temporaryListener(t, {
    inspect: async () => ({ meta: { id: binding.session_id, cwd: binding.cwd }, inheritedEventCount: 0, events: [] }),
    prompt: async (request, signal) => { promptSignal = signal; started(); return new Promise(resolve => { release = resolve }) },
    cancel: () => assert.fail('observer cancellation must not cancel native turn'),
  })
  await exchange(socketPath, JSON.stringify({ version: 1, op: 'register', worker_token: 'fixture-worker-token', scope }) + '\n')
  const socket = net.createConnection(socketPath)
  socket.on('error', () => {})
  await new Promise(resolve => socket.on('connect', resolve))
  socket.write(JSON.stringify({ version: 1, op: 'queue', worker_token: 'fixture-worker-token', scope, content: text, confirmed_latch: scope }) + '\n')
  await reachedPrompt
  socket.destroy(); await host.close()
  assert.equal(promptSignal.aborted, false)
  release({ accepted: true })
})

test('concurrent alias admission is rejected without a second native call', async () => {
  const { host, request, calls } = await setup()
  await request('register', { scope })
  let release, started
  const entered = new Promise(resolve => { started = resolve })
  host.controller.prompt = async nativeRequest => { calls.push(nativeRequest); started(); return new Promise(resolve => { release = resolve }) }
  const first = request('queue', { scope, content: text, confirmed_latch: scope })
  await entered
  await assert.rejects(request('queue', { scope, content: text, confirmed_latch: scope }))
  await assert.rejects(request('query', { scope: { ...scope, attempt_token: 'other-attempt' } }))
  release({ accepted: true }); assert.equal((await first).state, 'queued')
  assert.equal(calls.length, 1)
})

test('irrelevant scalar event data does not obscure exact native pending evidence', async () => {
  const { request, calls } = await setup([event(0, 'fixture/unrelated', null), { ...inserted(), seq: 1 }])
  await request('register', { scope })
  assert.equal((await request('query', { scope })).state, 'queued')
  assert.equal(calls.length, 0)
})

test('restore collision preserves both later target and protected replacement, reporting static failure', async t => {
  const { closeSocketListener } = await import('./host.js')
  const dir = await mkdtemp(realpathSync(tmpdir()) + '/mn-collision-'); await chmod(dir, 0o700)
  t.after(() => rm(dir, { recursive: true, force: true }))
  const socketPath = dir + '/host.sock'
  const server = net.createServer(socket => socket.destroy())
  await new Promise(resolve => server.listen(socketPath, resolve))
  const owned = await lstat(socketPath)
  await rm(socketPath); await writeFile(socketPath, 'replacement fixture', { mode: 0o600 })
  const replacement = await lstat(socketPath)
  const actualClose = server.close.bind(server)
  // Decorate the real public callback to deterministically place a later target
  // after libuv closes and before the production no-overwrite restore step.
  server.close = callback => actualClose(error => { writeFile(socketPath, 'later fixture', { mode: 0o600 }).then(() => callback(error)) })
  await assert.rejects(closeSocketListener(server, socketPath, owned), error => error.code === 'socket_replaced' && error.message === 'Desktop host cleanup requires recovery.')
  assert.equal(await readFile(socketPath, 'utf8'), 'later fixture')
  const dirs = (await readdir(dir)).filter(name => name.startsWith('.mn-host-close-'))
  assert.equal(dirs.length, 1)
  const protectedPath = dir + '/' + dirs[0] + '/replacement'
  assert.equal((await lstat(protectedPath)).ino, replacement.ino)
  assert.equal(await readFile(protectedPath, 'utf8'), 'replacement fixture')
})

test('directory replacement is retained for manual recovery without recursive modification', async t => {
  const { host, socketPath } = await temporaryListener(t, {}, true)
  await rm(socketPath); await mkdir(socketPath, { mode: 0o700 }); await writeFile(socketPath + '/fixture', 'directory bytes', { mode: 0o600 })
  const before = await lstat(socketPath)
  await assert.rejects(host.close(), error => error.code === 'socket_replaced')
  await assert.rejects(host.close(), error => error.code === 'socket_replaced')
  const protectedDir = (await readdir(pathDirectory(socketPath))).find(name => name.startsWith('.mn-host-close-'))
  const protectedPath = pathDirectory(socketPath) + '/' + protectedDir + '/replacement'
  assert.equal((await lstat(protectedPath)).ino, before.ino)
  assert.equal(await readFile(protectedPath + '/fixture', 'utf8'), 'directory bytes')
  // This test deliberately caused cleanup failure; the fake directory fixture
  // is removed only by its parent fixture cleanup, not by production teardown.
})
const pathDirectory = filename => filename.slice(0, filename.lastIndexOf('/'))

test('previously observed evidence permanently blocks enqueue after native prefix regression', async () => {
  for (const operation of ['register', 'queue', 'query']) {
    for (const initial of [[inserted()], [event(1, 'turn/start', { turn: 1 })]]) {
      const { request, view, calls } = await setup(operation === 'register' ? initial : [])
      await request('register', { scope })
      if (operation !== 'register') {
        view.events = initial
        const first = await request(operation, operation === 'queue' ? { scope, content: text, confirmed_latch: scope } : { scope })
        assert.ok(['queued', 'unknown'].includes(first.state))
      }
      view.events = []
      await request('queue', { scope, content: text, confirmed_latch: scope })
      assert.equal(calls.length, 0, operation + ' evidence must survive a shorter native prefix')
    }
  }
})

function ownCompletionEvents(turn = 3, step = 1) {
  return [inserted(), event(1, 'turn/start', { turn }), event(2, 'agent/inbox/spliced', { target: 'next-turn', start: 0, removedCount: 1, inserted: [] }), event(3, 'step/start', { turn, step }), event(4, 'user/message', msg, { surfaceOp: 'append' }), event(5, 'assistant/message', { turn, step, message: { id: 'assistant-1', content: [{ type: 'text', text: 'hidden native reply' }], source: { provider: 'fixture', model: 'fixture' } }, stream: { id: 'fixture-stream' } }, { surfaceOp: 'append' }), event(6, 'step/end', { turn, step }), event(7, 'turn/end', { turn, reason: { kind: 'completed' } })]
}

test('malformed native turn and step identities always return a safe unknown projection', async () => {
  for (const invalid of ['private native body', { private: 'private native body' }, ['private native body'], null, true, 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    const histories = [ownCompletionEvents(invalid, 1), ownCompletionEvents(3, invalid)]
    for (const [position, field] of [[1, 'turn'], [3, 'turn'], [3, 'step'], [5, 'turn'], [5, 'step'], [6, 'turn'], [6, 'step'], [7, 'turn']]) {
      const events = ownCompletionEvents(); events[position].data[field] = invalid; histories.push(events)
    }
    for (const events of histories) {
      const { request, view, calls } = await setup(events)
      await request('register', { scope })
      assert.deepEqual(await request('query', { scope }), { state: 'unknown', native_request_id: scope.native_request_id })
      // All unknown paths, including a later gap or duplicate, must remain a
      // safe projection rather than spread previously accumulated native data.
      view.events.push(event(10, 'fixture/gap', null))
      assert.deepEqual(await request('query', { scope }), { state: 'unknown', native_request_id: scope.native_request_id })
      view.events = []
      await request('queue', { scope, content: text, confirmed_latch: scope })
      assert.equal(calls.length, 0)
    }
  }
})

test('mismatched native structural boundaries cannot claim completion', async () => {
  for (const change of [
    events => { events[3].data.turn = 99 },
    events => { events[5].data.step = 99 },
    events => { events[6].data.turn = 99 },
    events => { events[7].data.turn = 99 },
    events => { events[3] = event(3, 'step/start', { turn: 3, step: 2 }) },
    events => { events[3] = event(3, 'turn/start', { turn: 3 }) },
  ]) {
    const events = ownCompletionEvents(); change(events)
    const { request, calls } = await setup(events)
    await request('register', { scope })
    assert.deepEqual(await request('query', { scope }), { state: 'unknown', native_request_id: scope.native_request_id })
    assert.equal(calls.length, 0)
  }
})

for (const defect of [null, 'legacy', 'peer-capability', 'codex-local', 'missing-peer-expectation', 'array-alias', 'object-alias', 'number-alias', 'boolean-alias', 'null-alias', 'invalid-alias']) {
  test(`v2 host activates only own DSH binding (${defect ?? 'valid'})`, async t => {
    const { loadHostConfig } = await import('./host.js')
    const dir = await mkdtemp(realpathSync(tmpdir()) + '/mn-local-')
    t.after(() => rm(dir, { recursive: true, force: true }))
    const raw = Object.fromEntries(Object.entries(binding).filter(([k]) => k !== 'alias'))
    const peer = { ...raw, agent_id: 'peer-runtime', session_id: 'session-33333333-3333-3333-3333-333333333333' }
    const expected = { client_id: 'own-client', agent_id: 'own-visible-agent', discord_user_id: '123' }
    const profile = { version: 2, local_alias: 'receiver', workspace_id: 'w', channel_id: 'c',
      mcp_url: 'https://fixture.invalid/mcp', lifecycle_profile: dir + '/absent-life.json',
      channel_profiles: { receiver: dir + '/absent-own.json' },
      sender_expectations: { receiver: expected, peer: { ...expected, client_id: 'peer-client', discord_user_id: '456' } },
      bindings: { receiver: raw, peer } }
    if (defect === 'array-alias') profile.local_alias = ['receiver']
    if (defect === 'object-alias') profile.local_alias = {}
    if (defect === 'number-alias') profile.local_alias = 1
    if (defect === 'boolean-alias') profile.local_alias = true
    if (defect === 'null-alias') profile.local_alias = null
    if (defect === 'invalid-alias') profile.local_alias = 'receiver.invalid'
    if (defect === 'legacy') profile.version = 1
    if (defect === 'peer-capability') profile.channel_profiles.peer = dir + '/absent-peer.json'
    if (defect === 'codex-local') profile.bindings.receiver = { ...raw, kind: 'codex', session_id: '11111111-1111-1111-1111-111111111111' }
    if (defect === 'missing-peer-expectation') delete profile.sender_expectations.peer
    await writeFile(dir + '/profile.json', JSON.stringify(profile), { mode: 0o600 })
    await writeFile(dir + '/worker.token', 'fake-worker', { mode: 0o600 })
    await writeFile(dir + '/config.json', JSON.stringify({ version: 1, profile_file: dir + '/profile.json',
      socket_path: dir + '/host.sock', worker_token_file: dir + '/worker.token' }), { mode: 0o600 })
    if (defect) await assert.rejects(loadHostConfig(dir + '/config.json'))
    else assert.deepEqual(Object.keys((await loadHostConfig(dir + '/config.json')).bindings), ['receiver'])
  })
}
