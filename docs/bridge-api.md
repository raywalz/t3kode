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
reply body) to include the running turn's steps and its live activity (`live`, below).
The device's "Show progress while working" setting does this.

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

`update` says what can be updated (also `GET /v1/update`):

```json
{"bridge": "0.1.1", "plugin": "0.1.1", "release": "0.1.2", "self_update": true, "status": "idle"}
```

`bridge` is the bridge's version and `plugin` the version of the plugin it serves; a
device whose plugin is older offers to update it (`GET /v1/plugin`). `release` is a newer
release, or null. With `self_update` false, `manual` says why the bridge can't update
itself and what to do instead. `status` is `idle`, `updating`, `restarting`, or `failed`
(with a `message`).

Like the desktop inbox, the main list leaves out settled threads unless they need you
or are working, and snoozed threads until they wake (at their time, or earlier when they
ask for something or fail, as T3 decides). Each project carries `settled` and `snoozed`
(how many of its threads are) and `more` (active threads beyond `limit`). With
`project=<id>`, the list includes every thread in that project, settled and snoozed ones
too, pinned ones first.

Pinned threads come first, in a group of their own (`"title": "Pinned", "pinned": true,
"id": null`), in the order they were pinned, and not again under their project.

Each row also has `settled`, `pinned`, `snoozed_until` (ISO time while it's asleep, else
null) and `can_mark_unread` (it has a finished run and isn't unread already).

`state` is `needs_you | working | failed | done | idle`. `unread` means the last
run finished after the thread was last opened (on any device). `limit` is per
project. Pass `project` to get one project's full list.

### `POST /v1/threads/<id>/action`

The app's thread menu, from the list's hold menu:

```jsonc
{"action": "pin"}            // or "unpin"
{"action": "snooze", "snooze": "tomorrow"}  // hour | three-hours | evening | tomorrow | next-week
{"action": "unsnooze"}       // wake it now
{"action": "mark_unread"}
{"action": "archive"}        // leaves the list (T3's shell omits archived threads)
{"action": "rename", "title": "Fix the login bug"}
{"action": "auto_title"}     // T3 writes a new title in the background
```

Snooze presets are the app's: in an hour, in three hours, this evening at 18:00 (only
while that's more than an hour away), tomorrow at 9:00, next Monday at 9:00, in the
bridge's local time. A thread waiting for you can't be snoozed, and a working or waiting
one can't be archived (409). Returns `{"ok": true}` once the change shows in T3.

### `GET /v1/threads/wait?v=<v>&timeout=55&limit=8`

Same body as the list, plus `"changed": true|false`.

### `GET /v1/projects`

`{"projects": [{"id", "title"}], "call": true|false}`, for choosing where a new thread goes.
`call`: whether `POST /v1/projects/<id>/call` works (voice replies are set up). The
thread list (`GET /v1/threads`) has the same `call` field.

### `GET /v1/models?thread=<id>` or `?project=<id>`

The models to choose from, as in the desktop's model picker:

```json
{"current": "q3k2m7xa", "current_label": "GPT-6.2",
 "favorites": [{"id": "q3k2m7xa", "label": "GPT-6.2"}, ...],
 "groups": [{"title": "Codex", "models": [{"id": "...", "label": "GPT-6.2-Sol"}]}, ...]}
```

`favorites` are the models starred in T3, in T3's order. `groups` holds every other
model, under its provider's name, leaving out models hidden in T3's settings, legacy
models, and providers that are disabled or failing. A model offered by two providers
(say, a personal and a work account) has the provider in its favorite label. `current` is the
thread's model, or with `project`, the one a new thread there would get (see
`POST /v1/threads`). Model ids are short and stable, like thread ids.

### `POST /v1/threads/<id>/model`

`{"model": "<id from /v1/models>", "client_msg_id": "..."}` switches the thread's model
(and provider, if it differs), as the app's model picker does. The thread's next turn
uses it. Returns the new thread view. Choosing the current model changes nothing.

### `GET /v1/threads/<id>?turns=3&after=<cursor>&before=<cursor>&visit=1&progress=1`

