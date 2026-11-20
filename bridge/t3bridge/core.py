"""Bridge state and logic: what the device sees, and what its taps do in T3.

The HTTP layer (server.py) is a thin wrapper around `Bridge`. Everything here
works against any object with T3Client's methods, so tests can pass a fake.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import render
from .t3client import pending_requests_from

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
    return (state, pending, t.get("title"), render.is_unread(t),
            None if state == "working" else t.get("itemCount"))


class Bridge:
    def __init__(self, client, suggester=None, *, poll_fast: float = 2.0, poll_slow: float = 20.0):
        self.client = client
        self.suggester = suggester
        self.poll_fast = poll_fast
        self.poll_slow = poll_slow
        self.cond = threading.Condition()
        self.seq = 0  # bumps on every change the device could care about
        self.list_v = 0
        self.shell: dict | None = None
        self.threads: dict[str, dict] = {}  # long id -> shell thread
        self.thread_v: dict[str, int] = {}
        self.wake_keys: dict[str, tuple] = {}
        self.by_short: dict[str, str] = {}  # short id -> long id (threads and projects)
        self.entries: dict[str, tuple[tuple, list[dict]]] = {}  # long id -> (sig, entries)
        self.pending: dict[str, tuple[tuple, list[dict]]] = {}
        self.suggestions: dict[str, dict] = {}  # long id -> {cursor, status, items}
        self.partial_answers: dict[str, dict] = {}  # request id -> {question id: answer}
        self.done_requests: OrderedDict[str, dict] = OrderedDict()  # client_msg_id -> response
        self.last_device_at = 0.0
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
                self.refresh()
            except Exception as e:  # keep polling through T3 restarts
                log.warning("refresh failed: %s", e)

    def refresh(self) -> None:
        shell = self.client.shell_snapshot()
        finished = []
        first = self.shell is None
        touched = []
        with self.cond:
            changed = False
            new_threads = {t["id"]: t for t in shell["threads"] if not t.get("deletedAt")}
            for tid, t in new_threads.items():
                key = _wake_key(t)
                if self.wake_keys.get(tid) != key:
                    old = self.threads.get(tid)
                    self.seq += 1
                    self.thread_v[tid] = self.seq
                    self.wake_keys[tid] = key
                    changed = True
                    touched.append(tid)
                    if (old is not None and render.thread_state(old) == "working"
                            and render.thread_state(t) in ("done", "failed")):
                        finished.append(tid)
            for tid in set(self.threads) - set(new_threads):
                self.seq += 1
                self.thread_v[tid] = self.seq
                self.wake_keys.pop(tid, None)
                changed = True
            if changed or self.shell is None:
                self.list_v = self.seq
            self.shell = shell
            self.threads = new_threads
            for t in new_threads.values():
                self.by_short[render.short_id(t["id"])] = t["id"]
            for p in shell["projects"]:
                self.by_short[render.short_id(p["id"])] = p["id"]
            if changed:
                self.cond.notify_all()
        for tid in finished:
            if self._wants_suggestions(tid):
                self._schedule_suggestions(tid)
        self._prefetch(self._recent_per_project(3) if first else touched)

    def _top_level(self, t: dict) -> bool:
        return not (t.get("lineage") or {}).get("parentThreadId")

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

    def _entries(self, t: dict) -> list[dict]:
        sig = _signature(t)
        cached = self.entries.get(t["id"])
        if cached and cached[0] == sig:
            return cached[1]
        items = [e["item"] for e in self.client.thread_transcript(t["id"])["items"]]
        entries = render.render_items(items)
        self.entries[t["id"]] = (sig, entries)
        return entries

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
        groups: dict[str | None, list[dict]] = {}
        for t in self.threads.values():
            if (t.get("lineage") or {}).get("parentThreadId"):
                continue  # subagent and child threads: not shown yet
            groups.setdefault(t.get("projectId"), []).append(t)
        if project is not None:
            pid = self.by_short.get(project)
            groups = {pid: groups.get(pid, [])}
        out = []
        for pid, threads in groups.items():
            threads.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
            p = projects.get(pid) if pid else None
            out.append({
                "id": render.short_id(pid) if pid else None,
                "title": (p or {}).get("title") or "No project",
                "updated": threads[0].get("updatedAt") if threads else None,
                "total": len(threads),
                "threads": [self._thread_row(t) for t in threads[:limit]],
            })
        out.sort(key=lambda g: g["updated"] or "", reverse=True)
        return {"v": self.list_v, "projects": out}

    def _thread_row(self, t: dict) -> dict:
        cached = self.entries.get(t["id"])
        lvm = (t.get("latestVisibleMessage") or {}).get("text")
        prev = render.preview(cached[1]) if cached else (" ".join(lvm.split())[:80] if lvm else "")
        return {"id": render.short_id(t["id"]), "title": t.get("title") or "Untitled",
                "state": render.thread_state(t), "unread": render.is_unread(t),
                "updated": t.get("updatedAt"), "preview": prev}

    def projects(self) -> dict:
        shell = self.shell or {"projects": []}
        return {"projects": [{"id": render.short_id(p["id"]), "title": p.get("title")}
                             for p in shell["projects"] if not p.get("archivedAt")]}

    # ---- thread view --------------------------------------------------------

    def thread_view(self, short: str, after: int | None = None, before: int | None = None,
                    turns: int = 3, visit: bool = False) -> dict:
        self.last_device_at = time.monotonic()
        t = self._thread(short)
        entries = self._entries(t)
        state = render.thread_state(t)
        cursor = entries[-1]["o"] if entries else 0

        if after is not None:
            shown = [e for e in entries if e["o"] > after]
            more_before = False
        else:
            pool = [e for e in entries if before is None or e["o"] < before]
            you = [i for i, e in enumerate(pool) if e["role"] == "you"]
            start = you[-turns] if len(you) >= turns else 0
            shown = pool[start:]
            more_before = start > 0

        if state in ("done", "failed") and self._wants_suggestions(t["id"]):
            self._await_suggestions(t, cursor)

        if visit and render.is_unread(t):
            try:
                self.client.visit_thread(t["id"], _now_iso())
            except Exception as e:
                log.warning("visit failed: %s", e)

        pending_view, replies, typed = self._replies(t, state, cursor)
        return {
            "id": short, "title": t.get("title") or "Untitled", "state": state,
            "v": self.thread_v.get(t["id"], 0), "cursor": cursor,
            "more_before": more_before, "entries": shown,
            "pending": pending_view, "replies": replies, "typed": typed,
        }

    # ---- reply buttons ------------------------------------------------------

    def _replies(self, t: dict, state: str, cursor: int):
        """(pending description, buttons, what typed text does)."""
        if state == "needs_you":
            pending = self._pending(t)
            if pending:
                return self._pending_replies(pending[0])
            # The shell says something is pending but the projection doesn't show it yet.
            return {"kind": "unknown", "text": "Waiting for you on another device."}, [], "message"
        buttons = []
        if state == "working":
            buttons.append({"id": "c:stop", "label": "Stop", "kind": "shortcut"})
            return None, buttons, "message"
        sug = self.suggestions.get(t["id"])
        if sug and sug["cursor"] == cursor and sug["status"] == "ready":
            for i, text in enumerate(sug["items"]):
                buttons.append({"id": f"s:{cursor}:{i}", "label": text, "kind": "suggestion"})
        for key, (label, _) in SHORTCUTS.items():
            buttons.append({"id": f"c:{key}", "label": label, "kind": "shortcut"})
        return None, buttons, "message"

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
        return bool(self.suggester and t and not (t.get("lineage") or {}).get("parentThreadId"))

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
        with self.cond:
            cur = self.suggestions.get(tid)
            if cur and cur["cursor"] == cursor and not (
                    cur["status"] == "failed" and time.monotonic() - cur["at"] > SUGGESTION_RETRY):
                return cur
            slot = {"cursor": cursor, "status": "pending", "items": [], "done": threading.Event(),
                    "at": time.monotonic()}
            self.suggestions[tid] = slot
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
                self.seq += 1
                self.thread_v[tid] = self.seq
                self.cond.notify_all()

    def _await_suggestions(self, t: dict, cursor: int) -> None:
        slot = self._schedule_suggestions(t["id"])  # returns the existing slot if still good
        if slot and slot["status"] == "pending":
            slot["done"].wait(SUGGESTION_WAIT)

    # ---- waiting ------------------------------------------------------------

    def wait_thread(self, short: str, v: int, timeout: float) -> bool:
        """Block until the thread's version differs from `v`. True if it changed."""
        self.last_device_at = time.monotonic()
        tid = self.by_short.get(short)
        if tid is None:
            raise BridgeError(404, "no such thread")
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.thread_v.get(tid, 0) == v:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
                self.last_device_at = time.monotonic()
        return True

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
              client_msg_id: str | None = None) -> dict:
        self.last_device_at = time.monotonic()
        t = self._thread(short)

        def act():
            before = _wake_key(t)
            if reply_id:
                self._do_reply(t, reply_id, client_msg_id)
            elif text and text.strip():
                self._do_text(t, text.strip(), client_msg_id)
            else:
                raise BridgeError(400, "send `reply` or `text`")
            self._settle(t["id"], before)
            return self.thread_view(short)

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
        elif kind in ("a", "q"):
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
            _, qi, oi = rest.split(":")
            questions = (match["item"] or {}).get("questions", [])
            q = questions[int(qi)]
            label = q["options"][int(oi)]["label"]
            self._answer(t, match, q, label, cmid)
            return
        raise BridgeError(400, "unknown reply")

    def _do_text(self, t: dict, text: str, cmid: str | None) -> None:
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
                    self._answer(t, p, q, text, cmid)
                    return
        self.client.send_message(t["id"], text, command_id=_stable_id(cmid, "c"),
                                 message_id=_stable_id(cmid, "m"))

    def _answer(self, t: dict, p: dict, q: dict, answer: str, cmid: str | None) -> None:
        """Record one question's answer; submit once every question has one."""
        req = p["request"]
        capability = (req.get("responseCapability") or {}).get("type")
        if capability == "message":
            self.client.send_message(t["id"], answer, command_id=_stable_id(cmid, "c"),
                                     message_id=_stable_id(cmid, "m"))
            return
        answers = self.partial_answers.setdefault(req["id"], {})
        answers[q["id"]] = [answer] if q.get("multiSelect") else answer
        questions = (p["item"] or {}).get("questions", [])
        if all(x["id"] in answers for x in questions):
            self.client.respond_user_input(t["id"], req["id"], dict(answers),
                                           command_id=_stable_id(cmid, "c"))
            self.partial_answers.pop(req["id"], None)
        else:
            # Next question: the view changes without anything changing in T3.
            with self.cond:
                self.seq += 1
                self.thread_v[t["id"]] = self.seq
                self.cond.notify_all()

    def _stop_thread(self, t: dict, cmid: str | None) -> None:
        if self.stop_capability:
            self.client.stop_thread(t["id"], command_id=_stable_id(cmid, "c"))
        elif t.get("activeRunId"):
            self.client.interrupt_run(t["id"], t["activeRunId"], command_id=_stable_id(cmid, "c"))

    def new_thread_settings(self, pid: str, project: dict) -> tuple[dict, str]:
        """(model selection, runtime mode) for a new thread, from the user's T3 settings,
        the way the app picks them: the project's override, then the server-wide default.
        Read fresh each time so a change made on the desktop applies at once."""
        settings = (self.client.server_config().get("settings") or {})
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
            raise BridgeError(409, "no default model is set in T3")
        # T3's own default when the setting is absent.
        runtime = override.get("defaultRuntimeMode") or settings.get("defaultRuntimeMode") or "full-access"
        return model, runtime

    def start_thread(self, project: str, text: str, client_msg_id: str | None = None) -> dict:
        self.last_device_at = time.monotonic()
        pid = self.by_short.get(project)
        shell = self.shell or {"projects": []}
        p = next((x for x in shell["projects"] if x["id"] == pid), None)
        if p is None:
            raise BridgeError(404, "no such project")
        if not text.strip():
            raise BridgeError(400, "text is empty")

        def act():
            model, runtime = self.new_thread_settings(pid, p)
            title = " ".join(text.split())[:60]
            res = self.client.launch_thread(pid, title, text.strip(), model, runtime_mode=runtime,
                                            command_id=_stable_id(client_msg_id, "c"),
                                            message_id=_stable_id(client_msg_id, "m"),
                                            generate_title=True)
            self.refresh()
            return self.thread_view(render.short_id(res["threadId"]))

        return self._once(client_msg_id, act)
