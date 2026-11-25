"""Bridge state and logic: what the device sees, and what its taps do in T3.

The HTTP layer (server.py) is a thin wrapper around `Bridge`. Everything here
works against any object with T3Client's methods, so tests can pass a fake.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import prs, render
from .images import Images
from .t3client import (PREVIEW_HOST_SETUP, PREVIEW_TAB_GONE, T3Error, T3RpcError, T3SocketError,
                       pending_requests_from)

log = logging.getLogger("t3bridge")

# Fixed shortcuts. Labels are what the device shows; texts are what gets sent.
SHORTCUTS = {
    "continue": ("Continue", "Continue."),
    "summarize": (
        "Summarize where we are",
        "Summarize where we are: what's done, what's left, and anything you need from me. "
        "Keep it short.",
    ),
}
APPROVAL_BUTTONS = [("accept", "Allow"), ("acceptForSession", "Allow for session"),
                    ("decline", "Decline")]

SUGGESTION_WAIT = 12.0  # seconds a thread view waits for suggestions already being generated
SUGGESTION_RETRY = 30.0  # seconds before a failed generation is tried again
DEVICE_ACTIVE_WINDOW = 120.0  # poll fast for this long after the last device request
CONFIG_TTL = 30.0  # seconds T3's server config (models, settings) is reused

_ID_NAMESPACE = uuid.UUID("6b0f7c52-7d0e-4c55-9a43-2a3c1f0b9e11")


class BridgeError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _stable_id(client_msg_id: str | None, salt: str) -> str | None:
    """A T3 id derived from the device's message id, so a retried request maps to the
    same T3 command and T3 drops the duplicate."""
    if not client_msg_id:
        return None
    return str(uuid.uuid5(_ID_NAMESPACE, f"{salt}:{client_msg_id}"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dedupe(rows: list[dict]) -> list[dict]:
    """Rows in order, each item once: where pages overlap, the newer copy wins."""
    seen, out = set(), []
    for r in reversed(rows):
        if r["item"]["id"] not in seen:
            seen.add(r["item"]["id"])
            out.append(r)
    return out[::-1]


def _signature(t: dict) -> tuple:
    """Changes whenever anything about the thread changes (cache key)."""
    pending = t.get("pendingRuntimeRequest") or {}
    return (t.get("status"), pending.get("id"), t.get("itemCount"), t.get("updatedAt"),
            t.get("title"), t.get("lastVisitedAt"))


def _wake_key(t: dict) -> tuple:
    """Changes only when the device should redraw. While the agent works, new tool calls
    don't count: no live streaming on e-ink."""
    state = render.thread_state(t)
    pending = (t.get("pendingRuntimeRequest") or {}).get("id")
    model = t.get("modelSelection") or {}
    return (state, pending, t.get("title"), render.is_unread(t), t.get("settledOverride"),
            None if state == "working" else t.get("itemCount"),
            model.get("instanceId"), model.get("model"),
            t.get("interactionMode"), bool(t.get("hasActionableProposedPlan")),
            bool(t.get("pinnedAt")), _snoozed(t),
            tuple((p["number"], p["status"]) for p in render.pull_requests(t)))