```json
{"id": "jqlcag76", "title": "Fix login", "state": "done", "model": "GPT-6.2",
 "mode": "default", "plan_mode": true,
 "v": 264, "cursor": 1000005,
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
- `entries`: `role` is `you | agent | tool | thought | note | fold | subagent`.
- A finished turn that changed files gets a line under its last reply, like the app's
  Changed files card: "▸ 3 files changed · +120 −30" (from the turn's checkpoint). It
  links to `t3:run:f<run>`; pass `f<run>` in `expand` to list the files with their
  added and removed lines.
- A `subagent` entry is one line per subagent ("Agent: <task> · <state>", with the
  subagent thread's live state). When its thread exists, the entry has `thread` (its id)
  and its `html` links to `t3:thread:<id>`. A subagent that's still working stays
  visible when its turn folds.
- `mode`: `plan` while the thread is in plan mode (the agent proposes a plan instead of
  changing things), otherwise `default`. `plan_mode`: whether the thread's model has a
  plan mode at all (T3 hides its Plan toggle for providers that don't).
- A proposed plan stays visible when its turn folds, as in the app. The message that
  implements one shows as "Implement the plan." rather than repeating the plan.
- `settled`, `pinned`, `snoozed_until` and `can_mark_unread`: as in a list row, for the
  thread menu (hold the title).
- `prs`: the thread's linked pull requests, open ones first, as T3 last read them (it
  checks open ones every few minutes): `{number, repo, host, project, url, title, open,
  status}`, where
  `status` sums one up like the app's PR row: `open · checks pass · needs review ·
  conflicts`, `draft`, `merged`, `closed`, or `not synced` when T3 hasn't read it yet.
  `project`, `repo`, `number` and `host` open its page (`GET /v1/prs/detail`).
- `tasks`: the agent's task list (its to-do tool or plan updates), like the
  app's "Tasks 3/7" banner, or null: `{done, total, live, steps: [{text, status}]}` with
  `status` `pending`, `running` or `completed`. It's the running turn's list (`live`), else
  the latest one while steps are left undone. Step changes count as a change mid-turn.
- `context`: how full the agent's context window is, like the app's context meter, or
  null when T3 has no report: `{used, max, percent, label}`, e.g. label `22% of context
  used (220k of 1m)`; `max` and `percent` are null when the provider doesn't give the
  window size. Mid-turn changes don't count as a change unless `progress` is on.
- `live`: with `progress` on, what the running turn is doing now, like the app's live
  row, or null: `{now, draft_text, draft_html, elapsed}`, each left out when there's
  nothing to show. `now`: the step still running (`Running make`, `Reading a.py`); it's
  left out of `entries` until it finishes. `draft_text`/`draft_html`: the reply it's
  writing so far. `elapsed`: how long the turn has run, in whole minutes (`4m`, `1h 5m`).
  Also with `progress` on, each of the running turn's thoughts is an entry with role
  `thought`: one line, its bold heading or else its first sentence, as the app shows it.
- `queue`: messages waiting for the running turn to end, or null:
  `{items: [{id, text}], held, can_send_now, can_steer}`, oldest first. `held`: Stop
  paused the queue. See `POST /v1/threads/<id>/queue`.
- Your message's citations (text you quoted from a reply, with a comment) show as the
  quote in a block quote, then the comment.
- A subagent thread's view has `parent: {id, title}`; top-level threads have `parent: null`.
- `images`: ids of the images in the shown entries (see `/v1/images`).
- Very long threads, whose full transcript T3 refuses, show their most recent part. A `tool` entry is one line covering
  a run of consecutive tool calls. `note` covers notifications, errors, questions and
  their answers, and messages sent by agents rather than you.
- Default window: everything from the `turns`-th most recent message of yours.
  `more_before` says whether older entries exist. Fetch them with `before=<o of the first entry>`.
  Threads too big for T3's whole transcript load their history from T3 a page at a
  time as you go back, so going back through those can take a moment longer.
- `after=<cursor>` returns only entries newer than a cursor (`cursor` is the `o` of the last entry).
- `visit=1` marks the thread read in T3 (pass it when the user opens the thread).
- When the thread has just finished, the call waits up to 12 s for suggestions
  already being generated, so one redraw shows both the reply and the buttons.
- Suggestions come from the agent itself when its last reply ends with
  `<!-- replies: ["Go", "Wait, run the tests first"] -->`. The bridge strips that
  comment from the entry, and T3's desktop doesn't show HTML comments either.
  Only a reply without one goes to the `SUGGEST_*` model, if it's configured.

**`replies`**: buttons in display order. Send back `id` unchanged.

| state | buttons |
|---|---|
| `needs_you`, approval | Allow · Allow for session · Decline |
| `needs_you`, question | one per option, then Stop |
| `working` | Stop |
| `done` / `failed` / `idle` | Implement plan (if a plan is waiting), 0–4 suggestions, then Continue · Summarize where we are |

**Implement plan** (`kind: "plan"`, id `p:<plan>`) appears while T3 marks the thread's
latest proposed plan as actionable. It does what the app's Implement button does: leaves
plan mode, sends `PLEASE IMPLEMENT THIS PLAN:` plus the plan, and links the turn to the
plan (`sourcePlanRef`) so T3 marks it implemented. A tap on a plan that's already been
implemented or replaced gets a 409.

**`pending`**: what's being asked, when `state` is `needs_you`:

```jsonc
{"kind": "approval", "header": "File change", "text": "/tmp/x.txt", "note": "Path is outside allowed working directories"}
{"kind": "question", "header": "Color", "text": "Which color?", "step": "1/2",
 "options": [{"label": "Red", "description": "Choose red."}, ...]}
{"kind": "secret", "header": "API key", "text": "<the agent's reason>", "link": "https://…",
 "placeholder": null, "note": "Stored securely, never shown to the agent"}
