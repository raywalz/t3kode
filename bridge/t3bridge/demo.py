"""Made-up T3 data for `python3 -m t3bridge --demo`: try the plugin, or take
screenshots, without a T3 server. Replies get a canned answer after a short pause."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCREENS = Path(__file__).parent / "demo_screens"

NOW = datetime.now(timezone.utc)


def _ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run(run, start_min, items):
    """Turn (type, fields) pairs into turn items for one run, a few seconds apart."""
    out = []
    for n, (kind, fields) in enumerate(items):
        t = start_min - n * 0.25
        out.append({"type": kind, "runId": run, "status": "completed", "startedAt": _ts(t),
                    "completedAt": _ts(t - 0.2), "createdBy": "user", **fields})
    return out


LOGIN_FIX = """The test was flaky because it raced the session cookie. **Fixed** in `auth/session.py`:

- The cookie now gets its expiry from the server clock, not the browser's.
- The test waits for the redirect instead of sleeping for 2 seconds.

| Run | Before | After |
|---|---|---|
| 50 local runs | 7 failures | 0 failures |
| CI (3 runs) | 1 failure | 0 failures |

```python
expires = server_now() + SESSION_TTL
```

Want me to open a pull request?"""

DEPS = """Most upgrades were clean. Two need a decision:

1. **Charting library 4 → 5** drops the old legend API we use on the dashboard.
2. **Test runner 2 → 3** changes snapshot paths, so every snapshot gets rewritten.

