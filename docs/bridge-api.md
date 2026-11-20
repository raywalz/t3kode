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

A thread view carries a version `v`: a fingerprint of what the device would draw
(title, state, entries, the pending request, buttons). A wait call returns as
soon as the view would look different from the `v` you pass, or after `timeout`
seconds (max 120) with `changed: false`. T3 updates threads for things the view
never shows (reasoning, replies still being written), and those don't count as
changes, so a device never redraws an identical screen.

By default, new steps in a turn that's still running don't count either: the view
changes when the turn ends, a question appears, or suggestions arrive. Pass
`progress=1` to the thread view and the thread wait (and `"progress": true` in the
reply body) to include the running turn's steps. The device's "Show progress while
working" setting does this.

The thread list has its own counter `v` with the same wait semantics.

To catch up after being offline: call the wait endpoint with the last `v`. It
returns immediately if anything visible changed.

## Endpoints

### `GET /v1/threads?limit=8&project=<id>`

Threads grouped by project, most recently active project first. Threads an agent
started under another thread (subagents) aren't listed; they're reached through
their parent, as in the desktop sidebar. Forks you made are listed normally. A
parent's row carries `agents: {total, needs_you, working}` so the list can show
when one of its subagents needs attention.

```json
{"v": 262, "projects": [
  {"id": "k3x9a2bq", "title": "my-app", "updated": "2026-11-19T22:59:01.120Z", "total": 20,
   "threads": [
     {"id": "jqlcag76", "title": "Fix login", "state": "needs_you", "unread": true,
      "updated": "...", "preview": "I need your OK to run the migration."}]}]}
```

An optional `notice` (string) reports a problem with the bridge itself, such as its T3
token expiring within a week without a T3_TOKEN_COMMAND to renew it. Show it prominently.

Like the desktop inbox, the main list leaves out settled threads unless they need you
or are working. Each project carries `settled` (how many of its threads are settled)
and `more` (active threads beyond `limit`). With `project=<id>`, the list includes every
thread in that project, settled ones too, and each row has `settled: true|false`.

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

- Finished turns fold the way T3's web UI does: your message, then one `fold` entry
  ("▸ Worked for 1m 12s"), then the agent's final reply. Notifications and answered
  questions stay visible. The turn that's still running is never folded. The fold's
  `html` is a link to `t3:run:<run>`. Pass `expand=<run>,<run>` (in the reply body:
  `"expand": [...]`) to show those turns' steps, with the fold shown open ("▾").
- `entries`: `role` is `you | agent | tool | note | fold | subagent`.
- A `subagent` entry is one line per subagent ("Agent: <task> · <state>", with the
  subagent thread's live state). When its thread exists, the entry has `thread` (its id)
  and its `html` links to `t3:thread:<id>`. A subagent that's still working stays
  visible when its turn folds.
- A subagent thread's view has `parent: {id, title}`; top-level threads have `parent: null`.
- `images`: ids of the images in the shown entries (see `/v1/images`).
- Very long threads, whose full transcript T3 refuses, show their most recent part. A `tool` entry is one line covering
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
submitted to T3 together after the last one. A question that allows several
choices shows its options as toggles (`kind: "toggle"`, labelled "○ X" / "● X")
plus a `kind: "submit"` Done button with `enabled: false` until something is ticked.
Ticking is handled by the bridge alone, so it answers instantly. Typed text on such
a question is added to whatever is ticked.

### `POST /v1/threads/<id>/settle`

`{"settled": true}` settles the thread, like Settle in the app: it leaves the main list.
`{"settled": false}` unsettles it. Returns `{"ok": true, "settled": …}` once the change
shows in T3.

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

### `GET /v1/images/<id>?w=<pixels>`

The image bytes for an `id` from a thread view's `images` list. Only images that
appeared in a view the bridge rendered can be fetched. The bridge gets them through
T3's signed asset URLs (`assets.createUrl`), so T3's access rules apply: files an agent
reply refers to need the bridge token's `filesystem:read` permission, while message
attachments need only `orchestration:read`. Images on the web aren't fetched.

With ImageMagick installed on the bridge machine, images are shrunk to at most `w`
pixels wide and turned grayscale: PNG for screenshots, JPEG for photos. Without it, the
original bytes are sent.

Images a tool returned (browser and device screenshots, images an agent read) appear
in that step's `tool` line as links, `<a href="t3:img:<id>">screenshot ›</a>`, fetched
from the same endpoint when tapped. They aren't in `images`, so they're never prefetched.

In entry `html`, an image is `<a href="t3:img:<id>"><img src="t3img:<id>" alt="…"/></a>`.
The device swaps `t3img:<id>` for its downloaded copy, or replaces the whole link with a
placeholder.

### `GET /v1/health`

`{"ok": true, "list_v": 262}`