def _content_version(body: dict, progress: bool) -> str:
    """Fingerprint of what the device would draw. Without `progress`, a working thread's
    steps are left out, so new steps don't count as a change until the turn ends."""
    shown = {k: body[k] for k in ("title", "state", "model", "mode", "more_before", "pending",
                                  "replies", "typed")}
    shown["prs"] = [(p["number"], p["status"], p["title"]) for p in body["prs"]]
    shown["tasks"] = body["tasks"]
    shown["queue"] = body["queue"]
    if progress or body["state"] != "working":
        shown["context"] = (body["context"] or {}).get("label")
        shown["entries"] = [(e["role"], e["text"], e.get("expanded")) for e in body["entries"]]
    raw = json.dumps(shown, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


_SECRET_REASONS = {
    "not_found": "This secret request no longer exists.",
    "already_answered": "This secret request was already answered.",
    "agent_stopped": "The agent that asked has stopped, so this secret can't be used.",
    "store_failed": "T3 couldn't store the secret.",
}


def _secret_error(e: Exception) -> str:
    """A safe message for a failed answer: T3's reason, nothing from the request body."""
    text = str(e)
    for reason, message in _SECRET_REASONS.items():
        if f'"{reason}"' in text:
            return message
    return "T3 didn't accept the answer."


def _is_settled(t: dict) -> bool:
    return t.get("settledOverride") == "settled"


def _snoozed(t: dict, now: str | None = None) -> bool:
    """Snoozed and still asleep, as T3 decides (threadSettled.ts effectiveSnoozed): the
    wake time hasn't passed, and the thread hasn't raised its hand since (a request
    for you, or a run that failed after it was snoozed)."""
    until = t.get("snoozedUntil")
    if not until or until <= (now or _now_iso()):
        return False
    state = render.thread_state(t)
    if state == "needs_you":
        return False
    if state == "failed":
        failed_at = t.get("latestRunCompletedAt") or t.get("updatedAt")
        if not t.get("snoozedAt") or (failed_at and failed_at > t["snoozedAt"]):
            return False
    return True


def snooze_until(preset: str, now: datetime | None = None) -> str:
    """The wake time for one of the app's snooze presets (threadSettled.ts
    resolveSnoozePresets), in the bridge's local time: in an hour, in three hours, this
    evening (18:00), tomorrow (9:00), next Monday (9:00)."""
    now = now or datetime.now().astimezone()
    at = lambda d, h: d.replace(hour=h, minute=0, second=0, microsecond=0)
    if preset == "hour":
        wake = now + timedelta(hours=1)
    elif preset == "three-hours":
        wake = now + timedelta(hours=3)
    elif preset == "evening":
        wake = at(now, 18)
        if wake - now <= timedelta(hours=1):
            raise BridgeError(400, "too late for this evening")
    elif preset == "tomorrow":
        wake = at(now + timedelta(days=1), 9)
    elif preset == "next-week":
        wake = at(now + timedelta(days=(7 - now.weekday()) or 7), 9)
    else:
        raise BridgeError(400, "unknown snooze preset")
    return wake.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _shelved(t: dict) -> bool:
    """Settled and not asking for anything: out of the main list, as in the desktop inbox."""
    return _is_settled(t) and render.thread_state(t) not in ("needs_you", "working")


def first_ready_model(providers: list[dict]) -> dict | None:
    """The model T3's app starts a new thread with when no default is set: the first
    healthy provider's default model (web providerInstances.ts,
    resolveProjectDefaultProviderModelSelection and getDefaultProviderInstanceModel)."""
    usable = [p for p in providers if p.get("enabled", True) and p.get("installed", True)
              and p.get("models")]
    for wanted in ("ready", "warning"):
        for p in usable:
            if p.get("status") != wanted:
                continue
            models = p["models"]
            m = (next((m for m in models if m.get("isDefault") and not m.get("isCustom")), None)
                 or next((m for m in models if not m.get("isCustom")), None) or models[0])
            return {"instanceId": p.get("instanceId") or p.get("provider"), "model": m["slug"]}
    return None


def _is_subagent(t: dict) -> bool:
    """Threads an agent started under another thread. They live under their parent,
    as in the desktop sidebar. Forks the user made are ordinary threads."""
    lineage = t.get("lineage") or {}
    if not lineage.get("parentThreadId"):
        return False
    return lineage.get("relationshipToParent") == "subagent" or t.get("createdBy") == "agent"


class Bridge:
    def __init__(self, client, suggester=None, *, poll_fast: float = 2.0, poll_slow: float = 20.0,
                 token_keeper=None, images: Images | None = None, updater=None):
        self.client = client
        self.updater = updater  # update.Updater: release checks and self-updates
        self.suggester = suggester
        self.token_keeper = token_keeper
        self.images = images or Images(client)
        self.poll_fast = poll_fast
        self.poll_slow = poll_slow
        self.cond = threading.Condition()
        self.seq = 0  # bumps on every change the device could care about
        self.list_v = 0
        self.shell: dict | None = None
        self.threads: dict[str, dict] = {}  # long id -> shell thread
        self.thread_pv: dict[str, int] = {}  # bumps on any update; wakes wait_view to re-check
        self.sigs: dict[str, tuple] = {}
        self.wake_keys: dict[str, tuple] = {}
        self.by_short: dict[str, str] = {}  # short id -> long id (threads and projects)
        self.entries: dict[str, tuple] = {}  # long id -> (sig, items, unfolded entries)
        self.snapshots: dict[str, tuple] = {}  # long id -> (sig, context usage, queue)
        # Older pages of threads too big for a whole transcript, fetched as the device
        # pages back: long id -> {"rows": [...] oldest first, "cursor": str, "more": bool}
        self.history: dict[str, dict] = {}
        self.pending: dict[str, tuple[tuple, list[dict]]] = {}
        self.suggestions: dict[str, dict] = {}  # long id -> {cursor, status, items}
        self.partial_answers: dict[str, dict] = {}  # request id -> {question id: answer}
        self.multi_picks: dict[tuple, list[str]] = {}  # (request id, question id) -> ticked labels
        self.done_requests: OrderedDict[str, dict] = OrderedDict()  # client_msg_id -> response
        self.last_device_at = 0.0
        self._config: tuple[float, dict] | None = None  # (fetched at, server config)
        self.stop_capability = True
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="suggest")
        # Fetches transcripts in the background for list previews (T3's own preview
        # field, latestVisibleMessage, is usually null).
        self.prefetch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prefetch")
        self._stop = threading.Event()

    # ---- polling ------------------------------------------------------------

    def start(self) -> None:
        env = self.client.environment()
        self.stop_capability = bool((env.get("capabilities") or {}).get("threadStop", False))
        log.info("T3 %s, protocol %s", env.get("serverVersion"), env.get("orchestrationProtocolVersion"))
        if self.token_keeper:
            self.token_keeper.check()
        self.refresh()
        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.prefetch_pool.shutdown(wait=False, cancel_futures=True)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            fast = (time.monotonic() - self.last_device_at < DEVICE_ACTIVE_WINDOW
                    or any(render.thread_state(t) == "working" for t in self.threads.values()))
            self._stop.wait(self.poll_fast if fast else self.poll_slow)
            try:
                if self.token_keeper and self.token_keeper.due():
                    self.token_keeper.check()
                if self.updater and self.updater.due():
                    self.updater.last_check = time.monotonic()  # one check at a time
                    threading.Thread(target=self.updater.check, name="release-check", daemon=True).start()
                self.refresh()
            except Exception as e:  # keep polling through T3 restarts
                log.warning("refresh failed: %s", e)
                if getattr(e, "status", None) == 401 and self.token_keeper:
                    self.token_keeper.check(rejected=True)

    def refresh(self) -> None:
        shell = self.client.shell_snapshot()
        finished = []
        first = self.shell is None
        touched = []
        with self.cond:
            changed = progressed = False
            new_threads = {t["id"]: t for t in shell["threads"] if not t.get("deletedAt")}
            for tid, t in new_threads.items():
                sig = _signature(t)
                if self.sigs.get(tid) != sig:
                    self.sigs[tid] = sig
                    self.seq += 1
                    self.thread_pv[tid] = self.seq
                    # A subagent's change can change its parent's view (its line shows its state).
                    parent = (t.get("lineage") or {}).get("parentThreadId")
                    if parent:
                        self.thread_pv[parent] = self.seq
                    progressed = True
                key = _wake_key(t)
                if self.wake_keys.get(tid) != key:
                    old = self.threads.get(tid)
                    self._bump(tid)
                    self.wake_keys[tid] = key
                    changed = True
                    touched.append(tid)
                    if (old is not None and render.thread_state(old) == "working"
                            and render.thread_state(t) in ("done", "failed")):
                        finished.append(tid)
            for tid in set(self.threads) - set(new_threads):
                self._bump(tid)
                self.wake_keys.pop(tid, None)
                self.sigs.pop(tid, None)
                changed = True
            if changed or self.shell is None:
                self.list_v = self.seq
            self.shell = shell
            self.threads = new_threads
            for t in new_threads.values():
                self.by_short[render.short_id(t["id"])] = t["id"]
            for p in shell["projects"]:
                self.by_short[render.short_id(p["id"])] = p["id"]
            if changed or progressed:
                self.cond.notify_all()
        for tid in finished:
            if self._wants_suggestions(tid):
                self._schedule_suggestions(tid)
        self._prefetch(self._recent_per_project(3) if first else touched)

    def _bump(self, tid: str) -> None:
        """Something about the thread changed (caller holds self.cond)."""
        self.seq += 1
        self.thread_pv[tid] = self.seq

    def _top_level(self, t: dict) -> bool:
        return not _is_subagent(t)

    def _children(self, tid: str) -> list[dict]:
        return [c for c in self.threads.values()
                if (c.get("lineage") or {}).get("parentThreadId") == tid and _is_subagent(c)]

    def _child_info(self, long_id: str):
        t = self.threads.get(long_id)
        return (render.short_id(long_id), render.thread_state(t)) if t else None

    def _recent_per_project(self, n: int) -> list[str]:
        groups: dict = {}
        for t in self.threads.values():
            if self._top_level(t):
                groups.setdefault(t.get("projectId"), []).append(t)
        out = []
        for threads in groups.values():
            threads.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
            out += [t["id"] for t in threads[:n]]
        return out

    def _prefetch(self, tids: list[str]) -> None:
        for tid in tids:
            t = self.threads.get(tid)
            if t and self._top_level(t) and render.thread_state(t) != "working":
                self.prefetch_pool.submit(self._prefetch_one, tid)

    def _prefetch_one(self, tid: str) -> None:
        t = self.threads.get(tid)
        if t is None:
            return
        try:
            self._entries(t)
        except Exception as e:
            log.debug("prefetch %s failed: %s", tid, e)

    # ---- lookups ------------------------------------------------------------

    def _thread(self, short: str) -> dict:
        tid = self.by_short.get(short)
        t = self.threads.get(tid) if tid else None
        if t is None:
            raise BridgeError(404, "no such thread")
        return t

    def _load(self, t: dict) -> tuple[list[dict], list[dict]]:
        """(raw turn items, unfolded entries), cached until the thread changes."""
        sig = _signature(t)
        cached = self.entries.get(t["id"])
        if cached and cached[0] == sig:
            return cached[1], cached[2]
        try:
            rows = self.client.thread_transcript(t["id"])["items"]
        except T3Error as e:
            # T3 refuses whole transcripts past a size limit; show the recent part instead.
            if e.status != 400 or "too_large" not in str(e):
                raise
            rows = self._with_history(t["id"], self.client.thread_bounded(t["id"]))
        else:
            self.history.pop(t["id"], None)
        return self._store(t["id"], sig, rows)

    def _store(self, tid: str, sig: tuple, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        # Keep each row's visibility: an inherited request can only be answered where it was asked.
        items = [dict(e["item"], _visibility=e.get("visibility")) for e in rows]
        entries = render.render_items(items)
        self.entries[tid] = (sig, items, entries)
        return items, entries

    def _with_history(self, tid: str, bounded: dict) -> list[dict]:
        """The bounded snapshot's recent rows, after the older pages already fetched."""
        recent = bounded["projection"]["visibleTurnItems"]
        h = self.history.get(tid)
        if h and h["rows"]:
            # The recent part may have moved on since; fetch what fell between.
            known = {r["item"]["id"] for r in h["rows"]}
            gap, cursor = [], bounded.get("historyCursor")
            for _ in range(5):
                if any(r["item"]["id"] in known for r in recent + gap) or not cursor:
                    break
                page = self.client.thread_history(tid, cursor)
                gap, cursor = page["items"] + gap, page.get("nextCursor")
            else:
                h = None  # too far behind: start over from the recent part
            if h:
                h["tail"] = gap + recent
                return _dedupe(h["rows"] + h["tail"])
        self.history[tid] = {"rows": [], "tail": recent, "cursor": bounded.get("historyCursor"),
                             "more": bool(bounded.get("hasMoreHistory") and bounded.get("historyCursor"))}
        return recent

    def _more_history(self, tid: str) -> bool:
        h = self.history.get(tid)
        return bool(h and h["more"])

    def _load_older(self, t: dict) -> bool:
        """Fetch the next older page of a big thread's history. False if there's none."""
        h = self.history.get(t["id"])
        if not h or not h["more"]:
            return False
        try:
            page = self.client.thread_history(t["id"], h["cursor"])
        except T3Error as e:
            log.warning("history page failed: %s", e)
            h["more"] = False
            return False
        h["rows"] = page["items"] + h["rows"]
        h["cursor"] = page.get("nextCursor")
        h["more"] = bool(page.get("hasMoreHistory") and h["cursor"])
        cached = self.entries.get(t["id"])
        if cached:  # the same snapshot, with the older page in front
            self._store(t["id"], cached[0], _dedupe(h["rows"] + h["tail"]))
        return True

    def _snapshot_info(self, t: dict) -> tuple[dict | None, dict | None]:
        """(context window usage, queued messages) from T3's bounded snapshot, which is
        quick; cached until the thread changes. (None, None) if it fails."""
        sig = _signature(t)
        cached = self.snapshots.get(t["id"])
        if cached and cached[0] == sig:
            return cached[1], cached[2]
        try:
            proj = self.client.thread_bounded(t["id"])["projection"]
            info = (render.context_usage(proj), render.queue_state(proj))
        except Exception as e:
            log.warning("thread snapshot: %s", e)
            info = (None, None)
        self.snapshots[t["id"]] = (sig, *info)
        return info

    def _entries(self, t: dict) -> list[dict]:
        """Every step, unfolded: for suggestions and previews."""
        return self._load(t)[1]

    def _pending(self, t: dict) -> list[dict]:
        if not t.get("pendingRuntimeRequest"):
            return []
        sig = _signature(t)
        cached = self.pending.get(t["id"])
        if cached and cached[0] == sig:
            return cached[1]
        result = pending_requests_from(self.client.thread_snapshot(t["id"])["projection"])
        self.pending[t["id"]] = (sig, result)
        return result

    # ---- thread list --------------------------------------------------------

    def thread_list(self, limit: int = 8, project: str | None = None) -> dict:
        self.last_device_at = time.monotonic()
        shell = self.shell or {"threads": [], "projects": []}
        projects = {p["id"]: p for p in shell["projects"]}
        now = _now_iso()
        groups: dict[str | None, list[dict]] = {}
        pinned = []
        for t in self.threads.values():
            if _is_subagent(t):
                continue  # reached through their parent thread
            if t.get("pinnedAt") and project is None:
                pinned.append(t)  # in their own group at the top, as in the app's sidebar
                continue
            groups.setdefault(t.get("projectId"), []).append(t)
        if project is not None:
            pid = self.by_short.get(project)
            groups = {pid: groups.get(pid, [])}
        out = []
        for pid, threads in groups.items():
            threads.sort(key=lambda t: (bool(t.get("pinnedAt")), t.get("updatedAt") or ""), reverse=True)
            p = projects.get(pid) if pid else None
            # Like the desktop inbox: settled and snoozed threads drop out of the main list
            # (unless they need you or are working); one project's own list shows everything.
            active = threads if project is not None else [
                t for t in threads if not _shelved(t) and not _snoozed(t, now)]
            settled = sum(1 for t in threads if _is_settled(t))
            out.append({
                "id": render.short_id(pid) if pid else None,
                "title": (p or {}).get("title") or "No project",
                "updated": threads[0].get("updatedAt") if threads else None,
                "active_updated": active[0].get("updatedAt") if active else None,
                "total": len(threads),
                "settled": settled,
                "snoozed": sum(1 for t in threads if _snoozed(t, now) and not _shelved(t)),
                "more": max(0, len(active) - limit),
                "threads": [self._thread_row(t, now) for t in active[:limit]],
            })
        # Projects with something active first, most recent first; settled-only ones after.
        out.sort(key=lambda g: (g["active_updated"] is not None, g["active_updated"] or g["updated"] or ""),
                 reverse=True)
        if pinned:
            # Pinned threads stay in the order they were pinned, as in the app.
            pinned.sort(key=lambda t: (t.get("pinOrderKey") or "", t.get("pinnedAt") or ""))
            out.insert(0, {"id": None, "title": "Pinned", "pinned": True, "updated": None,
                           "active_updated": None, "total": len(pinned), "settled": 0, "snoozed": 0,
                           "more": 0, "threads": [self._thread_row(t, now) for t in pinned]})
        body = {"v": self.list_v, "projects": out}
        notice = self.token_keeper.notice() if self.token_keeper else None
        if notice:
            body["notice"] = notice
        if self.updater:
            body["update"] = self.updater.info()
        return body

    def _thread_row(self, t: dict, now: str | None = None) -> dict:
        cached = self.entries.get(t["id"])
        lvm = (t.get("latestVisibleMessage") or {}).get("text")
        if lvm:
            lvm = " ".join(render.split_replies(lvm)[0].split())[:80]
        prev = render.preview(cached[2]) if cached else (lvm or "")
        row = {"id": render.short_id(t["id"]), "title": t.get("title") or "Untitled",
               "state": render.thread_state(t), "unread": render.is_unread(t),
               "updated": t.get("updatedAt"), "preview": prev, "settled": _is_settled(t),
               "pinned": bool(t.get("pinnedAt")),
               "snoozed_until": t["snoozedUntil"] if _snoozed(t, now) else None,
               # Mark unread needs a finished run (T3 refuses it otherwise).
               "can_mark_unread": bool(t.get("latestRunCompletedAt")) and not render.is_unread(t)}
        # Surface subagents that need attention on their parent's row.
        states = [render.thread_state(c) for c in self._children(t["id"])]
        if states:
            row["agents"] = {"total": len(states), "needs_you": states.count("needs_you"),
                             "working": states.count("working")}
        return row

    def projects(self) -> dict:
        shell = self.shell or {"projects": []}
        return {"projects": [{"id": render.short_id(p["id"]), "title": p.get("title")}
                             for p in shell["projects"] if not p.get("archivedAt")]}

    # ---- thread view --------------------------------------------------------

    def thread_view(self, short: str, after: int | None = None, before: int | None = None,
                    turns: int = 3, visit: bool = False, progress: bool = False,
                    expand: frozenset[str] = frozenset()) -> dict:
        """`progress`: the version `v` also changes mid-turn (see wait_view).
        `expand`: short ids of finished runs to show unfolded."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        state = render.thread_state(t)
        while True:
            items, unfolded = self._load(t)
            cursor = unfolded[-1]["o"] if unfolded else 0
            live_run = None
            if state in ("working", "needs_you"):
                live_run = t.get("activeRunId") or next(
                    (it.get("runId") for it in reversed(items) if it.get("runId")), None)
            entries = render.render_items(
                render.fold_runs(items, live_run, frozenset(expand)), child_info=self._child_info,
                expanded=frozenset(expand),
                image_ref=lambda kind, src, alt: self.images.ref(t["id"], kind, src, alt))

            if after is not None:
                shown = [e for e in entries if e["o"] > after]
                more_before = False
                # A big thread's history comes in pages: fetch back to the cursor.
                short_of_it = not any(e["o"] <= after for e in entries)
            else:
                pool = [e for e in entries if before is None or e["o"] < before]
                you = [i for i, e in enumerate(pool) if e["role"] == "you"]
                start = you[-turns] if len(you) >= turns else 0
                shown = pool[start:]
                more_before = start > 0 or self._more_history(t["id"])
                short_of_it = start == 0
            if not (short_of_it and self._load_older(t)):
                break

        if state in ("done", "failed") and self._wants_suggestions(t["id"]):
            self._await_suggestions(t, cursor)

        unread = render.is_unread(t)
        if visit and unread:
            try:
                self.client.visit_thread(t["id"], _now_iso())
                unread = False  # the snapshot catches up on the next poll
            except Exception as e:
                log.warning("visit failed: %s", e)

        pending_view, replies, typed = self._replies(t, state, cursor)
        context, queue = self._snapshot_info(t)
        parent = self.threads.get((t.get("lineage") or {}).get("parentThreadId") or "")
        body = {
            "id": short, "title": t.get("title") or "Untitled", "state": state,
            "model": self._model_label(t.get("modelSelection")),
            "mode": "plan" if t.get("interactionMode") == "plan" else "default",
            "plan_mode": self._plan_mode_ok(t.get("modelSelection")),
            "prs": render.pull_requests(t),
            "tasks": render.todo_list(items, live_run),
            "context": context,
            "queue": queue and {k: v for k, v in queue.items() if k != "items"} | {
                "items": [{"id": i["id"], "text": i["text"]} for i in queue["items"]]},
            # For the thread menu (hold the title): the same fields as a list row.
            "settled": _is_settled(t), "pinned": bool(t.get("pinnedAt")),
            "snoozed_until": t["snoozedUntil"] if _snoozed(t) else None,
            "can_mark_unread": bool(t.get("latestRunCompletedAt")) and not unread,
            "parent": ({"id": render.short_id(parent["id"]), "title": parent.get("title") or "Untitled"}
                       if parent and _is_subagent(t) else None),
            "cursor": cursor, "more_before": more_before, "entries": shown,
            "pending": pending_view, "replies": replies, "typed": typed,
            # Images in the shown entries, for the device to fetch from /v1/images/<id>.
            "images": list(dict.fromkeys(
                i for e in shown for i in re.findall(r'src="t3img:(\w+)"', e.get("html", "")))),
        }
        body["v"] = _content_version(body, progress)
        return body

    # ---- reply buttons ------------------------------------------------------

    def _pending_secret(self, t: dict) -> dict | None:
        """The secret request this thread is waiting on, if any (T3's request_secret tool)."""
        items, _ = self._load(t)
        for it in reversed(items):
            if (it.get("type") == "secret_request" and it.get("secretStatus") == "pending"
                    and (it.get("_visibility") or "local") == "local"):
                return it
        return None

    def _active_plan(self, t: dict) -> dict | None:
        """The proposed plan waiting to be implemented (T3's "actionable" plan): the
        thread's latest one, while T3 still marks it active."""
        if not t.get("hasActionableProposedPlan"):
            return None
        items, _ = self._load(t)
        return next((it for it in reversed(items) if it.get("type") == "proposed_plan"
                     and not it.get("streaming") and it.get("planId")), None)

    def _plan_mode_ok(self, sel: dict | None) -> bool:
        """Whether the model's provider has a plan mode (T3 hides the toggle otherwise)."""
        inst = (sel or {}).get("instanceId")
        try:
            providers = self._server_config().get("providers") or []
        except Exception as e:
            log.warning("server config failed: %s", e)
            return False
        p = next((p for p in providers if p.get("instanceId") == inst), None)
        return p is not None and p.get("showInteractionModeToggle") is not False

    def _set_mode(self, t: dict, mode: str | None, cmid: str | None) -> None:
        """Switch plan mode before a message, as the app does when you send with the
        Plan toggle changed. None leaves it as it is."""
        if mode is None or (t.get("interactionMode") or "default") == mode:
            return
        if mode not in ("plan", "default"):
            raise BridgeError(400, "mode is `plan` or `default`")
        if mode == "plan" and not self._plan_mode_ok(t.get("modelSelection")):
            raise BridgeError(400, "this model has no plan mode")
        self.client.set_interaction_mode(t["id"], mode, command_id=_stable_id(cmid, "i"))

    def _replies(self, t: dict, state: str, cursor: int):
        """(pending description, buttons, what typed text does)."""
        if state == "needs_you":
            secret = self._pending_secret(t)
            if secret is not None:
                return self._secret_replies(secret)
            pending = self._pending(t)
            if pending:
                return self._pending_replies(pending[0])
            # The shell says something is pending but the projection doesn't show it yet.
            return {"kind": "unknown", "text": "Waiting for you on another device."}, [], "message"
        buttons = []
        if state == "working":
            buttons.append({"id": "c:stop", "label": "Stop", "kind": "shortcut"})
            return None, buttons, "message"
        plan = self._active_plan(t)
        if plan is not None:
            buttons.append({"id": f"p:{render.short_id(plan['planId'])}", "label": "Implement plan",
                            "kind": "plan"})
        sug = self.suggestions.get(t["id"])
        if sug and sug["cursor"] == cursor and sug["status"] == "ready":
            for i, text in enumerate(sug["items"]):
                buttons.append({"id": f"s:{cursor}:{i}", "label": text, "kind": "suggestion"})
        for key, (label, _) in SHORTCUTS.items():
            buttons.append({"id": f"c:{key}", "label": label, "kind": "shortcut"})
        return None, buttons, "message"

    def _secret_replies(self, item: dict):
        sid = render.short_id(item["id"])
        view = {"kind": "secret", "header": item.get("label") or "Secret",
                "text": item.get("reason") or "", "placeholder": item.get("placeholder"),
                # As T3's own card says (client-runtime secretRequestPrivacyNote):
                "note": ("Kept out of chat, in a private file this agent's commands can read for 24 hours"
                         if item.get("delivery") == "file" else "Stored securely, never shown to the agent")}
        link = render.secret_request_link(item)
        if link:
            view["link"] = link
        buttons = [{"id": f"x:{sid}:enter", "label": "Enter secret", "kind": "secret"},
                   {"id": f"x:{sid}:decline", "label": "Decline", "kind": "answer"},
                   {"id": "c:stop", "label": "Stop", "kind": "shortcut"}]
        # "none": typed text must never go to the chat while a secret is being asked for.
        return view, buttons, "none"

    def answer_secret(self, short: str, secret: str | None) -> dict:
        """Give the pending secret request its value (or decline it with None).
        The value goes straight to T3, which stores it; it's never logged or kept here."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        item = self._pending_secret(t)
        if item is None:
            raise BridgeError(409, "no secret is being asked for")
        if secret is not None and not secret.strip():
            raise BridgeError(400, "the secret is empty")
        answer = {"type": "decline"} if secret is None else {"type": "save", "secret": secret.strip()}
        try:
            self.client.answer_secret(t["id"], item["id"], answer)
        except Exception as e:
            # T3's error names a reason (e.g. already answered), never the value.
            raise BridgeError(409, _secret_error(e)) from None
        before = _wake_key(t)
        self._settle(t["id"], before)
        return self.thread_view(short)

    def _pending_replies(self, p: dict):
        req, item = p["request"], p["item"] or {}
        rshort = render.short_id(req["id"])
        capability = (req.get("responseCapability") or {}).get("type")
        if req["kind"] == "user_input":
            answers = self.partial_answers.get(req["id"], {})
            questions = item.get("questions", [])
            qi = next((i for i, q in enumerate(questions) if q["id"] not in answers), 0)
            q = questions[qi]
            view = {"kind": "question", "header": q.get("header"), "text": q.get("question"),
                    "step": f"{qi + 1}/{len(questions)}" if len(questions) > 1 else None,
                    "options": [{"label": o.get("label"), "description": o.get("description")}
                                for o in q.get("options", [])]}
            if capability == "not_resumable":
                view["note"] = "This question can no longer be answered."
                return view, [], "message"
            if q.get("multiSelect"):
                # Tap to tick options (● picked, ○ not), then Done sends them together.
                picked = self.multi_picks.get((req["id"], q["id"]), [])
                buttons = [{"id": f"m:{rshort}:{qi}:{oi}", "kind": "toggle",
                            "label": ("● " if o.get("label") in picked else "○ ") + str(o.get("label"))}
                           for oi, o in enumerate(q.get("options", []))]
                buttons.append({"id": f"d:{rshort}:{qi}", "kind": "submit", "enabled": bool(picked),
                                "label": f"Done ({len(picked)} picked)" if picked else "Done"})
            else:
                buttons = [{"id": f"q:{rshort}:{qi}:{oi}", "label": o.get("label"), "kind": "answer"}
                           for oi, o in enumerate(q.get("options", []))]
            typed = "message" if capability == "message" else (
                "answer" if q.get("allowCustomAnswer", True) is not False else "none")
            buttons.append({"id": "c:stop", "label": "Stop", "kind": "shortcut"})
            return view, buttons, typed
        view = {"kind": "approval", "header": req["kind"].replace("-", " ").capitalize(),
                "text": item.get("prompt") or item.get("reason") or req["kind"]}
        if item.get("reason") and item.get("prompt"):
            view["note"] = item["reason"]
        if capability != "live":
            view["note"] = "Answer this on another device."
            return view, [], "message"
        buttons = [{"id": f"a:{rshort}:{d}", "label": label, "kind": "answer"}
                   for d, label in APPROVAL_BUTTONS]
        return view, buttons, "message"

    # ---- suggestions --------------------------------------------------------

    def _wants_suggestions(self, tid: str) -> bool:
        t = self.threads.get(tid)
        return bool(t and self._top_level(t))

    def _schedule_suggestions(self, tid: str) -> dict | None:
        t = self.threads.get(tid)
        if t is None:
            return None
        try:
            entries = self._entries(t)
        except Exception as e:
            log.warning("suggestions: transcript failed: %s", e)
            return None
        if not entries or entries[-1]["role"] != "agent":
            return None
        cursor = entries[-1]["o"]
        written = entries[-1].get("replies")  # the agent's own, from its reply
        if written is None and self.suggester is None:
            return None
        with self.cond:
            cur = self.suggestions.get(tid)
            if cur and cur["cursor"] == cursor and not (
                    cur["status"] == "failed" and time.monotonic() - cur["at"] > SUGGESTION_RETRY):
                return cur
            slot = {"cursor": cursor, "status": "pending", "items": [], "done": threading.Event(),
                    "at": time.monotonic()}
            self.suggestions[tid] = slot
            if written is not None:
                slot.update(status="ready", items=written)
                slot["done"].set()
                return slot
        self.pool.submit(self._run_suggestions, tid, slot, entries[-12:])
        return slot

    def _run_suggestions(self, tid: str, slot: dict, entries: list[dict]) -> None:
        try:
            slot["items"] = self.suggester.suggest(entries)
            slot["status"] = "ready"
        except Exception as e:
            log.warning("suggestions failed: %s", e)
            slot["status"] = "failed"
        finally:
            slot["done"].set()
            with self.cond:
                self._bump(tid)
                self.cond.notify_all()

    def _await_suggestions(self, t: dict, cursor: int) -> None:
        slot = self._schedule_suggestions(t["id"])  # returns the existing slot if still good
        if slot and slot["status"] == "pending":
            slot["done"].wait(SUGGESTION_WAIT)

    # ---- waiting ------------------------------------------------------------

    def wait_view(self, short: str, v: str, timeout: float, progress: bool = False,
                  **view_args) -> tuple[bool, dict]:
        """Block until the thread view would look different from version `v` (or timeout).
        Returns (changed, view).

        T3 updates the thread for things the view doesn't show (reasoning, replies still
        being written), so this re-renders on each update and compares what's displayed.
        """
        self.last_device_at = time.monotonic()
        tid = self.by_short.get(short)
        if tid is None:
            raise BridgeError(404, "no such thread")
        deadline = time.monotonic() + timeout
        while True:
            seen = self.thread_pv.get(tid, 0)
            view = self.thread_view(short, progress=progress, **view_args)
            if view["v"] != v:
                return True, view
            with self.cond:
                while self.thread_pv.get(tid, 0) == seen:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False, view
                    self.cond.wait(left)
            self.last_device_at = time.monotonic()

    def wait_list(self, v: int, timeout: float) -> bool:
        self.last_device_at = time.monotonic()
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.list_v == v:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
                self.last_device_at = time.monotonic()
        return True

    # ---- actions ------------------------------------------------------------

    def _once(self, client_msg_id: str | None, fn):
        """Run an action at most once per client_msg_id; a retry gets the first answer."""
        if client_msg_id and client_msg_id in self.done_requests:
            return self.done_requests[client_msg_id]
        result = fn()
        if client_msg_id:
            self.done_requests[client_msg_id] = result
            while len(self.done_requests) > 500:
                self.done_requests.popitem(last=False)
        return result

    def reply(self, short: str, reply_id: str | None = None, text: str | None = None,
              client_msg_id: str | None = None, progress: bool = False,
              expand: frozenset[str] = frozenset(), mode: str | None = None,
              deliver: str | None = None) -> dict:
        """`mode` ("plan" or "default"): for typed text, switch plan mode first.
        `deliver`: for typed text while a turn runs, "queue" it for after the turn or
        "steer" the turn with it (T3 queues it anyway when the turn can't take it)."""
        if deliver not in (None, "queue", "steer"):
            raise BridgeError(400, "deliver is `queue` or `steer`")
        self.last_device_at = time.monotonic()
        t = self._thread(short)

        def act():
            before = _wake_key(t)
            if reply_id:
                sent = self._do_reply(t, reply_id, client_msg_id)
            elif text and text.strip():
                sent = self._do_text(t, text.strip(), client_msg_id, mode, deliver)
            else:
                raise BridgeError(400, "send `reply` or `text`")
            if sent is not False:  # False: handled here (a tick, a first answer); T3 won't react
                self._settle(t["id"], before)
            return self.thread_view(short, progress=progress, expand=expand)

        return self._once(client_msg_id, act)

    settle_delay = 0.4

    def _settle(self, tid: str, before: tuple, tries: int = 6) -> None:
        """T3 accepts a command before the run starts. Refresh briefly until the thread
        visibly reacts, so the answer to a tap already shows the new state."""
        for i in range(tries):
            self.refresh()
            t = self.threads.get(tid)
            if t is None or _wake_key(t) != before:
                return
            if i < tries - 1:
                time.sleep(self.settle_delay)

    def _do_reply(self, t: dict, reply_id: str, cmid: str | None) -> None:
        tid = t["id"]
        kind, _, rest = reply_id.partition(":")
        if kind == "c":
            if rest == "stop":
                self._stop_thread(t, cmid)
                return
            if rest in SHORTCUTS:
                self.client.send_message(tid, SHORTCUTS[rest][1], command_id=_stable_id(cmid, "c"),
                                         message_id=_stable_id(cmid, "m"))
                return
        elif kind == "s":
            cursor, _, idx = rest.partition(":")
            sug = self.suggestions.get(tid)
            if not sug or str(sug["cursor"]) != cursor or not idx.isdigit() or int(idx) >= len(sug["items"]):
                raise BridgeError(409, "that suggestion is out of date")
            self.client.send_message(tid, sug["items"][int(idx)], command_id=_stable_id(cmid, "c"),
                                     message_id=_stable_id(cmid, "m"))
            return
        elif kind == "p":
            plan = self._active_plan(t)
            if plan is None or render.short_id(plan["planId"]) != rest:
                raise BridgeError(409, "that plan was already implemented or replaced")
            # As the app's Implement button: leave plan mode, send the plan, and link the
            # turn to it so T3 marks it implemented.
            self._set_mode(t, "default", cmid)
            self.client.send_message(tid, render.PLAN_PROMPT + (plan.get("markdown") or "").strip(),
                                     command_id=_stable_id(cmid, "c"), message_id=_stable_id(cmid, "m"),
                                     source_plan={"threadId": tid, "planId": plan["planId"]})
            return
        elif kind == "x":
            sid, _, action = rest.partition(":")
            item = self._pending_secret(t)
            if item is None or render.short_id(item["id"]) != sid:
                raise BridgeError(409, "that secret request was already answered")
            if action != "decline":
                raise BridgeError(400, "send the secret to /v1/threads/<id>/secret")
            self.client.answer_secret(tid, item["id"], {"type": "decline"})
            return
        elif kind in ("a", "q", "m", "d"):
            pending = self._pending(t)
            match = next((p for p in pending if render.short_id(p["request"]["id"]) == rest.split(":")[0]), None)
            if match is None:
                raise BridgeError(409, "that request was already answered")
            req = match["request"]
            if kind == "a":
                decision = rest.split(":", 1)[1]
                if decision not in dict(APPROVAL_BUTTONS):
                    raise BridgeError(400, "unknown decision")
                self.client.respond_approval(tid, req["id"], decision, command_id=_stable_id(cmid, "c"))
                return
            questions = (match["item"] or {}).get("questions", [])
            parts = rest.split(":")
            try:
                q = questions[int(parts[1])]
            except (IndexError, ValueError):
                raise BridgeError(400, "unknown question") from None
            if kind == "d":
                picked = self.multi_picks.get((req["id"], q["id"]), [])
                if not picked:
                    raise BridgeError(400, "pick at least one option")
                return self._answer(t, match, q, list(picked), cmid)
            label = q["options"][int(parts[2])]["label"]
            if kind == "m":
                key = (req["id"], q["id"])
                picked = self.multi_picks.setdefault(key, [])
                if label in picked:
                    picked.remove(label)
                else:
                    picked.append(label)
                order = [o["label"] for o in q["options"]]
                picked.sort(key=order.index)
                self._redraw(tid)
                return False
            return self._answer(t, match, q, label, cmid)
        raise BridgeError(400, "unknown reply")

    def _do_text(self, t: dict, text: str, cmid: str | None, mode: str | None = None,
                 deliver: str | None = None) -> None:
        state = render.thread_state(t)
        if state == "needs_you":
            pending = self._pending(t)
            if pending and pending[0]["request"]["kind"] == "user_input":
                p = pending[0]
                capability = (p["request"].get("responseCapability") or {}).get("type")
                questions = (p["item"] or {}).get("questions", [])
                answers = self.partial_answers.get(p["request"]["id"], {})
                q = next((q for q in questions if q["id"] not in answers), None)
                if q is not None and capability == "live" and q.get("allowCustomAnswer", True) is not False:
                    return self._answer(t, p, q, text, cmid)
        self._set_mode(t, mode, cmid)
        self.client.send_message(t["id"], text, queue=deliver == "queue" and state == "working",
                                 command_id=_stable_id(cmid, "c"), message_id=_stable_id(cmid, "m"))

    def _answer(self, t: dict, p: dict, q: dict, answer, cmid: str | None) -> bool:
        """Record one question's answer (a label, typed text, or a list of labels for a
        multiple-choice question); submit once every question has one.
        Returns True if anything was sent to T3."""
        req = p["request"]
        capability = (req.get("responseCapability") or {}).get("type")
        if capability == "message":
            text = ", ".join(answer) if isinstance(answer, list) else answer
            self.client.send_message(t["id"], text, command_id=_stable_id(cmid, "c"),
                                     message_id=_stable_id(cmid, "m"))
            return True
        if q.get("multiSelect") and not isinstance(answer, list):
            # Typed text on a multiple-choice question: add it to whatever is ticked.
            answer = self.multi_picks.get((req["id"], q["id"]), []) + [answer]
        answers = self.partial_answers.setdefault(req["id"], {})
        answers[q["id"]] = answer
        questions = (p["item"] or {}).get("questions", [])
        if all(x["id"] in answers for x in questions):
            self.client.respond_user_input(t["id"], req["id"], dict(answers),
                                           command_id=_stable_id(cmid, "c"))
            self.partial_answers.pop(req["id"], None)
            for x in questions:
                self.multi_picks.pop((req["id"], x["id"]), None)
            return True
        else:
            # Next question: the view changes without anything changing in T3.
            self._redraw(t["id"])
            return False

    def _redraw(self, tid: str) -> None:
        """The device's view changed without any change in T3."""
        with self.cond:
            self._bump(tid)
            self.cond.notify_all()

    def _stop_thread(self, t: dict, cmid: str | None) -> None:
        if self.stop_capability:
            self.client.stop_thread(t["id"], command_id=_stable_id(cmid, "c"))
        elif t.get("activeRunId"):
            self.client.interrupt_run(t["id"], t["activeRunId"], command_id=_stable_id(cmid, "c"))

    def new_thread_settings(self, pid: str, project: dict,
                            workspace: bool = True) -> tuple[dict, str, dict | None]:
        """(model selection, runtime mode, workspace strategy) for a new thread, from the
        user's T3 settings, the way the app picks them: the project's override, then the
        server-wide default. Read fresh each time so a change made on the desktop applies
        at once. The workspace is None for the project root, or when `workspace` is False
        (only the model and mode are wanted)."""
        config = self._server_config(fresh=True)
        settings = config.get("settings") or {}
        override = (settings.get("projectSettingsOverrides") or {}).get(pid) or {}
        model = (override.get("defaultModelSelection") or project.get("defaultModelSelection")
                 or settings.get("defaultModelSelection") or settings.get("fallbackModelSelection"))
        if not model:
            # No default configured anywhere: fall back to the project's latest thread.
            recent = max((t for t in self.threads.values() if t.get("projectId") == pid
                          and self._top_level(t)),
                         key=lambda t: t.get("createdAt") or "", default=None)
            model = (recent or {}).get("modelSelection")
        if not model:
            # A fresh install: the app then offers its first working provider's default model.
            model = first_ready_model(config.get("providers") or [])
        if not model:
            raise BridgeError(409, "no agent is ready in T3: install and sign in to one first")
        # T3's own default when the setting is absent.
        runtime = override.get("defaultRuntimeMode") or settings.get("defaultRuntimeMode") or "full-access"
        env_mode = (override.get("defaultThreadEnvMode") or project.get("defaultThreadEnvMode")
                    or settings.get("defaultThreadEnvMode") or "local")
        strategy = None
        if workspace and env_mode == "worktree":
            strategy = self._new_worktree(project, settings.get("newWorktreesStartFromOrigin", True))
        return model, runtime, strategy

    def _new_worktree(self, project: dict, start_from_origin: bool) -> dict | None:
        """A new worktree from the branch the project's checkout is on, as the app does.
        Not a git repo: the root, as the app does too."""
        root = project.get("workspaceRoot")
        try:
            status = self.client.vcs_status(root)
        except Exception as e:
            raise BridgeError(502, f"couldn't read the project's git branch: {e}") from None
        if not status.get("isRepo"):
            return None
        return {"type": "worktree", "baseRef": status.get("refName") or "HEAD",
                "startFromOrigin": bool(start_from_origin)}

    def settle(self, short: str, settled: bool = True) -> dict:
        """Settle a thread (mark it handled, out of the main list) or unsettle it."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        if settled:
            self.client.settle_thread(t["id"])
        else:
            self.client.unsettle_thread(t["id"])
        for _ in range(6):  # until the shell shows it, so the device's next list is right
            self.refresh()
            if _is_settled(self.threads.get(t["id"], {})) == settled:
                break
            time.sleep(self.settle_delay)
        return {"ok": True, "settled": _is_settled(self.threads.get(t["id"], {}))}

    ACTIONS = {
        "pin": ("thread.pin", lambda t: bool(t.get("pinnedAt"))),
        "unpin": ("thread.unpin", lambda t: not t.get("pinnedAt")),
        "snooze": ("thread.snooze", lambda t: bool(t.get("snoozedUntil"))),
        "unsnooze": ("thread.unsnooze", lambda t: not _snoozed(t)),
        "mark_unread": ("thread.mark-unread", render.is_unread),
        "archive": ("thread.archive", lambda t: t is None or bool(t.get("archivedAt"))),
        "rename": ("thread.metadata.update", None),  # done: see thread_action
        "auto_title": ("thread.metadata.update", None),
    }

    def thread_action(self, short: str, action: str, snooze: str | None = None,
                      title: str | None = None) -> dict:
        """Pin, unpin, snooze (until one of the app's presets), wake, mark unread, archive,
        rename (to `title`) or retitle (auto_title: T3 writes a new title) a thread, as
        the app's thread menu does. Returns once the shell shows it."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        if action not in self.ACTIONS:
            raise BridgeError(400, "unknown action")
        type_, done = self.ACTIONS[action]
        fields = {}
        if action == "snooze":
            if render.thread_state(t) == "needs_you":
                raise BridgeError(409, "a thread waiting for you can't be snoozed")
            fields["snoozedUntil"] = snooze_until(snooze or "")
        elif action == "unsnooze":
            fields["reason"] = "user"
        elif action == "archive" and render.thread_state(t) in ("working", "needs_you"):
            raise BridgeError(409, "stop the thread before archiving it")
        elif action == "rename":
            title = " ".join((title or "").split())
            if not title:
                raise BridgeError(400, "the title is empty")
            fields["title"] = title
            done = lambda now: (now or {}).get("title") == title
        elif action == "auto_title":
            # T3 generates it in the background; the list picks it up when it lands.
            fields["regenerateTitle"] = True
            done = lambda now: True
        try:
            self.client.thread_command(type_, t["id"], **fields)
        except T3Error as e:
            raise BridgeError(502, f"T3 refused: {e}") from None
        for _ in range(6):  # until the shell shows it, so the device's next list is right
            self.refresh()
            if done(self.threads.get(t["id"])):
                break
            time.sleep(self.settle_delay)
        return {"ok": True}

    def queue_action(self, short: str, action: str, item: str | None = None) -> dict:
        """What the app's queue offers: `send` a queued message now (when no turn runs),
        `steer` the running turn with it, `remove` it, or `resume` a queue paused by
        Stop. `item`: the queued message's id from the view. Returns the new view's
        queue once T3 shows the change (or after a few seconds)."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        if action not in ("send", "steer", "remove", "resume"):
            raise BridgeError(400, "action is send, steer, remove or resume")
        proj = self.client.thread_bounded(t["id"])["projection"]
        queue = render.queue_state(proj)
        if queue is None:
            raise BridgeError(409, "nothing is queued")
        run = None
        if action != "resume":
            run = next((i["run"] for i in queue["items"] if i["id"] == item), None)
            if run is None:
                raise BridgeError(409, "that message isn't queued any more")
        try:
            if action == "send":
                if not queue["can_send_now"]:
                    raise BridgeError(409, "a turn is running: steer it instead, or wait")
                self.client.thread_command("queued-run.start", t["id"], runId=run)
            elif action == "steer":
                active = render.active_run(proj)
                if not queue["can_steer"] or active is None:
                    raise BridgeError(409, "the running turn can't take a message now")
                self.client.thread_command("queued-message.promote-to-steer", t["id"],
                                           queuedRunId=run, targetRunId=active["id"])
            elif action == "remove":
                self.client.thread_command("queued-run.cancel", t["id"], runId=run)
            else:
                self.client.thread_command("queue.resume", t["id"])
        except T3Error as e:
            raise BridgeError(502, f"T3 refused: {e}") from None

        def done(q):
            if action == "resume":
                return q is None or not q["held"]
            return q is None or all(i["run"] != run for i in q["items"])

        for _ in range(10):
            time.sleep(self.settle_delay)
            try:
                if done(render.queue_state(self.client.thread_bounded(t["id"])["projection"])):
                    break
            except T3Error:
                pass
        self.refresh()
        return {"ok": True}

    def start_thread(self, project: str, text: str, client_msg_id: str | None = None,
                     model: str | None = None, mode: str | None = None) -> dict:
        """`model`: an id from /v1/models; None uses the thread default from T3's settings.
        `mode`: "plan" starts the thread in plan mode."""
        self.last_device_at = time.monotonic()
        pid = self.by_short.get(project)
        shell = self.shell or {"projects": []}
        p = next((x for x in shell["projects"] if x["id"] == pid), None)
        if p is None:
            raise BridgeError(404, "no such project")
        if not text.strip():
            raise BridgeError(400, "text is empty")

        def act():
            default, runtime, workspace = self.new_thread_settings(pid, p)
            chosen = self._resolve_model(model) if model else default
            if mode not in (None, "default", "plan"):
                raise BridgeError(400, "mode is `plan` or `default`")
            if mode == "plan" and not self._plan_mode_ok(chosen):
                raise BridgeError(400, "this model has no plan mode")
            title = " ".join(text.split())[:60]
            res = self.client.launch_thread(pid, title, text.strip(), chosen, runtime_mode=runtime,
                                            command_id=_stable_id(client_msg_id, "c"),
                                            message_id=_stable_id(client_msg_id, "m"),
                                            generate_title=True, workspace=workspace,
                                            interaction_mode=mode or "default")
            self.refresh()
            return self.thread_view(render.short_id(res["threadId"]))

        return self._once(client_msg_id, act)

    # ---- models -------------------------------------------------------------

    def _server_config(self, fresh: bool = False) -> dict:
        """T3's server config (providers with their models, the user's settings), reused
        for CONFIG_TTL seconds. `fresh`: always fetch, for a decision that must follow a
        setting changed on the desktop a moment ago."""
        now = time.monotonic()
        if fresh or self._config is None or now - self._config[0] > CONFIG_TTL:
            self._config = (now, self.client.server_config() or {})
        return self._config[1]

    def _catalog(self) -> list[dict]:
        """Every model of every provider instance, in T3's order:
        {id, label, group, sel, hidden, legacy}. `sel` is T3's model selection."""
        config = self._server_config()
        prefs = (config.get("settings") or {}).get("providerModelPreferences") or {}
        out = []
        for p in config.get("providers") or []:
            inst = p.get("instanceId")
            if not inst:
                continue
            usable = p.get("enabled", True) and p.get("installed", True) and \
                p.get("status") not in ("disabled", "error")
            hidden = set((prefs.get(inst) or {}).get("hiddenModels") or [])
            for m in p.get("models") or []:
                slug = m.get("slug")
                if not slug:
                    continue
                out.append({"id": _model_id(inst, slug), "label": m.get("name") or slug,
                            "group": p.get("displayName") or inst,
                            "sel": {"instanceId": inst, "model": slug},
                            "hidden": not usable or slug in hidden, "legacy": bool(m.get("isLegacy"))})
        return out

    def _model_label(self, sel: dict | None) -> str | None:
        if not sel or not sel.get("model"):
            return None
        mid = _model_id(sel.get("instanceId") or "", sel["model"])
        try:
            found = next((m for m in self._catalog() if m["id"] == mid), None)
        except Exception as e:  # a label isn't worth failing the view for
            log.warning("model list failed: %s", e)
            found = None
        return found["label"] if found else sel["model"]

    def _resolve_model(self, mid: str) -> dict:
        found = next((m for m in self._catalog() if m["id"] == mid), None)
        if found is None or found["hidden"]:
            raise BridgeError(400, "unknown or unavailable model")
        return dict(found["sel"])

    def models(self, thread: str | None = None, project: str | None = None) -> dict:
        """The models to choose from: the desktop's favorites, then every other model that
        isn't hidden or legacy, grouped by provider. `current` is the thread's model, or
        the one a new thread in `project` would get."""
        self.last_device_at = time.monotonic()
        current = None
        if thread:
            current = self._thread(thread).get("modelSelection")
        elif project:
            pid = self.by_short.get(project)
            p = next((x for x in (self.shell or {}).get("projects", []) if x["id"] == pid), None)
            if p is None:
                raise BridgeError(404, "no such project")
            current = self.new_thread_settings(pid, p, workspace=False)[0]
        current_id = _model_id(current.get("instanceId") or "", current.get("model") or "") \
            if current else None
        catalog = [m for m in self._catalog() if not m["hidden"]]
        by_id = {m["id"]: m for m in catalog}
        settings = self._server_config().get("settings") or {}
        favorites = []
        for f in settings.get("favorites") or []:
            m = by_id.get(_model_id(f.get("instanceId") or f.get("provider") or "", f.get("model") or ""))
            if m and m not in favorites:
                favorites.append(m)
        # The same model under two providers (say, a personal and a work account): say which.
        names = [m["label"] for m in favorites]
        groups: dict[str, list[dict]] = {}
        for m in catalog:
            if (not m["legacy"] or m["id"] == current_id) and m not in favorites:
                groups.setdefault(m["group"], []).append({"id": m["id"], "label": m["label"]})
        return {
            "current": current_id,
            "current_label": self._model_label(current),
            "favorites": [{"id": m["id"], "label": m["label"] if names.count(m["label"]) == 1
                           else f"{m['label']} · {m['group']}"} for m in favorites],
            "groups": [{"title": g, "models": ms} for g, ms in groups.items()],
        }

    def set_model(self, short: str, model: str, client_msg_id: str | None = None,
                  progress: bool = False) -> dict:
        """Switch a thread's model, as the app's model picker does. The next turn uses it."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)

        def act():
            sel = self._resolve_model(model)
            cur = t.get("modelSelection") or {}
            if (cur.get("instanceId"), cur.get("model")) != (sel["instanceId"], sel["model"]):
                try:
                    self.client.set_model(t["id"], sel, command_id=_stable_id(client_msg_id, "c"))
                except T3Error as e:
                    raise BridgeError(502, f"T3 refused the model change: {e}") from None
                for _ in range(6):  # until the shell shows it, so the returned view is right
                    self.refresh()
                    now = self.threads.get(t["id"], {}).get("modelSelection") or {}
                    if (now.get("instanceId"), now.get("model")) == (sel["instanceId"], sel["model"]):
                        break
                    time.sleep(self.settle_delay)
            return self.thread_view(short, progress=progress)

        return self._once(client_msg_id, act)


    # ---- preview browser (view only) ----------------------------------------

    def browser_tabs(self, short: str) -> dict:
        """The thread's preview browser tabs. Only tabs running on the T3 server
        (`runtime: "server"`, which is where agents' tabs run) can be shown; a tab open
        in the desktop app's own window lives there and nowhere else."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        try:
            sessions = self.client.preview_tabs(t["id"])
        except T3Error as e:
            raise BridgeError(502, f"T3 wouldn't list the browser tabs: {e}") from None
        tabs = []
        for s in sessions:
            nav = s.get("navStatus") or {}
            url = nav.get("url") or ""
            tabs.append({
                "id": s.get("tabId"),
                "title": nav.get("title") or url or "Blank tab",
                "url": url,
                "status": {"Loading": "loading", "LoadFailed": "failed"}.get(nav.get("_tag"), "ok"),
                "viewable": s.get("runtime") == "server",
            })
        return {"tabs": tabs, "devices": self._thread_devices(t["id"])}

    # ---- pull requests ------------------------------------------------------

    def pull_request_list(self, scope: str = "mine") -> dict:
        """Open PRs in the projects' repositories. `mine`: the app's first two groups,
        review requested and yours; `all`: everyone's, grouped by project."""
        self.last_device_at = time.monotonic()

        def fetch(involvement: str) -> dict:
            try:
                # Everyone's PRs take T3 a while to gather, so fewer per repository.
                return self.client.pull_requests("open", involvement, 40 if involvement == "all" else 100)
            except (T3Error, OSError) as e:
                raise BridgeError(502, _t3_message(e)) from None

        def rows(entries: list[dict]) -> list[dict]:
            entries = sorted(entries, key=lambda e: e.get("updatedAt") or "", reverse=True)
            return [prs.row(e, render.short_id(e["projectId"])) for e in entries]

        groups: list[dict] = []
        if scope == "all":
            result = fetch("all")
            by_project: dict[str, list[dict]] = {}
            for e in result.get("entries") or []:
                by_project.setdefault(e.get("projectTitle") or "", []).append(e)
            # Projects with the latest activity first.
            for title, entries in sorted(by_project.items(), key=lambda kv: max(
                    e.get("updatedAt") or "" for e in kv[1]), reverse=True):
                groups.append({"title": title, "prs": rows(entries)})
            errors, truncated = result.get("errors") or [], bool(result.get("truncated"))
        else:
            mine, review = fetch("authored"), fetch("reviewing")
            seen = {(e["projectId"], e["number"]) for e in review.get("entries") or []}
            groups.append({"title": "Review requested", "prs": rows(review.get("entries") or [])})
            groups.append({"title": "Yours", "prs": rows(
                [e for e in mine.get("entries") or [] if (e["projectId"], e["number"]) not in seen])})
            errors = {e.get("projectId"): e for e in (mine.get("errors") or []) + (review.get("errors") or [])}
            errors = list(errors.values())
            truncated = bool(mine.get("truncated") or review.get("truncated"))
        return {
            "scope": "all" if scope == "all" else "mine",
            "groups": [g for g in groups if g["prs"]],
            "errors": [{"project": e.get("projectTitle"), "message": e.get("message")} for e in errors],
            "truncated": truncated,
        }

    def pull_request_detail(self, project: str, repo: str, number: int, host: str | None = None) -> dict:
        """One PR's page: status, checks, description, conversation, commits and files."""
        self.last_device_at = time.monotonic()
        pid = self.by_short.get(project)
        if not pid or not any(p["id"] == pid for p in (self.shell or {}).get("projects") or []):
            raise BridgeError(404, "no such project")
        ref = {"projectId": pid, "repository": repo, "number": number}
        if host:
            ref["host"] = host
        try:
            detail = self.client.pull_request(ref)
        except (T3Error, OSError) as e:
            raise BridgeError(502, _t3_message(e)) from None

        def extra(name: str, fn):
            try:
                return fn(ref)
            except (T3Error, OSError) as e:  # the page still shows without it
                log.info("no PR %s for %s#%s: %s", name, repo, number, e)
                return None

        activity = extra("activity", self.client.pull_request_activity)
        breakdown = extra("breakdown", self.client.pull_request_breakdown)
        diff = extra("diff", self.client.pull_request_diff)
        linked = extra("threads", self.client.pull_request_threads) or []
        threads = [{"id": render.short_id(t["id"]), "title": self.threads[t["id"]].get("title")}
                   for t in linked if t.get("id") in self.threads and not t.get("archivedAt")]
        files = prs.diff_files(diff.get("patch") or "") if isinstance(diff, dict) else None
        return {
            "title": f"#{detail.get('number')} {detail.get('repository')}",
            "number": detail.get("number"), "repo": detail.get("repository"), "url": detail.get("url"),
            "status": prs.detail_status(detail, activity),
            "html": prs.detail_html(detail, activity, files, breakdown, threads),
        }

    def _thread_devices(self, thread_id: str, strict: bool = False) -> list[dict]:
        """Simulators and emulators open in the thread. T3 can screenshot any device on
        its machine, so only these are shown. A server without devices lists none."""
        try:
            state = self.client.devices() or {}
        except (T3Error, OSError) as e:
            if strict:
                raise BridgeError(502, f"T3 wouldn't list the devices: {e}") from None
            log.info("no device list: %s", e)
            return []
        known = {(d.get("hostId"), d.get("id")): d for d in state.get("devices") or []}
        out = []
        for s in state.get("sessions") or []:
            if s.get("threadId") != thread_id:
                continue
            d = known.get((s.get("hostId"), s.get("deviceId"))) or {}
            platform = s.get("platform") or d.get("platform") or "ios"
            out.append({
                "id": s.get("deviceId"),
                "hostId": s.get("hostId"),
                "name": d.get("name") or s.get("deviceId"),
                "platform": platform,
                "kind": "Android emulator" if platform == "android" else "iOS simulator",
                "booted": bool(d.get("booted", True)),
            })
        return out

    def device_snapshot(self, short: str, device_id: str, width: int) -> tuple[bytes, str]:
        """What a simulator or emulator open in the thread shows now, at most `width` wide."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        dev = next((d for d in self._thread_devices(t["id"], strict=True)
                    if d["id"] == device_id), None)
        if dev is None:
            raise BridgeError(404, "That device isn't open in this thread.")
        try:
            png = self.client.device_screenshot(dev["hostId"], device_id, dev["platform"])
        except T3Error as e:
            if e.status == 503:
                raise BridgeError(503, "T3's device hub isn't running; open the device "
                                       "in the desktop app to start it.") from None
            raise BridgeError(502, f"T3 couldn't take a screenshot: {e}") from None
        except OSError as e:
            raise BridgeError(502, f"T3 couldn't take a screenshot: {e}") from None
        return self.images.device_snapshot(png, width)

    def browser_snapshot(self, short: str, tab_id: str, width: int) -> tuple[bytes, str]:
        """What one tab shows right now, as an image at most `width` pixels wide."""
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        tab = next((s for s in self.client.preview_tabs(t["id"]) if s.get("tabId") == tab_id), None)
        if tab is None:
            raise BridgeError(404, "That browser tab is closed.")
        if tab.get("runtime") != "server":
            raise BridgeError(409, "That tab is open in the desktop app's window, which T3 can't share.")
        try:
            jpeg = self.client.preview_frame(t["id"], tab_id, max_width=width, max_height=width * 4)
        except T3SocketError as e:
            if e.close_code == PREVIEW_TAB_GONE:
                raise BridgeError(404, "That browser tab is closed.") from None
            if e.close_code == PREVIEW_HOST_SETUP:
                raise BridgeError(503, "T3's browser can't start on its machine; open the tab in "
                                       "the desktop app to see what it needs.") from None
            raise BridgeError(502, f"T3's browser stream failed: {e}") from None
        except (T3Error, OSError) as e:
            raise BridgeError(502, f"T3's browser stream failed: {e}") from None
        return self.images.page_snapshot(jpeg, width)


_PR_UNAVAILABLE = {
    "cli-missing": "T3 can't read pull requests: the tool it reads this host with isn't set up.",
    "cli-unauthenticated": "T3 can't read pull requests: this host has no working credentials.",
    "provider-unsupported": "T3 can't browse pull requests for this project's host yet.",
}


def _t3_message(e: Exception) -> str:
    """What a failed T3 call says, in its own words where it has some."""
    if isinstance(e, T3RpcError):
        stack = [e.exit]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                if str(v.get("_tag", "")).endswith("Error"):
                    text = v.get("requirement") or v.get("detail") or v.get("message")
                    text = text or _PR_UNAVAILABLE.get(v.get("reason") or "")
                    if text:
                        return str(text)
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
    return str(e)


def _model_id(instance: str, slug: str) -> str:
    return render.short_id(f"model:{instance}:{slug}")
