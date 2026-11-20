# T3 Code server API: notes for the bridge

What the bridge needs from a T3 Code server, worked out from the server and
contracts source (`apps/server/src`, `packages/contracts/src`,
`packages/client-runtime/src` in the T3 Code repo) and checked against a live
server by `bridge/api_test.py`.

Checked against: T3 Code server 0.1.12 and `main` as of 2026-11. Every endpoint,
RPC method, and command used below exists in both with the same shape. Orchestration protocol version: **2**.

Source paths below are relative to the T3 Code repo.

## Stability at a glance

| Piece | Confidence | Why |
|---|---|---|
| `t3 auth session issue`, bearer tokens, scopes | Stable | Documented for headless clients (`docs/internals/environment-auth.md`); scope names are kept backward-compatible on purpose. |
| `/.well-known/t3/environment` | Stable | Discovery descriptor; carries `serverVersion` and `orchestrationProtocolVersion`. |
| Protocol version gate (`x-t3-orchestration-protocol: 2`, `?orchestrationProtocol=2`) | Stable mechanism, the number will change | The server answers `426` with `orchestration_protocol_incompatible` on mismatch. The bridge should check the descriptor at startup and fail loudly. |
| `/api/orchestration/shell`, `/threads/:id`, `/threads/:id/transcript` | Fairly stable | Typed `HttpApi` endpoints in `contracts/environmentHttp.ts`, versioned by the protocol header, used by T3's own clients. |
| WS RPC framing (Effect RPC, JSON) | Stable while T3 uses Effect RPC | Generic library wire format (`effect/rpc/RpcMessage.ts`), not T3-specific. |
| `orchestration.dispatchCommand` with `message.dispatch`, `runtime-request.respond`, `thread.delete`, `thread.stop`, `run.interrupt` | Medium | The same commands the web and mobile apps send (`client-runtime/src/operations/commands.ts`). The command union grows often. Optional fields come and go, but the required core has held. |
| `orchestration.launchThread` | Medium | One-shot create plus first message, used by the web app and by MCP. |
| Turn item `type` values and their fields | Internal, grows | New kinds keep appearing (`handoff`, `fork`, `subagent`, ...). Unknown types must render as a generic marker, never crash. |
| Shell thread fields beyond the core | Internal | Many are recent and may be missing on some servers (`settledAt`, `snoozedUntil`, `goal`, `pinOrderKey`, ...). Read defensively. |
| ID formats | Opaque | IDs look like `thread:project:<pid>:<uuid>` or `runtime-request:provider:...`, with percent-encoding. Never parse them. URL-encode them in paths. |

## Auth

```sh
t3 auth session issue --scope orchestration:read --scope orchestration:operate \
    --label t3-koreader --ttl 30d --token-only
```

- Without `--scope`, the token gets every scope, including terminal and access
  management. The bridge needs only `orchestration:read` (snapshots) and
  `orchestration:operate` (send, answer, stop).
- HTTP: `authorization: Bearer <token>`.
- WebSocket: browsers can't set headers, so clients mint a short-lived ticket
  with `POST /api/auth/websocket-ticket/rpc` (bearer auth) → `{"ticket": "..."}`,
  then open `/ws?orchestrationProtocol=2&wsTicket=<ticket>`. A ticket names its
  route (`rpc` or `media`).
- Sessions with subject `mcp-client` (from the `/mcp` OAuth flow) are rejected
  outside `/mcp`. Use a normal session from the command above.

## Reads (HTTP)

All require `x-t3-orchestration-protocol: 2`.

### `GET /api/orchestration/shell`

`{schemaVersion, snapshotSequence, threads[], archivedThreads[], projects[], projectIdentities}`.
The thread list in one call (about 250 threads came to a few hundred KB). Useful
thread fields:

- `id`, `projectId`, `title`, `createdBy` (`user|agent|system`), `lineage.parentThreadId`
  (set for subagent/child threads; hide these by default)
- `status`: `idle` or a run status: `preparing|queued|starting|running|waiting|completed|interrupted|failed|cancelled|rolled_back`.
  Upstream treats `queued, preparing, starting, running, waiting` as live (`LIVE_SHELL_STATUSES`).
