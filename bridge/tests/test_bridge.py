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

    def vcs_status(self, cwd):
        return {"isRepo": cwd != "/not-git", "refName": "main"}

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

    def answer_secret(self, tid, item_id, answer):
        if getattr(self, "secret_error", None):
            raise RuntimeError(self.secret_error)
        for it in self.items.get(tid, []):
            if it.get("id") == item_id:
                it["secretStatus"] = "saved" if answer["type"] == "save" else "declined"
        self.threads[tid] = dict(self.threads[tid], pendingRuntimeRequest=None, status="running",
                                 itemCount=self.threads[tid]["itemCount"] + 1)
        return self._record("answer_secret", tid, item_id, answer)

    def settle_thread(self, tid):
        self.threads[tid] = dict(self.threads[tid], settledOverride="settled")
        return self._record("settle_thread", tid)

    def unsettle_thread(self, tid):
        self.threads[tid] = dict(self.threads[tid], settledOverride="active")
        return self._record("unsettle_thread", tid)

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

    def test_images_become_placeholders(self):
        from t3bridge.markdown import to_html
        self.assertEqual(to_html("![tile lattice](/tmp/x.png)"), "<p><i>[image: tile lattice]</i></p>")

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

    def test_open_question_is_left_to_the_pending_box(self):
        q = [{"id": "q", "question": "Red or blue?"}]
        self.assertEqual(render.render_items([item(1, "user_input_request", status="running",
                                                   requestId="r", questions=q)]), [])

    def test_states(self):
        self.assertEqual(render.thread_state(thread("a", pendingRuntimeRequest={"id": "r"})), "needs_you")
        self.assertEqual(render.thread_state(thread("a", status="waiting")), "working")
        self.assertEqual(render.thread_state(thread("a", status="failed")), "failed")
        self.assertTrue(render.is_unread(thread("a", lastVisitedAt=None)))

    def test_parse_suggestions(self):
        self.assertEqual(parse_suggestions('Sure:\n```json\n["A", "B", "A", 3]\n```', 3), ["A", "B"])
        self.assertEqual(parse_suggestions("no json here", 3), [])

    def test_agent_written_replies_are_split_off(self):
        text = 'Ready to ship.\n\n<!-- replies: ["Go", "Wait, run tests first"] -->\n'
        self.assertEqual(render.split_replies(text), ("Ready to ship.", ["Go", "Wait, run tests first"]))
        quoted = 'Use `<!-- replies: ["a"] -->` at the end.\n\nThat is all.'
        self.assertEqual(render.split_replies(quoted), (quoted, None))  # only a trailing one counts
        self.assertEqual(render.split_replies('Ok?\n<!-- ["Yes", "No"] -->'), ("Ok?", ["Yes", "No"]))
        self.assertEqual(render.split_replies("Ok?\n<!-- note to self -->")[1], None)
        e = render.render_items([item(1, "assistant_message", text=text, streaming=False)])
        self.assertEqual((e[0]["text"], e[0]["replies"]), ("Ready to ship.", ["Go", "Wait, run tests first"]))
        self.assertNotIn("replies", e[0]["html"])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.t3 = FakeT3()
        self.t3.threads["t1"] = thread("t1", itemCount=8, updatedAt="2026-01-03T00:00:00Z")
        self.t3.items["t1"] = list(CONVERSATION)
        self.t3.threads["t2"] = thread("t2", "p2", status="running", updatedAt="2026-01-04T00:00:00Z")
        self.t3.threads["boss"] = thread("boss", status="running", itemCount=2)
        self.t3.items["boss"] = [
            item(1, "user_message", runId="r9", text="Delegate it", createdBy="user"),
            item(2, "subagent", runId="r9", title="Research the API", childThreadId="child",
                 status="running", prompt="Look things up")]
        self.t3.threads["child"] = thread("child", createdBy="agent", status="running",
                                          lineage={"parentThreadId": "boss", "relationshipToParent": "subagent"})
        self.t3.threads["fork"] = thread("fork", createdBy="user",
                                         lineage={"parentThreadId": "t1", "relationshipToParent": "fork"})
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

    def test_subagents_nest_under_parent_and_forks_list(self):
        rows = {t["id"]: t for p in self.b.thread_list()["projects"] for t in p["threads"]}
        self.assertNotIn(render.short_id("child"), rows)
        self.assertIn(render.short_id("fork"), rows)
        self.assertEqual(rows[render.short_id("boss")]["agents"], {"total": 1, "needs_you": 0, "working": 1})
        self.assertNotIn("agents", rows[self.s1])

    def test_subagent_line_links_to_child_and_back(self):
        boss = render.short_id("boss")
        v = self.b.thread_view(boss)
        line = v["entries"][-1]
        self.assertEqual((line["role"], line["text"], line["thread"]),
                         ("subagent", "Agent: Research the API · working", render.short_id("child")))
        self.assertIn('href="t3:thread:', line["html"])
        child = self.b.thread_view(render.short_id("child"))
        self.assertEqual(child["parent"], {"id": boss, "title": "Thread boss"})
        self.assertIsNone(self.b.thread_view(self.s1)["parent"])

    def test_child_change_wakes_parent_view(self):
        boss = render.short_id("boss")
        v = self.b.thread_view(boss, progress=True)["v"]
        self.t3.threads["child"] = dict(self.t3.threads["child"], status="completed", itemCount=5)
        self.b.refresh()
        changed, view = self.b.wait_view(boss, v, timeout=0.5, progress=True)
        self.assertTrue(changed)
        self.assertEqual(view["entries"][-1]["text"], "Agent: Research the API · done")

    def test_settle_moves_thread_out_of_main_list(self):
        def rows(**kw):
            return {t["id"]: t for p in self.b.thread_list(**kw)["projects"] for t in p["threads"]}
        v = self.b.thread_list()["v"]
        self.assertEqual(self.b.settle(self.s1), {"ok": True, "settled": True})
        self.assertNotIn(self.s1, rows())
        self.assertTrue(self.b.wait_list(v, timeout=0.05))  # the list changed
        alpha = next(p for p in self.b.thread_list()["projects"] if p["title"] == "Alpha")
        self.assertEqual(alpha["settled"], 1)
        self.assertTrue(rows(project=alpha["id"])[self.s1]["settled"])  # still in its project
        self.b.settle(self.s1, settled=False)
        self.assertIn(self.s1, rows())

    def test_settled_thread_that_needs_you_stays_listed(self):
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], settledOverride="settled",
                                     pendingRuntimeRequest={"id": "r"})
        self.b.refresh()
        rows = {t["id"] for p in self.b.thread_list()["projects"] for t in p["threads"]}
        self.assertIn(self.s1, rows)

    def test_huge_thread_falls_back_to_recent_items(self):
        from t3bridge.t3client import T3Error

        def too_large(tid):
            raise T3Error('HTTP 400: {"reason":"thread_transcript_too_large"}', status=400)

        self.t3.thread_transcript = too_large
        self.t3.thread_bounded = lambda tid: {"projection": {"visibleTurnItems": [
            {"item": i} for i in CONVERSATION[-2:]]}}
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=99)  # not the cached version
        self.b.refresh()
        v = self.b.thread_view(self.s1)
        self.assertEqual([e["text"] for e in v["entries"]], ["Second", "Answer two"])

    def _ask_secret(self):
        self.t3.items["t1"] = list(CONVERSATION) + [item(3000001, "secret_request", id="sec-1", status="waiting",
            secretStatus="pending", label="API key", reason="Needed for the deploy, see https://example.com/keys.",
            delivery="file")]
        self.t3.threads["t1"] = thread("t1", status="running", itemCount=9,
                                       pendingRuntimeRequest={"id": "sec-1", "kind": "user_input"})
        self.b.refresh()

    def test_secret_request_view(self):
        self._ask_secret()
        v = self.b.thread_view(self.s1)
        self.assertEqual((v["state"], v["typed"]), ("needs_you", "none"))  # no typing into chat
        self.assertEqual(v["pending"]["header"], "API key")
        self.assertEqual(v["pending"]["link"], "https://example.com/keys")
        self.assertIn("private file", v["pending"]["note"])  # delivery: file
        self.assertEqual([r["label"] for r in v["replies"]], ["Enter secret", "Decline", "Stop"])
        self.assertNotIn("secret", " ".join(e["text"].lower() for e in v["entries"]))  # box only

    def test_secret_save_and_decline(self):
        self._ask_secret()
        v = self.b.answer_secret(self.s1, "  hunter2  ")
        self.assertEqual(self.t3.calls[-1][1], ("t1", "sec-1", {"type": "save", "secret": "hunter2"}))
        self.assertIn("Secret saved: API key", [e["text"] for e in v["entries"]])
        self._ask_secret()
        decline = self.b.thread_view(self.s1)["replies"][1]["id"]
        self.b.reply(self.s1, reply_id=decline)
        self.assertEqual(self.t3.calls[-1][1], ("t1", "sec-1", {"type": "decline"}))

    def test_secret_errors_never_echo_the_value(self):
        self._ask_secret()
        self.t3.secret_error = 'failed: {"reason": "already_answered", "secret": "hunter2"}'
        with self.assertRaises(BridgeError) as cm:
            self.b.answer_secret(self.s1, "hunter2")
        self.assertEqual(str(cm.exception), "This secret request was already answered.")
        self.t3.secret_error = "boom hunter2"
        with self.assertRaises(BridgeError) as cm:
            self.b.answer_secret(self.s1, "hunter2")
        self.assertNotIn("hunter2", str(cm.exception))

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

    def test_agent_written_replies_replace_the_model(self):
        self.t3.items["t1"] = list(CONVERSATION[:-1]) + [item(
            2000002, "assistant_message", streaming=False,
            text='Answer two\n<!-- replies: ["Go", "Not yet"] -->')]
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=98)
        self.b.refresh()
        v = self.b.thread_view(self.s1)
        self.assertEqual([r["label"] for r in v["replies"]][:2], ["Go", "Not yet"])
        self.assertEqual(v["entries"][-1]["text"], "Answer two")
        self.assertEqual(self.sug.calls, 0)
        self.b.reply(self.s1, reply_id=v["replies"][0]["id"])
        self.assertEqual(self.t3.calls[-1][:2], ("send_message", ("t1", "Go")))

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

    def test_multiple_choice_ticks_then_done(self):
        q = {"id": "q1", "header": "Langs", "question": "Which languages?", "multiSelect": True,
             "options": [{"label": "Go"}, {"label": "Rust"}, {"label": "Lua"}]}
        self._make_pending("user_input", item(9, "user_input_request", requestId="req1", questions=[q]))
        v = self.b.thread_view(self.s1)
        self.assertEqual([r["label"] for r in v["replies"]][:4], ["○ Go", "○ Rust", "○ Lua", "Done"])
        self.assertFalse(v["replies"][3]["enabled"])
        self.b.settle_delay = 0.3  # a real wait for T3, which ticks must skip
        t0 = time.monotonic()
        v = self.b.reply(self.s1, reply_id=v["replies"][2]["id"])  # Lua
        v = self.b.reply(self.s1, reply_id=v["replies"][0]["id"])  # Go
        self.assertLess(time.monotonic() - t0, 1.0)  # ticks don't wait for T3
        self.assertEqual([r["label"] for r in v["replies"]][:4], ["● Go", "○ Rust", "● Lua", "Done (2 picked)"])
        self.assertFalse([c for c in self.t3.calls if c[0] == "respond_user_input"])
        self.b.reply(self.s1, reply_id=v["replies"][3]["id"])
        self.assertEqual(self.t3.calls[-1][:2], ("respond_user_input", ("t1", "req1", {"q1": ["Go", "Lua"]})))

    def test_wait_wakes_on_change(self):
        v = self.b.thread_view(self.s2)["v"]
        result = {}

        def waiter():
            result["changed"], result["view"] = self.b.wait_view(self.s2, v, timeout=5)

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.1)
        self.t3.threads["t2"] = thread("t2", "p2", status="completed", itemCount=3)
        self.b.refresh()
        th.join(2)
        self.assertTrue(result.get("changed"))
        self.assertEqual(result["view"]["state"], "done")
        self.assertFalse(self.b.wait_view(self.s2, result["view"]["v"], timeout=0.05)[0])

    def _add_step(self, n, item_):
        self.t3.items["t2"] = self.t3.items.get("t2", []) + [item_]
        self.t3.threads["t2"] = thread("t2", "p2", status="running", itemCount=n,
                                       updatedAt=f"2026-01-05T00:00:{n:02d}Z")
        self.b.refresh()

    def test_tool_progress_while_working_does_not_wake(self):
        v = self.b.thread_view(self.s2)["v"]
        self._add_step(1, item(1, "command_execution", input="make"))
        self.assertFalse(self.b.wait_view(self.s2, v, timeout=0.05)[0])

    def test_progress_mode_wakes_on_tool_calls(self):
        pv = self.b.thread_view(self.s2, progress=True)["v"]
        self._add_step(1, item(1, "command_execution", input="make"))
        self.assertTrue(self.b.wait_view(self.s2, pv, timeout=0.05, progress=True)[0])

    def test_invisible_updates_do_not_wake(self):
        """Reasoning and replies still being written change T3's thread but not the screen."""
        self._add_step(1, item(1, "command_execution", input="make"))
        pv = self.b.thread_view(self.s2, progress=True)["v"]
        self._add_step(2, item(2, "reasoning", text="thinking"))
        self._add_step(3, item(3, "assistant_message", text="partial", streaming=True))
        self.assertFalse(self.b.wait_view(self.s2, pv, timeout=0.05, progress=True)[0])

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
        self.assertIsNone(self.t3.calls[-1][2]["workspace"])  # no worktree default: the root

    def test_start_thread_follows_worktree_default(self):
        self.t3.settings["defaultThreadEnvMode"] = "worktree"
        self.b.start_thread(render.short_id("p1"), "In a worktree", client_msg_id="w1")
        self.assertEqual(self.t3.calls[-1][2]["workspace"],
                         {"type": "worktree", "baseRef": "main", "startFromOrigin": True})
        self.t3.settings["newWorktreesStartFromOrigin"] = False
        self.t3.settings["projectSettingsOverrides"]["p2"]["defaultThreadEnvMode"] = "local"
        self.b.start_thread(render.short_id("p2"), "Project says local", client_msg_id="w2")
        self.assertIsNone(self.t3.calls[-1][2]["workspace"])


