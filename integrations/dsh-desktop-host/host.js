/** Private, bounded transport. Native inbox remains the only input queue. */
import { createHash, timingSafeEqual } from 'node:crypto'
import { constants } from 'node:fs'
import { open, lstat, chmod, unlink, mkdtemp, rename, link, rmdir } from 'node:fs/promises'
import path from 'node:path'
import net from 'node:net'

export const MAX_FRAME = 65536
const MAX_PRIVATE = 1048576
const TIMEOUT_MS = 5000
const SCOPE_FIELDS = ['job_id', 'attempt_token', 'lease_id', 'native_request_id', 'binding_digest', 'payload_digest', 'native_content_digest', 'recipient']
const BINDING_FIELDS = ['alias', 'kind', 'agent_id', 'session_id', 'cwd', 'revision']
const fail = () => { throw new Error('Desktop host unavailable.') }
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v)
const exact = (v, fields) => object(v) && Object.keys(v).length === fields.length && fields.every(k => Object.hasOwn(v, k))
const string = v => typeof v === 'string' && v.trim().length > 0 && !/[\x00-\x1f\x7f]/.test(v)
const sha = v => createHash('sha256').update(v, 'utf8').digest('hex')
const canonical = v => JSON.stringify(v, Object.keys(v).sort())
const same = (a, b) => object(a) && object(b) && canonical(a) === canonical(b)
const digest = v => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
const secretEquals = (a, b) => typeof a === 'string' && typeof b === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b))

function validBinding(binding) {
  if (!exact(binding, BINDING_FIELDS) || !Object.values(binding).every(string)
      || !['codex', 'dsh'].includes(binding.kind) || !path.isAbsolute(binding.cwd)
      || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(binding.alias)
      || !(binding.kind === 'dsh' ? /^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/ : /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/).test(binding.session_id)) fail()
  return binding
}

async function noSymlinks(filename) {
  if (typeof filename !== 'string' || !path.isAbsolute(filename) || path.normalize(filename) !== filename) fail()
  const parts = filename.split(path.sep).filter(Boolean)
  let current = path.parse(filename).root
  for (const part of parts) {
    current = path.join(current, part)
    const info = await lstat(current)
    if (info.isSymbolicLink()) fail()
  }
}

async function readPrivate(filename) {
  let fd
  try {
    await noSymlinks(filename)
    fd = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const info = await fd.stat()
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > MAX_PRIVATE) fail()
    const data = await fd.readFile()
    if (data.length > MAX_PRIVATE) fail()
    return new TextDecoder('utf-8', { fatal: true }).decode(data)
  } finally { await fd?.close() }
}

export async function loadHostConfig(filename) {
  try {
    const config = JSON.parse(await readPrivate(filename))
    if (!exact(config, ['version', 'profile_file', 'socket_path', 'worker_token_file']) || config.version !== 1) fail()
    for (const field of ['profile_file', 'socket_path', 'worker_token_file']) if (!string(config[field]) || !path.isAbsolute(config[field])) fail()
    const profile = JSON.parse(await readPrivate(config.profile_file))
    if (!exact(profile, ['version', 'workspace_id', 'channel_id', 'mcp_url', 'channel_profiles', 'lifecycle_profile', 'bindings', 'local_alias', 'sender_expectations'])
        || profile.version !== 2 || typeof profile.local_alias !== 'string'
        || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(profile.local_alias) || !object(profile.bindings) || !Object.keys(profile.bindings).length
        || !object(profile.channel_profiles) || !sameKeys({ [profile.local_alias]: true }, profile.channel_profiles)
        || !Object.hasOwn(profile.bindings, profile.local_alias) || !object(profile.sender_expectations)
        || !sameKeys(profile.bindings, profile.sender_expectations)
        || ![profile.workspace_id, profile.channel_id, profile.mcp_url].every(string)
        || !path.isAbsolute(profile.lifecycle_profile)) fail()
    if (!path.isAbsolute(profile.channel_profiles[profile.local_alias])) fail()
    for (const value of Object.values(profile.sender_expectations)) {
      if (!exact(value, ['client_id', 'agent_id', 'discord_user_id']) || !Object.values(value).every(string)
          || !/^[1-9][0-9]{0,19}$/.test(value.discord_user_id)
          || ![value.client_id, value.agent_id].every(v => /^[A-Za-z0-9_.:-]+$/.test(v))) fail()
    }
    const bindings = Object.create(null)
    for (const [alias, raw] of Object.entries(profile.bindings)) {
      if (!exact(raw, BINDING_FIELDS.filter(k => k !== 'alias'))) fail()
      bindings[alias] = validBinding({ alias, ...raw })
    }
    for (const key of ['agent_id', 'session_id']) if (new Set(Object.values(bindings).map(b => b[key])).size !== Object.keys(bindings).length) fail()
    if (bindings[profile.local_alias].kind !== 'dsh') fail()
    const localBindings = { [profile.local_alias]: bindings[profile.local_alias] }
    const workerToken = (await readPrivate(config.worker_token_file)).trim()
    if (!string(workerToken)) fail()
    return { bindings: localBindings, workerToken, socketPath: config.socket_path }
  } catch { fail() }
}
const sameKeys = (a, b) => Object.keys(a).sort().join('\0') === Object.keys(b).sort().join('\0')