```

A `secret` request comes from T3's request_secret tool. Its buttons are **Enter secret**
(`kind: "secret"`: ask for the value in a masked box and send it to `/v1/threads/<id>/secret`)
and **Decline** (an ordinary reply). While one is pending, `typed` is `none`, so typed text
can't end up in the chat.

A card that asks for several values (say, an email and a password) also has `fields`, in
the order to ask for them:

```json
"fields": [{"name": "email", "label": "Email", "placeholder": "you@example.com", "secret": false},
           {"name": "password", "label": "Password", "placeholder": null, "secret": true}]
```

`secret: false` means the value can be shown while it's typed. Answer such a card with
`values`, not `secret`.

`note` may also say the request has to be answered on another device (for
example, one that can't be resumed). Then there are no buttons.

**`typed`**: what text typed into the box will do:
- `message`: send it as a normal message
- `answer`: answer the current question with it (a custom answer)
- `none`: nothing can be typed (a question that only accepts its options, or a pending secret)

**`call`**: `true` when the bridge can take a spoken reply for this thread by phone
(voice replies are set up and `typed` isn't `none`). See `POST /v1/threads/<id>/call`.

### `GET /v1/threads/<id>/wait?v=<v>&timeout=55&progress=1`

Same body as the thread view, plus `"changed": true|false`.

### `POST /v1/threads/<id>/reply`

```jsonc
{"reply": "c:continue", "client_msg_id": "<unique per tap>"}
{"text": "Use the staging database", "client_msg_id": "..."}
{"text": "Plan the migration first", "mode": "plan", "client_msg_id": "..."}
```

With typed text, `mode` (`plan` or `default`) switches plan mode before sending, as
sending with the app's Plan toggle changed does. Leave it out to keep the thread's mode.

While a turn runs, `deliver` says what typed text does: `queue` waits for the turn to
end (the app's default), `steer` sends it into the running turn (T3 queues it anyway
when the turn can't take it). Left out, it steers.

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

### `POST /v1/threads/<id>/queue`

```jsonc
{"action": "steer", "item": "<queued message id>"}
{"action": "send" | "remove", "item": "..."}
{"action": "resume"}
```

What the app's queue offers for the view's `queue` items: `send` a queued message now
(only when no turn runs, `can_send_now`), `steer` the running turn with it
(`can_steer`), `remove` it, or `resume` a queue that Stop paused (`held`). Returns
`{"ok": true}` once T3 shows the change; 409 when the message isn't queued any more
or the action isn't possible now.

### `POST /v1/threads/<id>/settle`

`{"settled": true}` settles the thread, like Settle in the app: it leaves the main list.
`{"settled": false}` unsettles it. Returns `{"ok": true, "settled": …}` once the change
shows in T3.

### `POST /v1/threads/<id>/secret`

`{"secret": "<value>"}` answers the thread's pending secret request; `{"decline": true}`
declines it. A request with `fields` takes `{"values": {"<field name>": "<value>", ...}}`
instead, with exactly one non-empty value per field (400 otherwise). The bridge passes the value straight to T3 (`secrets.answerRequest`), which
stores it the way the desktop does; the thread only learns "saved". The bridge doesn't
log or keep the value, and its error messages never include it. Not retried: send it once.
Returns the new thread view.

### `POST /v1/threads`

```json
{"project": "k3x9a2bq", "text": "Fix the flaky login test", "client_msg_id": "..."}
```

Optional `"model": "<id from /v1/models>"` picks the model, and `"mode": "plan"` starts
the thread in plan mode; the rest still comes from your settings.

Starts a thread and returns its view. The thread uses your own T3 settings, as a
new thread in the app would: the model, permission mode and workspace come from
the project's overrides if it has any, otherwise from the server-wide defaults
(Settings → default model / runtime mode / new threads in). With no default model set
anywhere, as on a new install, it's the first ready agent's default model, as in the app. With the worktree
default, the thread gets a new worktree from the branch the project's checkout
is on, fetched from the remote first if "start from origin" is on. Otherwise it
runs in the project root. Settings are read fresh for every new thread, so
changes made on the desktop apply immediately.
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

### `GET /v1/threads/<id>/browser`

The thread's preview browser tabs and the simulators and emulators open in it, view only:

```json
{"tabs": [{"id": "tab_8_…", "title": "E Ink - Wikipedia", "url": "https://…",
           "status": "ok", "viewable": true}],
 "devices": [{"id": "6F1C…", "hostId": "local", "name": "iPhone 16 Pro", "platform": "ios",
              "kind": "iOS simulator", "booted": true}]}