class TokenTests(unittest.TestCase):
    class Client:
        def __init__(self, days_left, valid=("old", "new")):
            self.token = "old"
            self.days_left = days_left
            self.valid = valid

        def set_token(self, token):
            self.token = token

        def auth_session(self):
            from datetime import datetime, timedelta, timezone
            from t3bridge.t3client import T3Error
            if self.token not in self.valid:
                raise T3Error("HTTP 401", status=401)
            days = self.days_left if self.token == "old" else 30
            exp = datetime.now(timezone.utc) + timedelta(days=days)
            return {"authenticated": True, "expiresAt": exp.isoformat().replace("+00:00", "Z")}

    def setUp(self):
        import sys
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"
        self.env.write_text("T3_URL=http://x\nT3_TOKEN=old\nBRIDGE_PORT=1\n")
        self.cmd = f"{sys.executable} -c 'print(\"new\")'"

    def tearDown(self):
        self.dir.cleanup()

    def keeper(self, client, command=None):
        from t3bridge.token import TokenKeeper
        return TokenKeeper(client, self.cmd if command is None else command, self.env)

    def test_renews_near_expiry_and_saves(self):
        c = self.Client(days_left=3)
        k = self.keeper(c)
        k.check()
        self.assertEqual(c.token, "new")
        self.assertIn("T3_TOKEN=new\n", self.env.read_text())
        self.assertIn("BRIDGE_PORT=1", self.env.read_text())
        self.assertGreater(k.days_left, 29)
        self.assertIsNone(k.notice())

    def test_leaves_fresh_token_alone(self):
        c = self.Client(days_left=20)
        self.keeper(c).check()
        self.assertEqual(c.token, "old")

    def test_rejected_token_renews(self):
        c = self.Client(days_left=20, valid=("new",))
        self.keeper(c).check()
        self.assertEqual(c.token, "new")

    def test_without_command_warns_device(self):
        c = self.Client(days_left=3)
        k = self.keeper(c, command="")
        k.check()
        self.assertEqual(c.token, "old")
        self.assertIn("expires in 3 days", k.notice())

    def test_bad_command_keeps_old_token_and_warns(self):
        c = self.Client(days_left=3)
        k = self.keeper(c, command="false")
        k.check()
        self.assertEqual(c.token, "old")
        self.assertIn("expires", k.notice())
        self.assertIn("T3_TOKEN=old", self.env.read_text())