/** Fold only this rpcId from the complete ordinary-session event prefix. */
function nativeState(view, scope) {
  const result = { state: 'not_found', native_request_id: scope.native_request_id }
  const unknown = () => ({ state: 'unknown', native_request_id: scope.native_request_id })
  const index = value => Number.isSafeInteger(value) && value > 0
  if (!Array.isArray(view.events) || view.inheritedEventCount !== 0) return unknown()
  const inbox = { 'next-turn': [], 'next-step': [] }
  const ids = new Set()
  let openTurn, openStep, ownTurn, lastTurn = 0, lastStep = 0, admitted = false, answered = false, mixed = false, found = false
  const match = message => {
    if (message?.source?.kind !== 'user' || message.source.rpcId !== scope.native_request_id) return false
    if (typeof message.id !== 'string' || !Array.isArray(message.content) || message.content.length !== 1
        || message.content[0]?.type !== 'text' || typeof message.content[0].text !== 'string'
        || sha(message.content[0].text) !== scope.native_content_digest) fail()
    ids.add(message.id); if (ids.size > 1) fail()
    found = true
    return true
  }
  try {
    for (let i = 0; i < view.events.length; i++) {
      const event = view.events[i]
      if (!object(event) || event.seq !== i || typeof event.type !== 'string') return unknown()
      if (!['turn/start', 'turn/end', 'step/start', 'step/end', 'agent/inbox/spliced', 'user/message', 'assistant/message'].includes(event.type)) continue
      if (!object(event.data)) return unknown()
      const data = event.data
      if (event.type === 'turn/start') {
        if (!index(data.turn) || openTurn !== undefined || data.turn <= lastTurn) return unknown()
        openTurn = data.turn; lastTurn = data.turn; lastStep = 0
      }
      if (event.type === 'step/start') {
        if (!index(data.turn) || !index(data.step) || data.turn !== openTurn
            || openStep !== undefined || data.step !== lastStep + 1) return unknown()
        openStep = data.step; lastStep = data.step
      }
      if (event.type === 'step/end') {
        if (!index(data.turn) || !index(data.step) || data.turn !== openTurn || data.step !== openStep) return unknown()
        openStep = undefined
      }
      if (event.type === 'agent/inbox/spliced') {
        const pending = inbox[data.target], count = data.removedCount ?? 0
        if (!pending || !Number.isSafeInteger(data.start) || data.start < 0 || data.start > pending.length
            || !Number.isSafeInteger(count) || count < 0 || data.start + count > pending.length || !Array.isArray(data.inserted)) return unknown()
        const removed = pending.splice(data.start, count, ...data.inserted)
        for (const message of data.inserted) if (match(message)) result.state = 'queued'
        for (const message of removed) if (match(message)) result.state = 'unknown' // claim-to-log gap is uncertain
      }
      if (event.type === 'user/message' && event.surfaceOp === 'append') {
        if (match(data)) {
          if (admitted || openTurn === undefined || openStep === undefined) return unknown()
          ownTurn = openTurn; admitted = true; result.state = 'admitted'; result.turn = ownTurn; result.admitted_seq = event.seq
        } else if (ownTurn === openTurn && admitted && data.source?.kind === 'user') mixed = true
      }
      if (event.type === 'assistant/message') {
        if (!index(data.turn) || !index(data.step) || data.turn !== openTurn || data.step !== openStep
            || !object(data.message) || !Array.isArray(data.message.content)
            || !data.message.content.every(p => object(p) && typeof p.type === 'string')) return unknown()
        if (admitted && data.turn === ownTurn && event.seq > result.admitted_seq && event.surfaceOp === 'append') {
          answered = data.message.content.some(p => p.type === 'text' && typeof p.text === 'string' && p.text.trim())
            && !data.message.content.some(p => p.type === 'tool-call')
        }
      }
      if (event.type === 'turn/end') {
        if (!index(data.turn) || data.turn !== openTurn || openStep !== undefined || !object(data.reason)
            || !['completed', 'max-tokens', 'blocked', 'aborted', 'error'].includes(data.reason.kind)) return unknown()
        if (admitted && data.turn === ownTurn) {
          result.state = data.reason?.kind === 'completed' && answered && !mixed ? 'native_turn_completed' : 'unknown'
          result.end_seq = event.seq
        }
        if (data.turn === openTurn) { openTurn = undefined; openStep = undefined }
      }
    }
  } catch { return unknown() }
  if (mixed || ids.size > 1 || result.state === 'unknown') return unknown()
  return found ? result : { state: 'not_found', native_request_id: scope.native_request_id }
}