```

`status` is `ok`, `loading` or `failed`. A blank tab has `"title": "Blank tab"` and an empty
`url`. `viewable` is false for a tab open in the desktop app's own window: T3 can only share
tabs that run on its server, which is where agents' tabs run.

### `GET /v1/threads/<id>/browser/snapshot?tab=<tab id>&w=<pixels>`

What the tab shows right now: an image at most `w` pixels wide (grayscale PNG with
ImageMagick, else T3's JPEG). Sent with `cache-control: no-store`; fetch again to refresh.
Taking one never takes control of the tab from the agent or another viewer.
Errors: 404 if the tab closed, 409 if it isn't `viewable`, 503 if T3's browser can't start.

`devices` lists only the devices open in this thread. It is empty if T3 can't list
devices; the tabs are still listed. `platform` is `ios` or `android`.

### `GET /v1/threads/<id>/device/snapshot?device=<device id>&w=<pixels>`

What a simulator or emulator open in the thread shows right now, as for a tab (grayscale
PNG with ImageMagick, else T3's PNG), with `cache-control: no-store`. Nothing is sent to
the device. Errors: 404 if the device isn't open in this thread, 503 if T3's device hub
isn't running, 502 if T3 can't list devices or take the screenshot.

### `POST /v1/threads/<id>/call`

Ring the user's phone (Twilio) to take a spoken reply for this thread. Not idempotent:
each request places a call, so don't retry it. 404 when voice replies are off.

```json
{"id": "9f2c4a…", "thread": "a1b2c3d4", "status": "ringing", "text": "", "parts": 0,
 "error": null, "v": 1}
