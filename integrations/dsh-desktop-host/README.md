# MultiNexus DSH desktop Host

Thin Cordis Host plugin for DSH **0.2.1-rc.2**, disabled by default. It uses the
existing `sessionController` service and only existing, explicitly allowlisted
DSH sessions. This source has not been installed or enabled by its tests.

The bundle manifest is `cordis.patch.yml`. Any later installation and activation
belongs to the separately approved Plugin Manager workflow. Setting the plugin
configuration `enabled: true` loads the default private configuration file:

```text
~/.config/multinexus/dsh-desktop-host.json
```

The file accepts exactly these fields:

```json
{
  "version": 1,
  "profile_file": "/absolute/private/desktop-profile.json",
  "socket_path": "/absolute/private/host.sock",
  "worker_token_file": "/absolute/private/worker.token"
}
```

These example paths are placeholders. Files must be current-user-owned regular
files without group/other permissions, with absolute paths and no symlink path
components. The socket's parent must already exist, belong to the current user,
and have mode `0700`; the ready socket has mode `0600`. Existing nodes/listeners
are rejected, including stale sockets. Normal teardown closes only the owned
listener. If another inode has replaced its pathname, Node/libuv would otherwise
unlink that replacement: the Host temporarily renames it into a newly created
`.mn-host-close-*` directory under the same private parent, closes the listener,
then restores via atomic `link` that refuses to overwrite a later target.
Successful restore preserves the original inode and bytes. If restore collides
or the node cannot be hardlinked (for example a directory), the protected inode
is retained and cleanup fails with static `socket_replaced` / `Desktop host
cleanup requires recovery.`; manual recovery is required. No recursive cleanup or
destructive retry occurs. This bounded exception does not promise isolation from
malicious same-UID pathname races. No TCP listener or credential in process
arguments is used. Same-UID code is trusted; these permissions do not isolate
malicious code running as the same user. Missing/invalid configuration emits only
`Desktop host not ready.` and never creates a session.

`profile_file` requires the version-2 desktop profile: `local_alias`,
`sender_expectations` for every binding (exact `client_id`, canonical `agent_id`,
and `discord_user_id`), and `channel_profiles` containing only the local alias.
The local binding must be DSH. Peer bindings remain metadata and cannot be
inspected, registered or queued by this Host, even when they are also DSH.
The Host never reads channel/lifecycle credential files. Arbitrary aliases are
supported. Host config and worker wire protocol remain version 1; neither is
the desktop profile version. Version-1 profiles require an explicit upgrade.
All session operations inspect the exact original session ID and cwd and reject
subagents/children. No title search, fresh agent, model selection, permissions,
steer, native cancellation, listener for channel traffic, persistent task ledger,
recovery authority, or scheduler is added.

## Worker wire protocol

One bounded JSON line per Unix connection, at most 65,536 bytes including newline,
with a five-second connection timeout. Maximum 32 connections and 128 active
scopes; excess work is rejected rather than queued. Every request contains
`version: 1`, a fixed `op`, and `worker_token`, read from the private shared file.
Extra top-level fields and unknown operations are rejected. Errors contain only
`Desktop host unavailable.`; neither prompt nor native body appears in replies
or diagnostics.

Operations and their additional fields:

| `op` | Fields | Result |
| --- | --- | --- |
| `inspect` | `binding` (Task 1 `DesktopBinding.as_dict()`) | exact identity, digest, readiness |
| `register` | `scope` | `registered: true`, exact registered scope |
| `queue` | `scope`, `content`, `confirmed_latch` | `queued`, observed native state, or `unknown` |
| `query` | `scope` | only this request's bounded native metadata |
| `close` | `scope` | `closed: true`; stops scope observation only |

Scope has exactly eight nonempty string fields:
`job_id`, `attempt_token`, `lease_id`, `native_request_id`, `binding_digest`,
`payload_digest`, `native_content_digest`, `recipient`.

`binding_digest` uses Task 1's sorted canonical UTF-8 JSON SHA256;
`payload_digest` remains the original channel body SHA256.
`native_content_digest` is SHA256 of the complete UTF-8 text actually queued to
DSH, including any worker envelope. They may differ. The Host recomputes the
native hash before enqueue and checks the fixed binding digest.

`confirmed_latch` must equal the active scope field-for-field. It is the trusted
worker's attestation that it awaited a fenced progress write and verified the
exact readback. The Host does **not** independently validate server lease state.
Python `DSHHostTransport.queue(scope, content)` sends this attestation; callers
must invoke it only after the latch gate succeeds. ACK handling/capabilities
belong entirely to the worker's own active ACK channel and never to this Host.

Admission is serialized per alias without waiting for model completion. Before
calling native `prompt`, the volatile scope records that enqueue was attempted.
A second call for that scope returns its prior queued result or `unknown` and
never calls prompt again, even after an exception or a lost socket reply.
This volatile guard is not durable recovery authority. After Host reload,
workers must honor their existing durable latch and only observe; registering
a scope does not grant permission to replay an uncertain request.

Readonly inspection/query use only `inspect(sessionId, signal)`. The Host never
calls `follow`, whose cold-session implementation may activate an agent.
It folds the complete contiguous event prefix and native inbox splices for the
exact `source.rpcId`, checks content/message identity, and correlates admission
with the open turn/step. A claim-to-log gap, malformed/truncated prefix, duplicate
identity, canceled claim, or mixed external input stays `unknown` and never
causes automatic reenqueue. Old busy turns do not complete this request.

A native `{accepted:true}` is only `queued`. `native_turn_completed` is metadata
for a correlated final assistant message and completed turn; it is **not** a
receiver ACK or job success. Native/reply body content stays inside the Host.
Closing a scope, disconnecting, or tearing down this transport never calls
`controller.cancel` and never cancels an original native turn.

## Local verification

```sh
node --test integrations/dsh-desktop-host/host.test.js
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_desktop_host_transport.py -q
```

All sockets/files/controllers/processes in these tests are temporary fixtures.
Tests include Python-to-Node roundtrips but do not establish live desktop,
restart/reconnect, busy native agent, Plugin Manager, Windows, or business
acceptance.