function recordNativeObservation(active, view) {
  const state = nativeState(view, active.scope)
  if (state.state !== 'not_found') {
    active.enqueueBlocked = true
    active.result = state
  }
  return state
}

export class DesktopHost {
  constructor({ controller, bindings, workerToken }) {
    if (!string(workerToken) || !object(bindings)) fail()
    this.controller = controller; this.workerToken = workerToken
    this.bindings = Object.freeze(Object.fromEntries(Object.entries(bindings).map(([key, b]) => [key, Object.freeze({ ...validBinding(b) })])))
    this.scopes = new Map(); this.busy = new Set(); this.closed = false
  }
  binding(value) {
    validBinding(value)
    const fixed = this.bindings[value.alias]
    if (fixed?.kind !== 'dsh' || !same(fixed, value)) fail()
    return fixed
  }
  scope(value) {
    if (!exact(value, SCOPE_FIELDS) || !Object.values(value).every(string)
        || !['binding_digest', 'payload_digest', 'native_content_digest'].every(k => digest(value[k]))) fail()
    const binding = this.bindings[value.recipient]
    if (binding?.kind !== 'dsh' || sha(canonical(binding)) !== value.binding_digest) fail()
    return binding
  }
  async inspect(binding) {
    const view = await this.controller.inspect(binding.session_id, AbortSignal.timeout(TIMEOUT_MS))
    if (view?.meta?.id !== binding.session_id || view.meta.cwd !== binding.cwd
        || view.meta.parentSession !== undefined || view.meta.origin === 'subagent') fail()
    return view
  }
  shutdown() {
    this.closed = true; this.scopes.clear(); this.busy.clear()
  }
  async handle(request) {
    try { return await this.dispatch(request) } catch { fail() }
  }
  async dispatch(request) {
    if (this.closed || !object(request) || request.version !== 1 || !secretEquals(request.worker_token, this.workerToken)) fail()
    const fields = { inspect: ['binding'], register: ['scope'], queue: ['scope', 'content', 'confirmed_latch'], query: ['scope'], close: ['scope'] }[request.op]
    if (!fields || !exact(request, ['version', 'op', 'worker_token', ...fields])) fail()
    if (request.op === 'inspect') {
      const binding = this.binding(request.binding); await this.inspect(binding)
      return { ready: true, binding_digest: sha(canonical(binding)), session_id: binding.session_id, cwd: binding.cwd }
    }
    const binding = this.scope(request.scope)
    const scope = request.scope, key = scope.native_request_id
    if (this.busy.has(binding.alias)) fail()
    this.busy.add(binding.alias)
    try {
      const active = this.scopes.get(key)
      if (request.op === 'register') {
        if (active && !same(active.scope, scope)) fail()
        if (!active) {
          if (this.scopes.size >= 128 || [...this.scopes.values()].some(s => s.scope.job_id === scope.job_id)) fail()
          const registration = { scope: Object.freeze({ ...scope }), enqueueBlocked: false }
          recordNativeObservation(registration, await this.inspect(binding))
          if (this.closed) fail()
          this.scopes.set(key, registration)
        }
        return { registered: true, scope: { ...scope } }
      }
      if (!active || !same(active.scope, scope)) fail()
      if (request.op === 'close') { this.scopes.delete(key); return { closed: true } }
      if (request.op === 'queue') {
        if (!same(request.confirmed_latch, scope) || typeof request.content !== 'string' || !request.content.trim()
            || sha(request.content) !== scope.native_content_digest) fail()
        if (active.enqueueBlocked) return active.result ?? { state: 'unknown', native_request_id: key }
      }
      let state
      try { state = recordNativeObservation(active, await this.inspect(binding)) } catch {
        active.enqueueBlocked = true
        active.result = { state: 'unknown', native_request_id: key }
        throw new Error('Desktop host unavailable.')
      }
      if (request.op === 'query') return state.state === 'not_found' && active.enqueueBlocked ? (active.result ?? { state: 'unknown', native_request_id: key }) : state
      if (state.state !== 'not_found') return state
      if (this.closed) fail()
      active.enqueueBlocked = true
      const accepted = await this.controller.prompt({ requestId: key, sessionId: binding.session_id, mode: 'queue', content: [{ type: 'text', text: request.content }] }, AbortSignal.timeout(TIMEOUT_MS))
      active.result = { state: accepted?.accepted === true ? 'queued' : 'unknown', native_request_id: key }
      return active.result
    } finally { this.busy.delete(binding.alias) }
  }
}