```

`status` goes `ringing` → `listening` (→ `sending`, or `ending` after a hang-up from the
device), then ends in one of:
- `draft`: the call ended with something said. Put `text` in the reply box to check and
  send. `error` is set if pressing 1 on the phone couldn't send it.
- `sent`: the user pressed 1 and the bridge sent `text` as a typed reply (queued if a
  turn is running).
- `empty`: nothing was said; `no-answer`; `failed` (`error` says why); `cancelled`.

`text` is everything heard so far, and `parts` how many stretches of speech it came in.

### `POST /v1/projects/<id>/call`

The same, for the first message of a new thread in this project. Body:
`{"model": "<id from /v1/models>", "mode": "plan"}`, both optional, as for `POST /v1/threads`.
Pressing 1 starts the thread; the call's `thread` is then the new thread's id. The call
also has `"project": "<id>"` (null for a reply call).

### `GET /v1/calls/<call id>/wait?v=<v>&timeout=55`

The call as above, once its `v` differs from the one given (or after `timeout`).

### `POST /v1/calls/<call id>/cancel`

Hang up a call that's still going, the same as hanging up the phone: what was said is
kept. Returns the call as `ending` while Twilio delivers the last words (a second or two;
keep waiting on it), which then turns `draft` (or `empty`). A call not answered yet is
`cancelled` at once.

### `GET /v1/prs?scope=mine|all`

Open pull requests in the projects' repositories, as T3's Pull requests page lists them
(T3 asks the host, so this takes a few seconds, and everyone's can take 10–30 s).
`mine` (the default): `Review requested`, then `Yours`; `all`: everyone's, grouped by
project, the project with the latest activity first. Within a group, the latest updated
first.

```json
{"scope": "mine", "truncated": false,
 "groups": [{"title": "Yours", "prs": [{"project": "ab12cd34", "project_title": "app",
   "repo": "me/app", "host": "github.com", "number": 12, "title": "Fix the thing",
   "status": "open · checks pass · needs review", "draft": false, "author": "me",
   "updated": "2026-01-02T00:00:00Z"}]}],
 "errors": [{"project": "notes", "message": "me/notes could not be read. …"}]}
```

Empty groups are left out. `errors`: projects whose repository T3 couldn't read.
`truncated`: there are more than T3 listed. 502 with T3's reason when it can't list any
(for example, no credentials for the host).

### `GET /v1/prs/detail?project=<id>&repo=<owner/name>&number=<n>&host=<host>`

One pull request's page, from a list row or a thread's `prs`: `{title, number, repo, url,
status, html}`. `title` is `#12 me/app`; `status` as in a list row, its review part
worked out from the reviews. `html` holds the title, status, author and dates, branches,
size, then sections for linked threads (`t3:thread:<id>` links), checks (failures first),
reviewers, description, conversation (bots' comments one line each), review comments
(resolved and outdated ones one line each), commits, and changed files. `host` is
optional. 404 for an unknown project, 502 if T3 can't read the PR. If the comments,
diff or linked threads can't be read, the page leaves those sections out.

### `GET /v1/usage?period=today|week|month`

Usage stats from T3's `server.getUsageSummary` (the scan behind its Usage page), for
today, the last 7 days, or this month so far, in the bridge machine's time zone:
`{period, title, html}`. `html` holds the total cost, tokens and sessions, the token
mix (input, cache read and write, output), then tables by model (cheap ones folded into
one row), by provider when there's more than one, and by hour (today) or day, the latest
first, then notes on unpriced records and histories T3 couldn't read. 403 when the
bridge's T3 token lacks `diagnostics:read`, 502 when T3 can't read usage.

### `POST /v1/update`

`{"action": "update"}` updates the bridge to `release` in the background and returns the
`update` object with `status: "updating"`. The bridge fetches the release tag from its git
remote, runs its tests in a separate checkout, fast-forwards only if they pass, then
restarts (the same process, re-executed). Poll `GET /v1/update` (requests fail briefly
during the restart) until `bridge` is the new version or `status` is `failed`; then update
the plugin. 409 when there's no newer release or the bridge can't update itself.
`{"action": "check"}` looks for a release now.

### `GET /v1/plugin` and `GET /v1/plugin/files/<name>`

The plugin the bridge has, for the device to update itself from:
`{"version": "0.1.1", "files": [{"path": "main.lua", "size": 74291, "sha256": "…"}, ...]}`,
then each file's bytes. Only the plugin's own files are served. The device downloads them
into a folder next to its own, checks every size and hash, then swaps the folders and
reloads; any failure leaves the running plugin as it was.

### `GET /v1/health`

`{"ok": true, "list_v": 262}`