- `pendingRuntimeRequest`: `null` or `{id, kind, createdAt, blocking?}`. This is "needs you".
- `itemCount`, `updatedAt`, `latestUserMessageAt`, `archivedAt`, `settledAt`
- `latestVisibleMessage` is often `null` even for threads with replies, so don't
  rely on it for previews. Read the transcript tail instead.

Projects: `{id, title, workspaceRoot, ...}`. `workspaceRoot` is a host path, so
don't send it to the device.

Optional query flags: `omitIdleSubagents`, `omitOlderSettled`.

### `GET /api/orchestration/threads/:threadId/transcript`

`{threadId, title, updatedAt, items: [{position, visibility, sourceThreadId, item}]}`.
`item` is a turn item with `type`, `ordinal` (monotonic per thread), `status`, timestamps and:

| `type` | Fields worth showing |
|---|---|
| `user_message` | `text`, `attachments`, `createdBy`, `creationSource` |
| `assistant_message` | `text`, `streaming` |
| `command_execution` | `title`, `input` (the command), `output`, `exitCode` |
| `dynamic_tool` | `toolName`, `input`, `output` |
| `file_change` | changes |
| `approval_request` | `requestId`, `prompt`, `reason`, `options?` |
| `user_input_request` | `requestId`, `questions[]` |
| `reasoning`, `proposed_plan`, `todo_list`, `web_search`, `file_search`, `subagent`, `error`, `system_notice`, `checkpoint`, `compaction`, `handoff`, `fork`, ... | marker only |

**Reading "since a point"**: filter on `item.ordinal > last_seen`. Ordinals
look like `run * 1_000_000 + n`. Treat them as opaque but ordered. There's no
server-side "since" filter on this endpoint. The whole transcript came to about
17 KB for a short thread. Long threads will be bigger, so the bridge should
cache and trim before anything reaches the device.

### `GET /api/orchestration/threads/:threadId`

`{snapshotSequence, projection: {thread, runs, messages, runtimeRequests, turnItems, ...}}`.
The full projection, needed for pending-request details (see below).

Also present but not yet used: `/threads/:id/bounded` (a size-bounded snapshot)
and `/threads/:id/history?cursor=...` (paging).

## Writes (WebSocket RPC)

### Framing

Effect RPC, JSON serialization, one JSON value per WebSocket text frame (the
value may be an array of messages).

```jsonc
// client → server
{"_tag": "Request", "id": "1", "tag": "orchestration.dispatchCommand", "payload": {...}, "headers": []}
// server → client
{"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Success", "value": {"sequence": 222378}}}
{"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": {...}}]}}
// also possible: {"_tag": "Chunk", ...} for streams, {"_tag": "Defect", ...}, {"_tag": "Pong"}
```

Every RPC declares a required scope, checked before the handler runs
(`apps/server/src/auth/RpcAuthorization.ts`).

### Send a message to an existing thread

`orchestration.dispatchCommand`:

```json
{"type": "message.dispatch", "commandId": "<uuid>", "createdBy": "user", "creationSource": "mobile",
 "threadId": "...", "messageId": "<uuid>", "text": "...", "attachments": [],
 "deliveryIntent": "auto", "dispatchMode": {"type": "start_immediately"}}
```

This matches the web composer's default. `dispatchMode: {"type": "queue_after_active"}`
(with no `deliveryIntent`) queues behind a running turn instead. Result: `{sequence}`.

`creationSource` is a closed enum (`web|mobile|mcp|provider|server`). There's no
value for third-party clients, so the bridge sends `mobile`.

### Start a thread

`orchestration.launchThread`:

```json
{"commandId": "<uuid>", "creationSource": "mobile", "projectId": "...", "title": "...",
 "modelSelection": {"instanceId": "...", "model": "..."},
 "runtimeMode": "approval-required", "interactionMode": "default",
 "workspaceStrategy": {"type": "root"},
 "initialMessage": {"messageId": "<uuid>", "text": "...", "attachments": []}}
```

Returns `{threadId, projection, resumed, sequence?}`. The server picks the thread ID.
`runtimeMode`: `approval-required|auto-accept-edits|auto|full-access`.
`workspaceStrategy` can also be `{"type": "worktree", "baseRef": ..., "branch"?, "startFromOrigin"?}`
or `{"type": "existing_worktree", "worktreePath": ...}`. For model choice, the
bridge should default to the project's `defaultModelSelection`, or to the
model of the user's most recent thread in that project.

