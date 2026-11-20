# Bridge API (device-facing)

A small HTTP/JSON API for the KOReader plugin. The bridge holds the T3
credential and does all of the T3-specific work, so the device only renders
labels and sends back IDs.

- Base path `/v1`. Responses are compact UTF-8 JSON with `content-length` (no chunking).
- Auth: `authorization: Bearer <BRIDGE_TOKEN>` on every request when a token is configured.
  The bridge refuses to listen on a non-loopback address without one.
- Errors: `{"error": "..."}` with 400 (bad input), 401, 404 (unknown thread or project),
  409 (a button that's out of date: the request was already answered, or a suggestion
  belongs to an older turn), or 502 (T3 failed).
- IDs are short (8 characters), stable aliases for T3's long IDs. They survive bridge restarts.
- Each entry comes twice: `text` is plain (markdown stripped) and `html` is the same
  content as simple HTML (headings, bold/italic, lists, tables, code blocks, quotes)
  for KOReader's HTML widget. Tool markers use `▸`, bullets use `•`. No emoji.

## Versions and waiting

Threads and the thread list carry a version `v`. A wait call returns as soon as
`v` differs from the one you pass, or after `timeout` seconds (max 120) with
`changed: false`. Versions only change when the screen should change:

- the state changes (working → done, a question appears, ...)
- suggestions arrive
- the next question of a multi-question request comes up
- new content arrives while the thread is not working

By default, new tool calls while the agent works do **not** change `v`. That keeps
e-ink redraws down. Pass `progress=1` to the thread view, the thread wait, and (as
`"progress": true`) the reply body to get a version that also changes on every new
step mid-turn. The device's "Show progress while working" setting does this. Versions reset when the bridge
restarts, so a different `v` just means "fetch again".

To catch up after being offline: call the wait endpoint with the last `v`. It
returns immediately if anything changed.

## Endpoints

### `GET /v1/threads?limit=8&project=<id>`

Threads grouped by project, most recently active project first. Subagent and
child threads are hidden for now.

```json
{"v": 262, "projects": [
  {"id": "k3x9a2bq", "title": "my-app", "updated": "2026-11-19T22:59:01.120Z", "total": 20,
   "threads": [
     {"id": "jqlcag76", "title": "Fix login", "state": "needs_you", "unread": true,
      "updated": "...", "preview": "I need your OK to run the migration."}]}]}
```

`state` is `needs_you | working | failed | done | idle`. `unread` means the last
run finished after the thread was last opened (on any device). `limit` is per
project. Pass `project` to get one project's full list.

### `GET /v1/threads/wait?v=<v>&timeout=55&limit=8`

Same body as the list, plus `"changed": true|false`.

### `GET /v1/projects`

`{"projects": [{"id", "title"}]}`, for choosing where a new thread goes.

### `GET /v1/threads/<id>?turns=3&after=<cursor>&before=<cursor>&visit=1&progress=1`

```json
{"id": "jqlcag76", "title": "Fix login", "state": "done", "v": 264, "cursor": 1000005,
 "more_before": true,
 "entries": [
   {"o": 1000001, "role": "you", "text": "Why does login fail?", "html": "<p>Why does login fail?</p>"},
   {"o": 1000003, "role": "tool", "text": "▸ ran ssh box, grep · edited auth.py"},
   {"o": 1000005, "role": "agent", "text": "The session cookie expired early. I fixed it."}],
 "pending": null,
 "replies": [
   {"id": "s:1000005:0", "label": "Great, add a test for it", "kind": "suggestion"},
   {"id": "c:continue", "label": "Continue", "kind": "shortcut"},
   {"id": "c:summarize", "label": "Summarize where we are", "kind": "shortcut"}],
 "typed": "message"}
```

- `entries`: `role` is `you | agent | tool | note`. A `tool` entry is one line covering
  a run of consecutive tool calls. `note` covers notifications, errors, questions and
  their answers, and messages sent by agents rather than you.
- Default window: everything from the `turns`-th most recent message of yours.
  `more_before` says whether older entries exist. Fetch them with `before=<o of the first entry>`.
- `after=<cursor>` returns only entries newer than a cursor (`cursor` is the `o` of the last entry).
- `visit=1` marks the thread read in T3 (pass it when the user opens the thread).
- When the thread has just finished, the call waits up to 12 s for suggestions
  already being generated, so one redraw shows both the reply and the buttons.

**`replies`**: buttons in display order. Send back `id` unchanged.

| state | buttons |
|---|---|
| `needs_you`, approval | Allow · Allow for session · Decline |
| `needs_you`, question | one per option, then Stop |
| `working` | Stop |
| `done` / `failed` / `idle` | 0–4 suggestions, then Continue · Summarize where we are |

**`pending`**: what's being asked, when `state` is `needs_you`:

```jsonc
{"kind": "approval", "header": "File change", "text": "/tmp/x.txt", "note": "Path is outside allowed working directories"}
{"kind": "question", "header": "Color", "text": "Which color?", "step": "1/2",
 "options": [{"label": "Red", "description": "Choose red."}, ...]}
```

`note` may also say the request has to be answered on another device (for
example, one that can't be resumed). Then there are no buttons.

**`typed`**: what text typed into the box will do:
- `message`: send it as a normal message
- `answer`: answer the current question with it (a custom answer)
- `none`: the question only accepts its options

### `GET /v1/threads/<id>/wait?v=<v>&timeout=55&progress=1`

Same body as the thread view, plus `"changed": true|false`.

### `POST /v1/threads/<id>/reply`

```jsonc
{"reply": "c:continue", "client_msg_id": "<unique per tap>"}
{"text": "Use the staging database", "client_msg_id": "..."}
```

Returns the new thread view. The bridge waits briefly until T3 reacts, so the
response already shows `working` (and a Stop button) after a send.

`client_msg_id` makes the request safe to retry: the same ID is never acted on
twice, and the bridge derives T3's command ID from it, so T3 also drops duplicates.
Generate a fresh one per tap and reuse it when retrying that tap.

Multi-question requests are answered one question at a time. The answers are
submitted to T3 together after the last one. Questions that allow several
choices currently take a single tap.

### `POST /v1/threads`

```json
{"project": "k3x9a2bq", "text": "Fix the flaky login test", "client_msg_id": "..."}
```

Starts a thread in the project root and returns its view. The thread uses your
own T3 settings, as a new thread in the app would: the model and permission
mode come from the project's overrides if it has any, otherwise from the
server-wide defaults (Settings → default model / runtime mode). They're read
fresh for every new thread, so changes made on the desktop apply immediately.
T3 generates the title.

### `GET /v1/health`

`{"ok": true, "list_v": 262}`