Everything else is updated and the tests pass."""


def _threads():
    t = []

    def thread(tid, project, title, status, minutes_ago, items, **extra):
        t.append(({"id": tid, "projectId": project, "title": title, "status": status,
                   "createdBy": "user", "createdAt": _ts(minutes_ago + 30), "updatedAt": _ts(minutes_ago),
                   "latestRunCompletedAt": _ts(minutes_ago) if status != "running" else None,
                   "lastVisitedAt": _ts(minutes_ago + 1), "itemCount": len(items),
                   "pendingRuntimeRequest": None, "lineage": {"parentThreadId": None},
                   "modelSelection": {"instanceId": "demo", "model": "demo"},
                   "runtimeMode": "approval-required", "activeRunId": None, **extra}, items))

    thread("login", "app", "Fix the flaky login test", "completed", 12, _run("r1", 20, [
        ("user_message", {"text": "The login test fails about one run in ten. Can you find out why?"}),
        ("command_execution", {"input": "npm test -- login --repeat 50"}),
        ("dynamic_tool", {"toolName": "Read", "input": {"file_path": "auth/session.py"}}),
        ("assistant_message", {"text": "It's a race with the session cookie. Fixing it now.", "streaming": False}),
        ("file_change", {"fileName": "auth/session.py"}),
        ("file_change", {"fileName": "tests/login.spec.ts"}),
        ("command_execution", {"input": "npm test -- login --repeat 50"}),
        ("assistant_message", {"text": LOGIN_FIX, "streaming": False}),
    ]))
    dark = _run("r2", 8, [
        ("user_message", {"text": "Add a dark mode toggle to the settings page."}),
        ("dynamic_tool", {"toolName": "Read", "input": {"file_path": "src/settings/Page.tsx"}}),
        ("assistant_message", {"text": "The theme lives in one CSS file, so this is small. "
                                       "I need to edit the stylesheet.", "streaming": False}),
        ("approval_request", {"requestId": "req-dark", "requestKind": "file-change",
                              "prompt": "src/styles/theme.css", "status": "waiting"}),
    ])
    thread("dark", "app", "Add a dark mode toggle", "waiting", 3, dark,
           pendingRuntimeRequest={"id": "req-dark", "kind": "file-change"}, activeRunId="r2")
    thread("deps", "app", "Upgrade dependencies", "completed", 45, _run("r3", 60, [
        ("user_message", {"text": "Upgrade all dependencies and tell me what breaks."}),
        ("command_execution", {"input": "npm outdated"}),
        ("command_execution", {"input": "npm install"}),
        ("command_execution", {"input": "npm test"}),
        ("assistant_message", {"text": DEPS, "streaming": False}),
    ]))
    thread("docs", "app", "Write the API reference", "running", 1, _run("r4", 4, [
        ("user_message", {"text": "Write reference docs for the public API."}),
        ("dynamic_tool", {"toolName": "Grep", "input": {"pattern": "export function"}}),
        ("subagent", {"title": "Document the auth module", "childThreadId": "docs-auth",
                      "status": "running", "prompt": "Document every exported function in auth/."}),
    ]), activeRunId="r4")
    thread("docs-auth", "app", "Document the auth module", "running", 1, _run("r5", 3, [
        ("user_message", {"text": "Document every exported function in auth/.", "createdBy": "agent"}),
        ("dynamic_tool", {"toolName": "Read", "input": {"file_path": "auth/index.ts"}}),
    ]), createdBy="agent", lineage={"parentThreadId": "docs", "relationshipToParent": "subagent"},
           activeRunId="r5")
    thread("blog", "site", "Draft the launch blog post", "completed", 300, _run("r6", 320, [
        ("user_message", {"text": "Draft a short launch post."}),
        ("assistant_message", {"text": "Here's a draft in `posts/launch.md`. It's about 400 words, "
                                       "with a screenshot placeholder at the top.", "streaming": False}),
    ]))
    thread("seo", "site", "Fix broken links", "completed", 1500, settledOverride="settled", items=_run("r7", 1520, [
        ("user_message", {"text": "Find and fix broken links."}),
        ("command_execution", {"input": "npx linkinator ./public --recurse"}),
        ("assistant_message", {"text": "Fixed 4 broken links. Two pointed at an old docs domain.",
                               "streaming": False}),
    ]))
    return t


class DemoT3:
    """Stands in for T3Client with in-memory data."""

    token = "demo"

    def __init__(self):
        self.lock = threading.Lock()
        self.threads = {}
        self.items = {}
        self.ordinals = {}
        for th, items in _threads():
            for n, it in enumerate(items):
                it.setdefault("ordinal", 1_000_000 + n + 1)
            self.threads[th["id"]] = th
            self.items[th["id"]] = items
        self.projects = [{"id": "app", "title": "my-app", "defaultModelSelection": None},
                         {"id": "site", "title": "website", "defaultModelSelection": None}]

    # ---- reads ----
    def environment(self):
        return {"serverVersion": "demo", "orchestrationProtocolVersion": 2,
                "capabilities": {"threadStop": True}}

    def auth_session(self):
        return {"authenticated": True, "expiresAt": _ts(-60 * 24 * 365)}

    def server_config(self):
        return {"settings": {"defaultRuntimeMode": "approval-required",
                             "defaultModelSelection": {"instanceId": "demo", "model": "demo"},
                             "favorites": [{"provider": "demo", "model": "demo"}]},
                "providers": [{"instanceId": "demo", "displayName": "Demo", "status": "ready",
                               "models": [{"slug": "demo", "name": "Demo model"},
                                          {"slug": "demo-fast", "name": "Demo fast"}]}]}

    def preview_tabs(self, tid):
        return []

    def devices(self):
        """A simulator and an emulator, both open in the dark mode thread."""
        devs = [{"hostId": "local", "id": "demo-ios", "platform": "ios", "name": "iPhone 17 Pro",
                 "version": "iOS 26.5", "booted": True, "physical": False},
                {"hostId": "local", "id": "demo-android", "platform": "android", "name": "Pixel 9",
                 "version": "Android 15.0", "booted": True, "physical": False}]
        return {"devices": devs, "sessions": [
            {"threadId": "dark", "hostId": d["hostId"], "deviceId": d["id"], "platform": d["platform"],
             "openedAt": _ts(2)} for d in devs]}

    def device_screenshot(self, host_id, device_id, platform):
        return (SCREENS / f"{platform}.png").read_bytes()

    def set_model(self, tid, sel, **_kw):
        with self.lock:
            self.threads[tid]["modelSelection"] = sel
        return {"sequence": 1}

    def shell_snapshot(self):
        with self.lock:
            return {"threads": [dict(t) for t in self.threads.values()], "projects": self.projects}

    def thread_transcript(self, tid):
        with self.lock:
            return {"items": [{"item": dict(i)} for i in self.items.get(tid, [])]}

    def thread_snapshot(self, tid):
        with self.lock:
            t = self.threads[tid]
            reqs = []
            if t.get("pendingRuntimeRequest"):
                r = t["pendingRuntimeRequest"]
                reqs.append({"id": r["id"], "kind": r["kind"], "status": "pending",
                             "responseCapability": {"type": "live"}})
            return {"projection": {"runtimeRequests": reqs,
                                   "turnItems": [dict(i) for i in self.items.get(tid, [])]}}

    # ---- writes ----
    def _add(self, tid, kind, **fields):
        items = self.items.setdefault(tid, [])
        o = (items[-1]["ordinal"] if items else 1_000_000) + 1
        run = self.threads[tid].get("activeRunId") or f"run-{o}"
        items.append({"type": kind, "ordinal": o, "runId": run, "status": "completed",
                      "startedAt": _ts(0), "completedAt": _ts(0), "createdBy": "user", **fields})
        t = self.threads[tid]
        t["itemCount"] = len(items)
        t["updatedAt"] = _ts(0)

    def _work_then_reply(self, tid, reply):
        def go():
            time.sleep(2)
            with self.lock:
                self._add(tid, "command_execution", input="npm test")
            time.sleep(2)
            with self.lock:
                self._add(tid, "assistant_message", text=reply, streaming=False)
                t = self.threads[tid]
                t.update(status="completed", activeRunId=None, latestRunCompletedAt=_ts(0))
        threading.Thread(target=go, daemon=True).start()

    def send_message(self, tid, text, **_kw):
        with self.lock:
            t = self.threads[tid]
            t.update(status="running", activeRunId=f"run-{uuid.uuid4().hex[:6]}", pendingRuntimeRequest=None)
            self._add(tid, "user_message", text=text)
        self._work_then_reply(tid, "Done. (This is the demo bridge, so this reply is canned.)")
        return {"sequence": 1}

    def respond_approval(self, tid, request_id, decision, **_kw):
        with self.lock:
            t = self.threads[tid]
            t.update(pendingRuntimeRequest=None, status="running")
            for it in self.items[tid]:
                if it.get("requestId") == request_id:
                    it["status"] = "completed"
        ok = decision.startswith("accept")
        self._work_then_reply(tid, "Dark mode is in: there's a toggle under Settings → Appearance."
                              if ok else "OK, I left the stylesheet alone.")
        return {"sequence": 1}

    def respond_user_input(self, tid, request_id, answers, **_kw):
        return self.respond_approval(tid, request_id, "accept")

    def settle_thread(self, tid):
        with self.lock:
            self.threads[tid]["settledOverride"] = "settled"
        return {"sequence": 1}

    def unsettle_thread(self, tid):
        with self.lock:
            self.threads[tid]["settledOverride"] = "active"
        return {"sequence": 1}

    def stop_thread(self, tid, **_kw):
        with self.lock:
            self.threads[tid].update(status="interrupted", activeRunId=None, pendingRuntimeRequest=None)
        return {"sequence": 1}

    def interrupt_run(self, tid, run_id, **_kw):
        return self.stop_thread(tid)

    def visit_thread(self, tid, visited_at):
        with self.lock:
            self.threads[tid]["lastVisitedAt"] = visited_at
        return {"sequence": 1}

    def launch_thread(self, project_id, title, text, model, **_kw):
        tid = f"new-{uuid.uuid4().hex[:6]}"
        with self.lock:
            self.threads[tid] = {"id": tid, "projectId": project_id, "title": title, "status": "running",
                                 "createdBy": "user", "createdAt": _ts(0), "updatedAt": _ts(0),
                                 "latestRunCompletedAt": None, "lastVisitedAt": _ts(0), "itemCount": 0,
                                 "pendingRuntimeRequest": None, "lineage": {"parentThreadId": None},
                                 "activeRunId": "run-new", "runtimeMode": "approval-required",
                                 "modelSelection": model}
            self._add(tid, "user_message", text=text)
        self._work_then_reply(tid, "Started on it. (Demo bridge: this reply is canned.)")
        return {"threadId": tid}

    def set_token(self, token):
        pass

    def close(self):
        pass