### Answer a pending request

To find it, do what the web app does (`client-runtime/src/state/threadRequests.ts`,
`derivePendingThreadRequests`). Take `projection.runtimeRequests` with
`status == "pending"`, skip `kind` `auth_refresh` and `dynamic_tool_call`, and
join each to the last turn item with the same `requestId`:

- `kind == "user_input"` → `user_input_request` item, with `questions[]`:
  `{id, header, question, options: [{label, description?}], multiSelect, allowCustomAnswer?}`.
- any other kind (`command`, `file-change`, ...) → `approval_request` item, with `prompt`, `reason`, `options?`.

Also check `responseCapability.type`: `live` can be answered, `message` means
"answer by sending a message", and `not_resumable` can't be answered.

Then dispatch:

```jsonc
// approval
{"type": "runtime-request.respond", "commandId": "<uuid>", "threadId": "...", "requestId": "...",
 "decision": "accept"}   // accept | acceptForSession | acceptAlways | decline | cancel; optional "feedback"
// question
{"type": "runtime-request.respond", "commandId": "<uuid>", "threadId": "...", "requestId": "...",
 "answers": {"<question id>": "<option label>"}}   // list of labels for multiSelect; free text allowed if allowCustomAnswer != false
```

Both verified live: a declined file write (the agent replied "DECLINED" and no
file was created), and a two-option question answered with the first label.

### Retries are safe with a stable `commandId`

T3 keeps a receipt per `commandId`. Sending the same command ID again returns the
original result (`replayed: true` on the server side) instead of running the
command twice (`apps/server/src/orchestration-v2/Orchestrator.ts`). The bridge
derives command and message IDs from the device's `client_msg_id`, so a retried
tap can't send twice.

### Other commands the client will want

- Stop: when the environment descriptor has `capabilities.threadStop`, the web app sends
  `{"type": "thread.stop", "threadId"}`, which stops the thread and its delegated tasks.
  Without it, it sends `{"type": "run.interrupt", "threadId", "runId", "holdQueue": true}`
  for the active run.
- Mark read: `{"type": "thread.visit", "threadId", "visitedAt": <ISO time>, "opened": true}`
  moves the read watermark (`lastVisitedAt` in the shell), the same as opening the thread in the app.
- Switch model: `{"type": "thread.model-selection.set", "threadId", "modelSelection": {"instanceId", "model"}}`,
  what the app's picker sends. It can change provider too. Verified live (Haiku → Sonnet).
  The models come from `server.getConfig`: `providers[].models[]` (`slug`, `name`, `isLegacy`),
  plus `settings.favorites` (`[{provider, model}]`, `provider` being the instance id) and
  `settings.providerModelPreferences[<instance>].hiddenModels`.
- Delete (test cleanup only): `{"type": "thread.delete", "threadId"}`.

### What `creationSource` does

It's recorded on every message, but the apps don't display it for your own
messages. Behaviourally, only `web` and `mobile` user messages release a queue
that was held after a stop or failure (`Orchestrator.ts`, `sentByUserFromClient`).
The bridge sends `mobile`, so it behaves like the apps.

### Runtime mode matters for approvals

A thread only asks for approvals if its `runtimeMode` calls for them. Even then,
the agent ran read-only commands such as `echo` without asking. A file write
outside the workspace did ask (with `reason: "Path is outside allowed working directories"`).

## Change notifications (not used yet)

`orchestration.subscribeShell` (`{afterSequence?}`) and
`orchestration.subscribeThread` are streaming RPCs (Chunk messages). The
server wraps the protocol in an output window (`withOutputWindow`), so stream
consumers probably need to send `Ack`s. The bridge could use `subscribeShell`
to learn when turns finish instead of polling the snapshot, which also gives a
clean trigger for generating suggestions. The bridge polls
`/api/orchestration/shell` instead: every 2 s while something runs or a device is
active, every 20 s otherwise. The trade-off: polling is simpler and resyncs for
free after restarts, but it re-downloads the snapshot and lags by up to one interval.

## Not used

- The SQLite database: internal, off-limits by design.
- `/mcp` (the orchestrator MCP server): it requires an OAuth `mcp-client`
  session, and its tool output is written for LLMs, not for rendering.
