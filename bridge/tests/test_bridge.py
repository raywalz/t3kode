"""Bridge tests against an in-memory fake T3. Run: python3 -m unittest discover -s tests (from bridge/)."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from t3bridge import render
from t3bridge.core import Bridge, BridgeError
from t3bridge.server import serve
from t3bridge.suggest import parse_suggestions


def item(o, type_, **kw):
    return {"ordinal": o, "type": type_, "status": kw.pop("status", "completed"), **kw}


def thread(tid, pid="p1", **kw):
    base = {"id": tid, "projectId": pid, "title": f"Thread {tid}", "status": "completed",
            "pendingRuntimeRequest": None, "itemCount": 0, "updatedAt": "2026-01-01T00:00:00Z",
            "lineage": {"parentThreadId": None}, "latestRunCompletedAt": "2026-01-01T00:00:00Z",
            "lastVisitedAt": "2026-01-02T00:00:00Z", "modelSelection": {"instanceId": "x", "model": "m"},
            "runtimeMode": "approval-required", "createdAt": "2026-01-01T00:00:00Z"}
    base.update(kw)
    return base


class FakeT3:
    def __init__(self):
        self.threads = {}
        self.items = {}
        self.projections = {}
        self.projects = [{"id": "p1", "title": "Alpha"}, {"id": "p2", "title": "Beta"}]
        self.calls = []

    settings = {"defaultRuntimeMode": "full-access",
                "defaultModelSelection": {"instanceId": "x", "model": "default-model"},
                "projectSettingsOverrides": {"p2": {"defaultRuntimeMode": "approval-required"}}}

    def server_config(self):
        return {"settings": self.settings}

    def environment(self):
        return {"serverVersion": "test", "orchestrationProtocolVersion": 2,
                "capabilities": {"threadStop": True}}

    def shell_snapshot(self):
        return {"threads": list(self.threads.values()), "projects": self.projects}

    def thread_transcript(self, tid):
        return {"items": [{"item": i} for i in self.items.get(tid, [])]}

    def thread_snapshot(self, tid):
        return {"projection": self.projections[tid]}

    def _record(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        return {"sequence": len(self.calls)}

    def send_message(self, *a, **kw):
        return self._record("send_message", *a, **kw)

    def respond_approval(self, *a, **kw):
        return self._record("respond_approval", *a, **kw)

    def respond_user_input(self, *a, **kw):
        return self._record("respond_user_input", *a, **kw)

    def stop_thread(self, *a, **kw):
        return self._record("stop_thread", *a, **kw)

    def interrupt_run(self, *a, **kw):
        return self._record("interrupt_run", *a, **kw)

    def visit_thread(self, *a, **kw):
        return self._record("visit_thread", *a, **kw)

    def launch_thread(self, pid, title, text, model, **kw):
        self._record("launch_thread", pid, title, text, model, **kw)
        tid = f"new-{len(self.threads)}"
        self.threads[tid] = thread(tid, pid, status="running")
        return {"threadId": tid}


class FakeSuggester:
    def __init__(self, items=("Yes, do it", "No, stop there"), delay=0.0):
        self.items = list(items)
        self.delay = delay
        self.calls = 0

    def suggest(self, entries):
        self.calls += 1
        time.sleep(self.delay)
        return self.items


CONVERSATION = [
    item(1000001, "user_message", text="First question", createdBy="user"),
    item(1000002, "command_execution", input="cd x && ssh root@box 'ls' | head"),
    item(1000003, "dynamic_tool", toolName="Edit", input={"file_path": "/a/b/c.py"}),
    item(1000004, "reasoning", text="hmm"),
    item(1000005, "assistant_message", text="**Done.** See `c.py`.", streaming=False),
    item(1000006, "checkpoint"),
    item(2000001, "user_message", text="Second", createdBy="user"),
    item(2000002, "assistant_message", text="Answer two", streaming=False),
]


class MarkdownTests(unittest.TestCase):
    def test_blocks_and_inline(self):
        from t3bridge.markdown import to_html
        out = to_html("## Title\nSome **bold** and *it* `a<b`\n\n- one\n  - two\n\n"
                      "| A | B |\n|---|---|\n| 1 | 2 |\n\n```\nx < y\n```")
        self.assertEqual(out, "<h4>Title</h4><p>Some <b>bold</b> and <i>it</i> <code>a&lt;b</code></p>"
                         "<ul><li>one<ul><li>two</li></ul></li></ul>"
                         "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
                         "<pre>x &lt; y</pre>")

    def test_entries_carry_html(self):
        e = render.render_items([item(1, "assistant_message", text="**Hi** <there>", streaming=False)])
        self.assertEqual(e[0]["html"], "<p><b>Hi</b> &lt;there&gt;</p>")
        self.assertEqual(e[0]["text"], "Hi <there>")


def run_item(o, run, type_, start, end, **kw):
    return item(o, type_, runId=run, startedAt=f"2026-01-01T00:{start}Z", completedAt=f"2026-01-01T00:{end}Z", **kw)


TWO_RUNS = [
    run_item(1, "r1", "user_message", "00:00", "00:00", text="Do it", createdBy="user"),
    run_item(2, "r1", "command_execution", "00:01", "00:05", input="make"),
    run_item(3, "r1", "assistant_message", "00:05", "00:06", text="Built. Testing next.", streaming=False),
    run_item(4, "r1", "command_execution", "00:06", "01:10", input="pytest"),
    run_item(5, "r1", "assistant_message", "01:11", "01:12", text="All done.", streaming=False),
    run_item(6, "r2", "user_message", "02:00", "02:00", text="Again", createdBy="user"),
    run_item(7, "r2", "command_execution", "02:01", "02:30", input="make"),
]


class FoldTests(unittest.TestCase):
    def test_finished_run_folds_to_worked_for(self):
        entries = render.render_items(render.fold_runs(TWO_RUNS, live_run="r2"))
        self.assertEqual([(e["role"], e["text"]) for e in entries], [
            ("you", "Do it"), ("fold", "▸ Worked for 1m 12s"), ("agent", "All done."),
            ("you", "Again"), ("tool", "▸ ran make")])
        self.assertIn('href="t3:run:', entries[1]["html"])

    def test_expanded_run_shows_every_step(self):
        r1 = render.short_id("r1")
        entries = render.render_items(render.fold_runs(TWO_RUNS, live_run="r2", expanded=frozenset({r1})))
        self.assertEqual([e["role"] for e in entries[:6]], ["you", "fold", "tool", "agent", "tool", "agent"])
        self.assertTrue(entries[1]["expanded"])

    def test_stopped_runs_stay_open(self):
        items = TWO_RUNS[:4] + [run_item(5, "r1", "run_interrupt_result", "01:00", "01:00", message="Stopped")]
        roles = [e["role"] for e in render.render_items(render.fold_runs(items))]
        self.assertNotIn("fold", roles)


class RenderTests(unittest.TestCase):
    def test_collapses_tools_and_cleans_text(self):
        entries = render.render_items(CONVERSATION)
        self.assertEqual([e["role"] for e in entries], ["you", "tool", "agent", "you", "agent"])
        self.assertEqual(entries[1]["text"], "▸ ran ssh box · edited c.py")
        self.assertEqual(entries[2]["text"], "Done. See c.py.")

    def test_command_programs(self):
        cases = {
            "python3 - <<'EOF'\nimport json\nEOF\ngit status": ["python3", "git"],
            "FOO=1 sudo timeout 30 make test && npm run x | tail": ["make", "npm"],
            "for f in a b; do grep x $f; done": ["grep"],
            "echo 'unterminated": [],
        }
        for cmd, want in cases.items():
            self.assertEqual(render.command_programs(cmd), want, cmd)

    def test_streaming_and_unknown_items(self):
        entries = render.render_items([
            item(1, "assistant_message", text="partial", streaming=True),
            item(2, "brand_new_kind"),
            item(3, "command_execution", input="make", status="failed"),
        ])
        self.assertEqual([(e["o"], e["role"], e["text"]) for e in entries],
                         [(3, "tool", "▸ brand new kind · ran make (1 failed)")])

    def test_question_note_shows_answer(self):
        e = render.render_items([item(1, "user_input_request", requestId="r",
                                      questions=[{"id": "q", "question": "Red or blue?"}],
                                      questionAnswer={"answers": {"q": "Red"}})])
        self.assertEqual(e[0]["text"], "Q: Red or blue? → Red")

    def test_states(self):
        self.assertEqual(render.thread_state(thread("a", pendingRuntimeRequest={"id": "r"})), "needs_you")
        self.assertEqual(render.thread_state(thread("a", status="waiting")), "working")
        self.assertEqual(render.thread_state(thread("a", status="failed")), "failed")
        self.assertTrue(render.is_unread(thread("a", lastVisitedAt=None)))

    def test_parse_suggestions(self):
        self.assertEqual(parse_suggestions('Sure:\n```json\n["A", "B", "A", 3]\n```', 3), ["A", "B"])
        self.assertEqual(parse_suggestions("no json here", 3), [])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.t3 = FakeT3()
        self.t3.threads["t1"] = thread("t1", itemCount=8, updatedAt="2026-01-03T00:00:00Z")
        self.t3.items["t1"] = list(CONVERSATION)
        self.t3.threads["t2"] = thread("t2", "p2", status="running", updatedAt="2026-01-04T00:00:00Z")
        self.t3.threads["child"] = thread("child", lineage={"parentThreadId": "t1"})
        self.sug = FakeSuggester()
        self.b = Bridge(self.t3, self.sug)
        self.b.settle_delay = 0
        self.b.refresh()
        self.s1 = render.short_id("t1")
        self.s2 = render.short_id("t2")

    def tearDown(self):
        self.b.close()

    def test_list_groups_by_project_and_hides_children(self):
        out = self.b.thread_list()
        self.assertEqual([p["title"] for p in out["projects"]], ["Beta", "Alpha"])
        ids = [t["id"] for p in out["projects"] for t in p["threads"]]
        self.assertNotIn(render.short_id("child"), ids)
        self.assertEqual(out["projects"][0]["threads"][0]["state"], "working")

    def test_view_window_and_after(self):
        v = self.b.thread_view(self.s1, turns=1)
        self.assertEqual([e["text"] for e in v["entries"]], ["Second", "Answer two"])
        self.assertTrue(v["more_before"])
        self.assertEqual(v["cursor"], 2000002)
        self.assertEqual(self.b.thread_view(self.s1, after=1000005)["entries"][0]["text"], "Second")
        older = self.b.thread_view(self.s1, before=2000001, turns=1)
        self.assertEqual(older["entries"][0]["text"], "First question")

    def test_done_thread_gets_suggestions_then_shortcuts(self):
        v = self.b.thread_view(self.s1)
        labels = [r["label"] for r in v["replies"]]
        self.assertEqual(labels, ["Yes, do it", "No, stop there", "Continue", "Summarize where we are"])
        self.assertEqual(v["typed"], "message")

    def test_working_thread_offers_stop(self):
        v = self.b.thread_view(self.s2)
        self.assertEqual([r["id"] for r in v["replies"]], ["c:stop"])
        self.b.reply(self.s2, reply_id="c:stop")
        self.assertEqual(self.t3.calls[-1][0], "stop_thread")

    def test_suggestion_reply_sends_text_and_rejects_stale(self):
        v = self.b.thread_view(self.s1)
        sid = v["replies"][0]["id"]
        self.b.reply(self.s1, reply_id=sid)
        self.assertEqual(self.t3.calls[-1][:2], ("send_message", ("t1", "Yes, do it")))
        with self.assertRaises(BridgeError) as cm:
            self.b.reply(self.s1, reply_id="s:999:0")
        self.assertEqual(cm.exception.status, 409)

    def test_retry_with_same_client_msg_id_sends_once(self):
        self.b.reply(self.s1, text="hello", client_msg_id="abc")
        self.b.reply(self.s1, text="hello", client_msg_id="abc")
        sends = [c for c in self.t3.calls if c[0] == "send_message"]
        self.assertEqual(len(sends), 1)
        self.assertIsNotNone(sends[0][2]["command_id"])

    def _make_pending(self, kind, item_):
        self.t3.threads["t1"] = thread("t1", itemCount=9, status="waiting",
                                       pendingRuntimeRequest={"id": "req1", "kind": kind})
        self.t3.projections["t1"] = {
            "runtimeRequests": [{"id": "req1", "kind": kind, "status": "pending",
                                 "responseCapability": {"type": "live"}}],
            "turnItems": [item_],
        }
        self.b.refresh()

    def test_approval_buttons(self):
        self._make_pending("command", item(9, "approval_request", requestId="req1", prompt="rm -rf build"))
        v = self.b.thread_view(self.s1)
        self.assertEqual(v["pending"]["text"], "rm -rf build")
        self.assertEqual([r["label"] for r in v["replies"]], ["Allow", "Allow for session", "Decline"])
        self.b.reply(self.s1, reply_id=v["replies"][2]["id"])
        self.assertEqual(self.t3.calls[-1][:2], ("respond_approval", ("t1", "req1", "decline")))

    def test_multi_question_answers_submit_together(self):
        questions = [
            {"id": "q1", "header": "Color", "question": "Red or blue?", "options": [{"label": "Red"}, {"label": "Blue"}]},
            {"id": "q2", "header": "Size", "question": "Big or small?", "options": [{"label": "Big"}, {"label": "Small"}]},
        ]
        self._make_pending("user_input", item(9, "user_input_request", requestId="req1", questions=questions))
        v = self.b.thread_view(self.s1)
        self.assertEqual((v["pending"]["text"], v["pending"]["step"]), ("Red or blue?", "1/2"))
        self.assertEqual(v["typed"], "answer")
        v = self.b.reply(self.s1, reply_id=v["replies"][1]["id"])  # Blue
        self.assertEqual(v["pending"]["text"], "Big or small?")
        self.assertFalse([c for c in self.t3.calls if c[0] == "respond_user_input"])
        self.b.reply(self.s1, text="Medium, please")  # typed custom answer
        self.assertEqual(self.t3.calls[-1][:2],
                         ("respond_user_input", ("t1", "req1", {"q1": "Blue", "q2": "Medium, please"})))

    def test_wait_wakes_on_change(self):
        v = self.b.thread_view(self.s2)["v"]
        result = {}

        def waiter():
            result["changed"] = self.b.wait_thread(self.s2, v, timeout=5)

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.1)
        self.t3.threads["t2"] = thread("t2", "p2", status="completed", itemCount=3)
        self.b.refresh()
        th.join(2)
        self.assertTrue(result.get("changed"))
        self.assertFalse(self.b.wait_thread(self.s2, self.b.thread_v["t2"], timeout=0.05))

    def test_tool_progress_while_working_does_not_wake(self):
        v = self.b.thread_v["t2"]
        self.t3.threads["t2"] = thread("t2", "p2", status="running", itemCount=40,
                                       updatedAt="2026-01-05T00:00:00Z")
        self.b.refresh()
        self.assertEqual(self.b.thread_v["t2"], v)

    def test_progress_mode_wakes_on_tool_calls(self):
        pv = self.b.thread_view(self.s2, progress=True)["v"]
        self.t3.threads["t2"] = thread("t2", "p2", status="running", itemCount=41,
                                       updatedAt="2026-01-06T00:00:00Z")
        self.b.refresh()
        self.assertTrue(self.b.wait_thread(self.s2, pv, timeout=0.05, progress=True))
        v = self.b.thread_view(self.s2)["v"]
        self.assertFalse(self.b.wait_thread(self.s2, v, timeout=0.05))

    def test_failed_suggestions_are_retried_later(self):
        from t3bridge import core
        self.sug.items = []  # model returns nothing usable
        self.b.thread_view(self.s1)
        self.assertEqual(self.b.suggestions["t1"]["status"], "ready")  # fake returns [] without raising
        self.b.suggestions["t1"]["status"] = "failed"
        self.b.suggestions["t1"]["at"] -= core.SUGGESTION_RETRY + 1
        self.sug.items = ["Retry worked"]
        v = self.b.thread_view(self.s1)
        self.assertEqual(v["replies"][0]["label"], "Retry worked")

    def test_finished_turn_triggers_suggestions(self):
        self.t3.items["t2"] = [item(1, "user_message", text="go", createdBy="user"),
                               item(2, "assistant_message", text="done", streaming=False)]
        self.t3.threads["t2"] = thread("t2", "p2", status="completed", itemCount=2)
        self.b.refresh()
        self.b.suggestions["t2"]["done"].wait(2)
        self.assertEqual(self.b.suggestions["t2"]["status"], "ready")

    def test_start_thread_uses_t3_settings(self):
        v = self.b.start_thread(render.short_id("p1"), "Fix the login bug", client_msg_id="n1")
        name, args, kw = self.t3.calls[-1]
        self.assertEqual((name, args[0], args[2]), ("launch_thread", "p1", "Fix the login bug"))
        self.assertEqual((args[3]["model"], kw["runtime_mode"]), ("default-model", "full-access"))
        self.assertEqual(v["state"], "working")
        # A project override wins over the server-wide default.
        self.b.start_thread(render.short_id("p2"), "Other project", client_msg_id="n2")
        self.assertEqual(self.t3.calls[-1][2]["runtime_mode"], "approval-required")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.t3 = FakeT3()
        self.t3.threads["t1"] = thread("t1", itemCount=8)
        self.t3.items["t1"] = list(CONVERSATION)
        self.b = Bridge(self.t3, None)
        self.b.settle_delay = 0
        self.b.refresh()
        self.httpd = serve(self.b, "127.0.0.1", 0, "secret")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.b.close()

    def req(self, path, body=None, token="secret"):
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST")
        if token:
            r.add_header("authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_auth_and_routes(self):
        self.assertEqual(self.req("/v1/threads", token=None)[0], 401)
        self.assertEqual(self.req("/v1/threads", token="wrong")[0], 401)
        status, body = self.req("/v1/threads")
        self.assertEqual(status, 200)
        sid = body["projects"][0]["threads"][0]["id"]
        status, view = self.req(f"/v1/threads/{sid}")
        self.assertEqual((status, view["replies"][0]["label"]), (200, "Continue"))
        status, view = self.req(f"/v1/threads/{sid}/reply", {"reply": "c:continue", "client_msg_id": "x1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.t3.calls[-1][1], ("t1", "Continue."))
        self.assertEqual(self.req("/v1/threads/nope")[0], 404)
        self.assertEqual(self.req(f"/v1/threads/{sid}/reply", {})[0], 400)
        status, body = self.req(f"/v1/threads/{sid}/wait?v={view['v']}&timeout=0")
        self.assertEqual((status, body["changed"]), (200, False))


if __name__ == "__main__":
    unittest.main()