/** Preserve a replaced node before libuv's unconditional pathname cleanup. */
export async function closeSocketListener(server, socketPath, inode) {
  let protection
  try {
    let current
    try { current = await lstat(socketPath) } catch (error) { if (error.code !== 'ENOENT') throw error }
    const replaced = current && (current.ino !== inode.ino || current.dev !== inode.dev)
    if (replaced) {
      const parent = path.dirname(socketPath)
      await noSymlinks(parent)
      const info = await lstat(parent)
      if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) fail()
      protection = await mkdtemp(path.join(parent, '.mn-host-close-'))
      await rename(socketPath, path.join(protection, 'replacement'))
    }
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
    if (protection) {
      // link is atomic and refuses EEXIST: never overwrite a later pathname.
      await link(path.join(protection, 'replacement'), socketPath)
      await unlink(path.join(protection, 'replacement'))
      await rmdir(protection)
    }
    return { closed: true, socket_replaced: Boolean(replaced) }
  } catch {
    const error = new Error('Desktop host cleanup requires recovery.')
    error.code = 'socket_replaced'
    throw error // Leave any protected inode in place; no destructive retry.
  }
}

export async function startHost({ controller, bindings, workerToken, socketPath }) {
  let server, inode, closePromise
  const connections = new Set()
  try {
    await noSymlinks(path.dirname(socketPath))
    const parent = await lstat(path.dirname(socketPath))
    if (!parent.isDirectory() || parent.uid !== process.getuid() || (parent.mode & 0o777) !== 0o700 || !path.isAbsolute(socketPath)) fail()
    try { await lstat(socketPath); fail() } catch (error) { if (error.code !== 'ENOENT') throw error }
    const host = new DesktopHost({ controller, bindings, workerToken })
    server = net.createServer(socket => {
      if (connections.size >= 32) { socket.destroy(); return }
      connections.add(socket); socket.on('close', () => connections.delete(socket)); socket.on('error', () => {})
      socket.setTimeout(TIMEOUT_MS, () => socket.destroy())
      let bytes = Buffer.alloc(0), handled = false
      socket.on('data', data => {
        if (handled) { socket.destroy(); return }
        bytes = Buffer.concat([bytes, data])
        if (bytes.length > MAX_FRAME) { socket.destroy(); return }
        const newline = bytes.indexOf(10)
        if (newline === -1) return
        handled = true
        if (newline !== bytes.length - 1) { socket.destroy(); return }
        ;(async () => {
          let response
          try {
            const request = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, newline)))
            response = { version: 1, ok: true, result: await host.handle(request) }
          } catch { response = { version: 1, ok: false, error: 'Desktop host unavailable.' } }
          if (!socket.destroyed) socket.end(JSON.stringify(response) + '\n')
        })().catch(() => socket.destroy())
      })
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(socketPath, resolve) })
    inode = await lstat(socketPath)
    await chmod(socketPath, 0o600)
    return { close: async () => {
      if (!closePromise) {
        host.shutdown()
        for (const socket of connections) socket.destroy()
        closePromise = closeSocketListener(server, socketPath, inode)
      }
      return await closePromise
    } }
  } catch {
    if (server?.listening) {
      if (inode) await closeSocketListener(server, socketPath, inode)
      else await new Promise(resolve => server.close(resolve))
    }
    fail()
  }
}