class ImageTests(unittest.TestCase):
    PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201"
                        "f2b9e0a20000000049454e44ae426082")  # 1x1 PNG

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.png = Path(self.dir.name) / "shot.png"
        self.png.write_bytes(self.PNG)
        self.t3 = FakeT3()
        self.t3.base_url = ""
        self.t3.rpc = lambda method, payload: {"relativeUrl": self.png.as_uri()}
        self.t3.threads["t1"] = thread("t1", itemCount=2)
        self.t3.items["t1"] = [
            item(1, "user_message", text="", createdBy="user",
                 attachments=[{"type": "image", "id": "att1", "name": "photo.jpg", "mimeType": "image/jpeg"}]),
            item(2, "assistant_message", streaming=False,
                 text=f"Here: ![screenshot]({self.png}) and ![web](https://x/y.png)"),
        ]
        self.b = Bridge(self.t3, None)
        self.b.refresh()

    def tearDown(self):
        self.b.close()
        self.dir.cleanup()

    def test_view_lists_images_and_links_them(self):
        v = self.b.thread_view(render.short_id("t1"))
        self.assertEqual(len(v["images"]), 2)  # the attachment and the local file; not the web URL
        self.assertIn('<img src="t3img:', v["entries"][0]["html"])  # image-only message still shown
        self.assertIn("[image: web]", v["entries"][1]["html"])
        self.assertIn('href="t3:img:', v["entries"][1]["html"])

    def test_tool_screenshots_become_links(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
        self.assertEqual(render.tool_output_images({"content": [img, {"type": "text"}, img]}), 2)
        self.assertEqual(render.tool_output_images([{"type": "image", "mimeType": "image/tiff"}]), 0)
        self.assertEqual(render.tool_output_images({"content": [img] * 12}), 8)  # T3's cap
        self.t3.items["t1"].append(item(3, "dynamic_tool", id="tool-1", toolName="mcp__t3-code__preview_snapshot",
                                        input={}, output={"content": [img, img]}))
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=3)
        self.b.refresh()
        v = self.b.thread_view(render.short_id("t1"))
        line = v["entries"][-1]
        self.assertEqual(line["role"], "tool")
        self.assertEqual(line["html"].count('href="t3:img:'), 2)
        self.assertIn("screenshot 1 ›", line["html"])
        self.assertEqual(len(v["images"]), 2)  # links aren't prefetched; only the inline images are
        link_id = line["html"].split('t3:img:')[1].split('"')[0]
        resource, _ = self.b.images.registry[link_id]
        self.assertEqual(resource, {"_tag": "tool-output-image", "threadId": "t1", "itemId": "tool-1", "index": 0})

    def test_only_registered_images_are_served(self):
        from t3bridge.images import ImageError
        with self.assertRaises(ImageError):
            self.b.images.get("unknown1", 800)
        v = self.b.thread_view(render.short_id("t1"))
        self.b.images.converter = None
        data, ctype = self.b.images.get(v["images"][1], 800)
        self.assertEqual((data, ctype), (self.PNG, "image/png"))

    def test_converter_makes_grayscale_png(self):
        import shutil
        conv = shutil.which("magick") or shutil.which("convert")
        if not conv:
            self.skipTest("ImageMagick not installed")
        v = self.b.thread_view(render.short_id("t1"))
        self.b.images.converter = conv
        data, ctype = self.b.images.get(v["images"][1], 800)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))


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
        self.assertEqual(body["v"], view["v"])
        self.assertEqual((status, body["changed"]), (200, False))


if __name__ == "__main__":
    unittest.main()
