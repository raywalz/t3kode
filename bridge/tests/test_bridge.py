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

    providers = [
        {"instanceId": "x", "displayName": "Personal", "enabled": True, "installed": True, "status": "ready",
         "models": [{"slug": "m", "name": "Model M"}, {"slug": "default-model", "name": "Default"},
                    {"slug": "old", "name": "Old", "isLegacy": True},
                    {"slug": "secret", "name": "Hidden"}]},
        {"instanceId": "y", "displayName": "Work", "enabled": True, "installed": True,
         "status": "ready", "models": [{"slug": "m", "name": "Model M"}]},
        {"instanceId": "z", "displayName": "Broken", "enabled": True, "installed": True,
         "status": "error", "models": [{"slug": "z1", "name": "Z1"}]},
    ]

    def server_config(self):
        return {"settings": {**self.settings,
                             "favorites": [{"provider": "x", "model": "m"}, {"provider": "y", "model": "m"}],
                             "providerModelPreferences": {"x": {"hiddenModels": ["secret"]}}},
                "providers": self.providers}

    def thread_command(self, type_, tid, **fields):
        t = dict(self.threads[tid])
        if type_ == "thread.pin":
            t.update(pinnedAt="2026-01-05T00:00:00Z", pinOrderKey=f"k{len(self.calls)}")
        elif type_ == "thread.unpin":
            t.update(pinnedAt=None, pinOrderKey=None)
        elif type_ == "thread.snooze":
            t.update(snoozedUntil=fields["snoozedUntil"], snoozedAt="2026-01-05T00:00:00Z")
        elif type_ == "thread.unsnooze":
            t.update(snoozedUntil=None)
        elif type_ == "thread.metadata.update" and "title" in fields:
            t.update(title=fields["title"])
        elif type_ == "thread.mark-unread":
            t.update(lastVisitedAt="2025-12-31T00:00:00Z")
        elif type_ in ("queued-run.cancel", "queued-run.start", "queued-message.promote-to-steer"):
            run = fields.get("runId") or fields.get("queuedRunId")
            for r in self.projections[tid]["runs"]:
                if r["id"] == run:
                    r["status"] = "cancelled" if type_ == "queued-run.cancel" else "running"
        if type_ == "thread.archive":
            del self.threads[tid]  # the shell leaves archived threads out
        else:
            self.threads[tid] = t
        return self._record("thread_command", type_, tid, **fields)

    def set_interaction_mode(self, tid, mode, **kw):
        self.threads[tid] = dict(self.threads[tid], interactionMode=mode)
        return self._record("set_interaction_mode", tid, mode, **kw)

    def set_model(self, tid, sel, **kw):
        self.threads[tid] = dict(self.threads[tid], modelSelection=sel)
        return self._record("set_model", tid, sel, **kw)

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

    def thread_bounded(self, tid):
        return {"projection": self.projections.get(tid, {})}

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

    def test_html_img_tags_are_images(self):
        from t3bridge.markdown import to_html
        seen = []
        def image(src, alt):
            seen.append((src, alt))
            return f"<img src='{len(seen)}'/>"
        md = ('Shots:\n\n<p align="center">\n<img src="/tmp/a.png" width="45%" alt="A &amp; B">\n'
              "<a href='x'><img src='/tmp/b.png'></a>\n</p>\n\nAfter `<img src=\"/tmp/c.png\">` and <b>raw</b>.")
        self.assertEqual(to_html(md, image),
                         "<p>Shots:</p><p><img src='1'/><br/><img src='2'/></p>"
                         "<p>After <code>&lt;img src=\"/tmp/c.png\"&gt;</code> and &lt;b&gt;raw&lt;/b&gt;.</p>")
        self.assertEqual(seen, [("/tmp/a.png", "A & B"), ("/tmp/b.png", "")])
        self.assertEqual(to_html('<img alt="logo">'), "<p><i>[image: logo]</i></p>")  # no src

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


class ContextTests(unittest.TestCase):
    def test_context_usage(self):
        self.assertEqual([render.format_tokens(n) for n in (950, 9500, 9049, 219877, 999_499, 1_000_000)],
                         ["950", "9.5k", "9k", "220k", "999k", "1m"])
        proj = {"thread": {"activeProviderThreadId": "pt2"},
                "providerThreads": [{"id": "pt1", "contextUsage": {"usedTokens": 5}},
                                    {"id": "pt2", "contextUsage": {"usedTokens": 219877, "maxTokens": 1000000}}],
                "providerTurns": []}
        self.assertEqual(render.context_usage(proj), {
            "used": 219877, "max": 1000000, "percent": 22, "label": "22% of context used (220k of 1m)"})
        # The newest turn report wins over the provider thread's.
        proj["providerTurns"] = [
            {"tokenUsage": {"usedTokens": 300000, "maxTokens": 1000000, "updatedAt": "2026-01-02T00:00:00Z"}},
            {"tokenUsage": {"usedTokens": 100000, "maxTokens": 1000000, "updatedAt": "2026-01-01T00:00:00Z"}},
            {}]
        self.assertEqual(render.context_usage(proj)["percent"], 30)
        # No window size: tokens only. No reports: nothing.
        proj["providerTurns"] = [{"tokenUsage": {"usedTokens": 1500, "updatedAt": "x"}}]
        self.assertEqual(render.context_usage(proj)["label"], "1.5k tokens of context used")
        self.assertIsNone(render.context_usage({}))


class CitationTests(unittest.TestCase):
    def test_citations_show_quote_and_comment(self):
        url = ("t3-citation://v1/env/thread%3Ax/message%3Ay?text=Reload+without+restarting%3A+"
               "%22Reload%22%0A%0A%0A%0AFrom+you&start=1&end=2&prefix=a&suffix=+b&comment=do+this+please")
        plain, body = render.user_message(f"[Assistant quote]({url}) [Assistant quote]({url}) and <b>")
        self.assertEqual(plain, "“Reload without restarting: \"Reload\"\n\nFrom you”\n\ndo this please\n\n"
                                "“Reload without restarting: \"Reload\"\n\nFrom you”\n\ndo this please\n\nand <b>")
        self.assertEqual(body.count("<blockquote><p>Reload without restarting: \"Reload\"</p><p>From you</p>"
                                    "</blockquote><p>do this please</p>"), 2)
        self.assertTrue(body.endswith("<p>and &lt;b&gt;</p>"))
        self.assertNotIn("t3-citation", body)
        long = "x" * 500
        plain, _ = render.user_message(f"[q](t3-citation://v1/a?text={long})")
        self.assertEqual(plain, "“" + "x" * (render.QUOTE_MAX - 1) + "…”")
        self.assertEqual(render.user_message("plain [link](https://e.com)")[0], "plain [link](https://e.com)")


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

    def test_huge_thread_pages_back_through_history(self):
        from t3bridge.t3client import T3Error

        def too_large(tid):
            raise T3Error('HTTP 400: {"reason":"thread_transcript_too_large"}', status=400)

        def turn(n):  # your message n and the reply, ordinals n*10+1 and n*10+2
            return [{"item": item(n * 10 + 1, "user_message", id=f"u{n}", text=f"Q{n}", createdBy="user")},
                    {"item": item(n * 10 + 2, "assistant_message", id=f"a{n}", text=f"A{n}",
                                  streaming=False)}]

        pages = {"c3": ([r for n in (4, 5, 6) for r in turn(n)], "c2", True),
                 "c2": ([r for n in (1, 2, 3) for r in turn(n)], None, False)}
        fetched = []

        def history(tid, cursor):
            fetched.append(cursor)
            rows, nxt, more = pages[cursor]
            return {"items": rows, "nextCursor": nxt, "hasMoreHistory": more}

        self.t3.thread_transcript = too_large
        self.t3.thread_history = history
        self.t3.thread_bounded = lambda tid: {"historyCursor": "c3", "hasMoreHistory": True,
            "projection": {"visibleTurnItems": [r for n in (7, 8) for r in turn(n)]}}
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=99)
        self.b.refresh()
        texts = lambda v: [e["text"] for e in v["entries"] if e["role"] == "you"]
        v = self.b.thread_view(self.s1)  # 3 turns: one older page is fetched for the third
        self.assertEqual((texts(v), v["more_before"]), (["Q6", "Q7", "Q8"], True))
        first = v["entries"][0]["o"]
        v = self.b.thread_view(self.s1, before=first)
        self.assertEqual((texts(v), v["more_before"]), (["Q3", "Q4", "Q5"], True))
        v = self.b.thread_view(self.s1, before=v["entries"][0]["o"])
        self.assertEqual((texts(v), v["more_before"]), (["Q1", "Q2"], False))
        self.assertEqual(fetched, ["c3", "c2"])  # each page once
        v = self.b.thread_view(self.s1, after=first - 1)  # pinned at Q6: already loaded
        self.assertEqual(texts(v), ["Q6", "Q7", "Q8"])
        self.assertEqual(fetched, ["c3", "c2"])

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

    def test_start_thread_on_a_fresh_t3_uses_first_ready_provider(self):
        # A new install: no default model anywhere and no earlier thread in the project.
        self.t3.settings = {"defaultRuntimeMode": "full-access", "projectSettingsOverrides": {}}
        self.t3.providers = [dict(FakeT3.providers[2]),  # status error: skipped
                             dict(FakeT3.providers[1], models=[{"slug": "a", "isCustom": True}, {"slug": "b"},
                                                               {"slug": "c", "isDefault": True}])]
        self.t3.projects.append({"id": "p3", "title": "Gamma"})
        self.b.refresh()
        self.assertEqual(self.b.models(project=render.short_id("p3"))["current_label"], "c")
        self.b.start_thread(render.short_id("p3"), "Hello", client_msg_id="f1")
        self.assertEqual(self.t3.calls[-1][1][3], {"instanceId": "y", "model": "c"})

    def test_first_ready_model(self):
        from t3bridge.core import first_ready_model
        warn = {"instanceId": "w", "status": "warning", "models": [{"slug": "w1"}]}
        ready = {"instanceId": "r", "status": "ready", "models": [{"slug": "r1"}, {"slug": "r2", "isDefault": True}]}
        off = {"instanceId": "o", "status": "ready", "enabled": False, "models": [{"slug": "o1"}]}
        self.assertEqual(first_ready_model([off, warn, ready]), {"instanceId": "r", "model": "r2"})
        self.assertEqual(first_ready_model([off, warn]), {"instanceId": "w", "model": "w1"})
        self.assertIsNone(first_ready_model([off, {"instanceId": "e", "status": "error", "models": [{"slug": "e"}]}]))

    def test_start_thread_follows_worktree_default(self):
        self.t3.settings["defaultThreadEnvMode"] = "worktree"
        self.b.start_thread(render.short_id("p1"), "In a worktree", client_msg_id="w1")
        self.assertEqual(self.t3.calls[-1][2]["workspace"],
                         {"type": "worktree", "baseRef": "main", "startFromOrigin": True})
        self.t3.settings["newWorktreesStartFromOrigin"] = False
        self.t3.settings["projectSettingsOverrides"]["p2"]["defaultThreadEnvMode"] = "local"
        self.b.start_thread(render.short_id("p2"), "Project says local", client_msg_id="w2")
        self.assertIsNone(self.t3.calls[-1][2]["workspace"])

    def test_models_lists_favorites_then_the_rest(self):
        out = self.b.models(thread=self.s1)
        mid = lambda inst, slug: render.short_id(f"model:{inst}:{slug}")
        self.assertEqual(out["current"], mid("x", "m"))
        self.assertEqual(out["current_label"], "Model M")
        # The same model under two providers says which.
        self.assertEqual([f["label"] for f in out["favorites"]],
                         ["Model M · Personal", "Model M · Work"])
        # Favorites aren't repeated; legacy, hidden and broken providers' models are left out.
        self.assertEqual(out["groups"], [{"title": "Personal", "models": [
            {"id": mid("x", "default-model"), "label": "Default"}]}])
        self.assertEqual(self.b.models(project=render.short_id("p1"))["current"], mid("x", "default-model"))

    def test_view_shows_model_and_set_model_switches_it(self):
        v = self.b.thread_view(self.s1)
        self.assertEqual(v["model"], "Model M")
        target = render.short_id("model:x:default-model")
        out = self.b.set_model(self.s1, target, client_msg_id="sm1")
        name, args, kw = self.t3.calls[-1]
        self.assertEqual((name, args[1]), ("set_model", {"instanceId": "x", "model": "default-model"}))
        self.assertEqual(out["model"], "Default")
        self.assertNotEqual(out["v"], v["v"])
        # Choosing the model it already has sends nothing.
        n = len(self.t3.calls)
        self.b.set_model(self.s1, target, client_msg_id="sm2")
        self.assertEqual(len(self.t3.calls), n)
        with self.assertRaises(BridgeError):
            self.b.set_model(self.s1, render.short_id("model:x:secret"))

    def test_start_thread_with_chosen_model(self):
        self.b.start_thread(render.short_id("p1"), "Use M", client_msg_id="cm1",
                            model=render.short_id("model:y:m"))
        self.assertEqual(self.t3.calls[-1][1][3], {"instanceId": "y", "model": "m"})


    def _with_plan(self):
        self.t3.items["t1"].append(item(9, "proposed_plan", runId="r2", planId="plan-1",
                                        markdown="# Add caching\n\n1. Do it", streaming=False))
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=9, interactionMode="plan",
                                     hasActionableProposedPlan=True)
        self.b.refresh()

    def test_view_context_usage(self):
        self.t3.projections["t1"] = {"thread": {"activeProviderThreadId": "pt"}, "providerThreads": [
            {"id": "pt", "contextUsage": {"usedTokens": 50000, "maxTokens": 200000}}]}
        self.b.refresh()
        v = self.b.thread_view(self.s1)
        self.assertEqual(v["context"]["label"], "25% of context used (50k of 200k)")
        self.t3.projections["t1"]["providerThreads"][0]["contextUsage"]["usedTokens"] = 100000
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], updatedAt="2026-01-09T00:00:00Z")
        self.b.refresh()
        self.assertNotEqual(self.b.thread_view(self.s1)["v"], v["v"])
        self.assertIsNone(self.b.thread_view(render.short_id("t2"))["context"])

    def _with_queue(self, active="running", held=False):
        self.t3.threads["t2"] = dict(self.t3.threads["t2"], activeRunId="r1")
        self.t3.projections["t2"] = {
            "thread": {"id": "t2", "activeProviderThreadId": "pt"},
            "runs": [
                {"id": "r1", "ordinal": 1, "status": active, "userMessageId": "m1", "activeAttemptId": "a1",
                 "providerThreadId": "pt"},
                {"id": "r3", "ordinal": 3, "status": "queued", "queuePosition": 0, "userMessageId": "m3",
                 "queueHeld": held},
                {"id": "r2", "ordinal": 2, "status": "queued", "queuePosition": 1, "userMessageId": "m2"},
                {"id": "r4", "ordinal": 4, "status": "queued", "userMessageId": "m4"},
            ],
            "messages": [{"id": "m1", "text": "Go"}, {"id": "m2", "text": "Then this"},
                         {"id": "m3", "text": "First this"},
                         {"id": "m4", "text": "Subagent done", "delegatedCompletion": {}}],
            "providerThreads": [{"id": "pt", "providerSessionId": "s1"}],
            "providerSessions": [{"id": "s1", "status": "ready",
                                  "capabilities": {"turns": {"supportsActiveSteering": True}}}],
            "providerTurns": [{"runAttemptId": "a1", "status": "running"}],
        }
        self.b.refresh()
        return render.short_id("t2")

    def test_queued_messages(self):
        short = self._with_queue()
        q = self.b.thread_view(short)["queue"]
        # In queue order; a subagent's automatic completion message isn't yours to manage.
        self.assertEqual(q, {"items": [{"id": render.short_id("r3"), "text": "First this"},
                                       {"id": render.short_id("r2"), "text": "Then this"}],
                             "held": False, "can_send_now": False, "can_steer": True})
        with self.assertRaises(BridgeError):  # a turn is running
            self.b.queue_action(short, "send", q["items"][0]["id"])
        self.b.settle_delay = 0
        self.b.queue_action(short, "steer", q["items"][0]["id"])
        self.assertEqual(self.t3.calls[-1][1][:2], ("queued-message.promote-to-steer", "t2"))
        self.assertEqual(self.t3.calls[-1][2], {"queuedRunId": "r3", "targetRunId": "r1"})
        self.b.queue_action(short, "remove", q["items"][1]["id"])
        self.assertEqual(self.t3.calls[-1][2], {"runId": "r2"})
        with self.assertRaises(BridgeError):  # gone now
            self.b.queue_action(short, "remove", q["items"][1]["id"])

    def test_queue_after_stop(self):
        short = self._with_queue(active="interrupted", held=True)
        q = self.b.thread_view(short)["queue"]
        self.assertEqual((q["held"], q["can_send_now"], q["can_steer"]), (True, True, False))
        self.b.settle_delay = 0
        self.b.queue_action(short, "send", q["items"][1]["id"])
        self.assertEqual(self.t3.calls[-1][1][0], "queued-run.start")
        self.b.queue_action(short, "resume")
        self.assertEqual(self.t3.calls[-1][1][0], "queue.resume")

    def test_typed_reply_queue_or_steer(self):
        short = render.short_id("t2")  # working
        self.b.settle_delay = 0
        self.b.reply(short, text="later", client_msg_id="q1", deliver="queue")
        self.assertTrue(self.t3.calls[-1][2]["queue"])
        self.b.reply(short, text="now", client_msg_id="q2", deliver="steer")
        self.assertFalse(self.t3.calls[-1][2]["queue"])
        with self.assertRaises(BridgeError):
            self.b.reply(short, text="x", client_msg_id="q3", deliver="sideways")

    def test_task_list(self):
        steps = lambda *st: [{"id": f"todo-{i}", "text": f"Step {i}", "status": x} for i, x in enumerate(st)]
        self.t3.items["t2"] = [
            item(1, "user_message", runId="r1", text="Old", createdBy="user"),
            item(2, "todo_list", runId="r1", planId="a", steps=steps("completed", "completed")),
            item(3, "user_message", runId="r2", text="Go", createdBy="user"),
            item(4, "todo_list", runId="r2", planId="b", steps=steps("completed", "running", "pending")),
        ]
        self.t3.threads["t2"] = dict(self.t3.threads["t2"], itemCount=4, activeRunId="r2")
        self.b.refresh()
        short = render.short_id("t2")
        v = self.b.thread_view(short)
        self.assertEqual(v["tasks"], {"done": 1, "total": 3, "live": True, "steps": [
            {"text": "Step 0", "status": "completed"}, {"text": "Step 1", "status": "running"},
            {"text": "Step 2", "status": "pending"}]})
        # A step finishing changes what the device draws, even mid-turn without progress.
        self.t3.items["t2"][-1] = item(4, "todo_list", runId="r2", planId="b",
                                       steps=steps("completed", "completed", "running"))
        self.t3.threads["t2"] = dict(self.t3.threads["t2"], updatedAt="2026-01-05T00:00:00Z")
        self.b.refresh()
        self.assertNotEqual(self.b.thread_view(short)["v"], v["v"])
        # The turn ended with a step left: still shown. All done: gone.
        self.t3.threads["t2"] = dict(self.t3.threads["t2"], status="completed", activeRunId=None,
                                     updatedAt="2026-01-06T00:00:00Z")
        self.b.refresh()
        self.assertEqual(self.b.thread_view(short)["tasks"]["live"], False)
        self.t3.items["t2"][-1] = item(4, "todo_list", runId="r2", planId="b",
                                       steps=steps("completed", "completed", "completed"))
        self.t3.threads["t2"] = dict(self.t3.threads["t2"], updatedAt="2026-01-07T00:00:00Z")
        self.b.refresh()
        self.assertIsNone(self.b.thread_view(short)["tasks"])

    def test_actionable_plan_offers_implement(self):
        self._with_plan()
        v = self.b.thread_view(self.s1)
        self.assertEqual((v["mode"], v["plan_mode"]), ("plan", True))
        # The plan stays visible when its turn folds, as in the app.
        self.assertTrue(any(e["text"].startswith("Add caching") for e in v["entries"]))
        self.assertEqual(v["replies"][0], {"id": f"p:{render.short_id('plan-1')}",
                                           "label": "Implement plan", "kind": "plan"})
        self.b.reply(self.s1, reply_id=v["replies"][0]["id"], client_msg_id="ip1")
        (n1, a1, _), (n2, a2, kw) = self.t3.calls[-2:]
        self.assertEqual((n1, a1[1]), ("set_interaction_mode", "default"))
        self.assertEqual(n2, "send_message")
        self.assertEqual(a2[1], "PLEASE IMPLEMENT THIS PLAN:\n# Add caching\n\n1. Do it")
        self.assertEqual(kw["source_plan"], {"threadId": "t1", "planId": "plan-1"})
        # Shown short: the plan is right above it.
        self.t3.items["t1"].append(item(10, "user_message", runId="r3", createdBy="user", text=a2[1]))
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=10, updatedAt="2026-01-05T00:00:00Z")
        self.b.refresh()
        self.assertEqual(self.b.thread_view(self.s1)["entries"][-1]["text"], "Implement the plan.")
        # Once T3 marks it implemented, the button goes and a stale tap is refused.
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], hasActionableProposedPlan=False, itemCount=11)
        self.b.refresh()
        self.assertFalse(any(r["kind"] == "plan" for r in self.b.thread_view(self.s1)["replies"]))
        with self.assertRaises(BridgeError):
            self.b.reply(self.s1, reply_id=f"p:{render.short_id('plan-1')}", client_msg_id="ip2")

    def test_typed_reply_switches_plan_mode_first(self):
        self.b.reply(self.s1, text="Plan the cache", mode="plan", client_msg_id="pm1")
        names = [c[0] for c in self.t3.calls[-2:]]
        self.assertEqual(names, ["set_interaction_mode", "send_message"])
        self.assertEqual(self.b.thread_view(self.s1)["mode"], "plan")
        # Same mode again: nothing extra is sent.
        n = len(self.t3.calls)
        self.b.reply(self.s1, text="More", mode="plan", client_msg_id="pm2")
        self.assertEqual([c[0] for c in self.t3.calls[n:]], ["send_message"])

    def test_plan_mode_follows_provider(self):
        self.t3.providers = [dict(p, showInteractionModeToggle=False) for p in FakeT3.providers]
        self.b._config = None
        self.b.refresh()
        self.assertFalse(self.b.thread_view(self.s1)["plan_mode"])
        with self.assertRaises(BridgeError):
            self.b.reply(self.s1, text="Plan it", mode="plan", client_msg_id="pp1")

    def test_start_thread_in_plan_mode(self):
        self.b.start_thread(render.short_id("p1"), "Plan a refactor", client_msg_id="sp1", mode="plan")
        self.assertEqual(self.t3.calls[-1][2]["interaction_mode"], "plan")
        self.b.start_thread(render.short_id("p1"), "Just do it", client_msg_id="sp2")
        self.assertEqual(self.t3.calls[-1][2]["interaction_mode"], "default")


    def _titles(self, body):
        return {g["title"]: [r["title"] for r in g["threads"]] for g in body["projects"]}

    def test_pin_puts_thread_in_a_pinned_group_on_top(self):
        self.b.thread_action(self.s2, "pin")
        out = self.b.thread_list()
        self.assertEqual(out["projects"][0]["title"], "Pinned")
        self.assertEqual(self._titles(out)["Pinned"], ["Thread t2"])
        self.assertTrue(out["projects"][0]["threads"][0]["pinned"])
        self.assertNotIn("Beta", self._titles(out))  # not listed twice
        # A project's own list shows it first, in place.
        self.assertEqual(self.b.thread_list(project=render.short_id("p2"))["projects"][0]["threads"][0]["title"],
                         "Thread t2")
        # The thread view carries the same fields, for its own thread menu.
        v = self.b.thread_view(self.s2)
        self.assertEqual((v["pinned"], v["settled"], v["snoozed_until"]), (True, False, None))
        # Opening an unread thread marks it read, so it can be marked unread again at once.
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], lastVisitedAt="2025-01-01T00:00:00Z")
        self.b.refresh()
        self.assertFalse(self.b.thread_view(self.s1)["can_mark_unread"])
        self.assertTrue(self.b.thread_view(self.s1, visit=True)["can_mark_unread"])
        self.b.thread_action(self.s2, "unpin")
        self.assertNotIn("Pinned", self._titles(self.b.thread_list()))

    def test_snooze_hides_until_wake_or_a_request(self):
        self.b.thread_action(self.s1, "snooze", snooze="tomorrow")
        name, args, kw = self.t3.calls[-1]
        self.assertEqual(args[0], "thread.snooze")
        self.assertTrue(kw["snoozedUntil"].endswith("Z"))
        out = self.b.thread_list()
        alpha = next(g for g in out["projects"] if g["title"] == "Alpha")
        self.assertNotIn("Thread t1", [r["title"] for r in alpha["threads"]])
        self.assertEqual(alpha["snoozed"], 1)
        row = next(r for r in self.b.thread_list(project=render.short_id("p1"))["projects"][0]["threads"]
                   if r["title"] == "Thread t1")
        self.assertIsNotNone(row["snoozed_until"])
        # Asking for something wakes it, as in T3.
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], pendingRuntimeRequest={"id": "r"})
        self.b.refresh()
        self.assertIn("Thread t1", self._titles(self.b.thread_list())["Alpha"])
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], pendingRuntimeRequest=None)
        self.b.refresh()
        self.b.thread_action(self.s1, "unsnooze")
        self.assertEqual(self.t3.calls[-1][2], {"reason": "user"})
        self.assertIn("Thread t1", self._titles(self.b.thread_list())["Alpha"])
        with self.assertRaises(BridgeError):
            self.b.thread_action(self.s1, "snooze", snooze="someday")

    def test_snooze_presets(self):
        from datetime import datetime, timezone
        from t3bridge.core import snooze_until
        fri = datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc)  # a Friday
        self.assertEqual(snooze_until("hour", fri), "2026-11-20T11:30:00.000Z")
        self.assertEqual(snooze_until("evening", fri), "2026-11-20T18:00:00.000Z")
        self.assertEqual(snooze_until("tomorrow", fri), "2026-11-21T09:00:00.000Z")
        self.assertEqual(snooze_until("next-week", fri), "2026-11-23T09:00:00.000Z")
        with self.assertRaises(BridgeError):
            snooze_until("evening", fri.replace(hour=17, minute=30))

    def test_mark_unread_and_archive(self):
        row = lambda: next(r for r in self.b.thread_list()["projects"] for r in r["threads"]
                           if r["title"] == "Thread t1")
        self.assertTrue(row()["can_mark_unread"])
        self.b.thread_action(self.s1, "mark_unread")
        self.assertEqual((row()["unread"], row()["can_mark_unread"]), (True, False))
        with self.assertRaises(BridgeError):
            self.b.thread_action(self.s2, "archive")  # still working
        self.b.thread_action(self.s1, "archive")
        self.assertNotIn("Thread t1", self._titles(self.b.thread_list()).get("Alpha", []))


    def test_view_sums_up_linked_prs(self):
        snap = lambda **kw: {"title": "Fix it", "isDraft": False, "syncedAt": "2026-01-05T00:00:00Z", **kw}
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], updatedAt="2026-01-06T00:00:00Z", pullRequests=[
            {"number": 3, "repository": "me/app", "url": "u3", "snapshot": snap(state="merged")},
            {"number": 4, "repository": "me/app", "url": "u4", "snapshot": snap(
                state="open", checksState="failing", reviewDecision="approved", mergeability="conflicting")},
            {"number": 5, "repository": "me/app", "url": "u5", "snapshot": snap(state="open", isDraft=True)},
            {"number": 6, "repository": "me/app", "url": "u6", "snapshot": None}])
        self.b.refresh()
        v = self.b.thread_view(self.s1)
        self.assertEqual([(p["number"], p["status"]) for p in v["prs"]], [
            (4, "open · checks fail · approved · conflicts"), (5, "draft"),
            (3, "merged"), (6, "not synced")])
        # Checks finishing changes the view.
        self.t3.threads["t1"]["pullRequests"][1]["snapshot"]["checksState"] = "passing"
        self.b.refresh()
        self.assertNotEqual(self.b.thread_view(self.s1)["v"], v["v"])


    def test_changed_files_fold_under_the_last_reply(self):
        self.t3.items["t1"] = [
            item(1, "user_message", runId="r1", text="Fix it", createdBy="user"),
            item(2, "assistant_message", runId="r1", text="Looking.", streaming=False),
            item(3, "dynamic_tool", runId="r1", toolName="Edit", input={"file_path": "/a/b.py"}),
            item(4, "assistant_message", runId="r1", text="Fixed.", streaming=False),
            item(5, "checkpoint", runId="r1", files=[
                {"path": "a/b.py", "kind": "modified", "additions": 10, "deletions": 2},
                {"path": "a/new.py", "kind": "added", "additions": 5, "deletions": 0}]),
            item(6, "user_message", runId="r2", text="Thanks", createdBy="user"),
            item(7, "assistant_message", runId="r2", text="No changes here.", streaming=False),
            item(8, "checkpoint", runId="r2", files=[])]
        self.t3.threads["t1"] = dict(self.t3.threads["t1"], itemCount=8, updatedAt="2026-01-07T00:00:00Z")
        self.b.refresh()
        v = self.b.thread_view(self.s1)
        fixed = next(e for e in v["entries"] if e["text"].startswith("Fixed."))
        self.assertEqual(fixed["text"], "Fixed.\n▸ 2 files changed · +15 −2")
        key = "f" + render.short_id("r1")
        self.assertIn(f'href="t3:run:{key}"', fixed["html"])
        self.assertEqual(v["entries"][-1]["text"], "No changes here.")  # nothing changed: no line
        # Tapping it lists the files, and that counts as a change for the device.
        opened = self.b.thread_view(self.s1, expand=frozenset({key}))
        fixed2 = next(e for e in opened["entries"] if e["text"].startswith("Fixed."))
        self.assertIn("a/new.py (new) +5 −0", fixed2["text"])
        self.assertNotEqual(opened["v"], v["v"])


    def test_rename_and_auto_title(self):
        self.b.thread_action(self.s1, "rename", title="  Fix   the login bug ")
        self.assertEqual(self.t3.calls[-1][1][:2], ("thread.metadata.update", "t1"))
        self.assertEqual(self.t3.calls[-1][2], {"title": "Fix the login bug"})
        self.assertEqual(self.b.thread_view(self.s1)["title"], "Fix the login bug")
        with self.assertRaises(BridgeError):
            self.b.thread_action(self.s1, "rename", title="   ")
        self.b.thread_action(self.s1, "auto_title")
        self.assertEqual(self.t3.calls[-1][2], {"regenerateTitle": True})


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

    def test_renews_right_after_boot(self):
        # The monotonic clock starts near zero at boot, which once looked like a recent failure.
        from unittest import mock
        c = self.Client(days_left=3)
        with mock.patch("t3bridge.token.time.monotonic", return_value=5.0):
            self.keeper(c).check()
        self.assertEqual(c.token, "new")

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


class BrowserTests(unittest.TestCase):
    JPEG = b"\xff\xd8fake"

    def setUp(self):
        self.t3 = FakeT3()
        self.t3.threads["t1"] = thread("t1", itemCount=0)
        self.t3.tabs = [
            {"tabId": "tab_1_e", "runtime": "server",
             "navStatus": {"_tag": "Success", "url": "https://a.test/", "title": "A page"}},
            {"tabId": "tab_2", "navStatus": {"_tag": "Idle"}},  # desktop app window
        ]
        self.frames = []
        self.t3.preview_tabs = lambda tid: self.t3.tabs if tid == "t1" else []

        def frame(tid, tab, max_width, max_height):
            self.frames.append((tid, tab, max_width, max_height))
            if getattr(self, "close_code", None):
                from t3bridge.t3client import T3SocketError
                raise T3SocketError("closed", close_code=self.close_code)
            return self.JPEG
        self.t3.preview_frame = frame
        self.b = Bridge(self.t3, None)
        self.b.images.converter = None
        self.b.refresh()
        self.sid = render.short_id("t1")

    def tearDown(self):
        self.b.close()

    def test_lists_tabs(self):
        tabs = self.b.browser_tabs(self.sid)["tabs"]
        self.assertEqual(tabs[0], {"id": "tab_1_e", "title": "A page", "url": "https://a.test/",
                                   "status": "ok", "viewable": True})
        self.assertEqual((tabs[1]["title"], tabs[1]["viewable"]), ("Blank tab", False))

    def test_snapshot(self):
        self.assertEqual(self.b.browser_snapshot(self.sid, "tab_1_e", 1448), (self.JPEG, "image/jpeg"))
        self.assertEqual(self.frames, [("t1", "tab_1_e", 1448, 1448 * 4)])

    def test_snapshot_errors(self):
        for tab, status in (("tab_2", 409), ("tab_gone", 404)):
            with self.assertRaises(BridgeError) as cm:
                self.b.browser_snapshot(self.sid, tab, 800)
            self.assertEqual(cm.exception.status, status)
        for code, status in ((4404, 404), (4503, 503), (1011, 502)):
            self.close_code = code
            with self.assertRaises(BridgeError) as cm:
                self.b.browser_snapshot(self.sid, "tab_1_e", 800)
            self.assertEqual(cm.exception.status, status)

    def test_http_snapshot_is_not_cached(self):
        httpd = serve(self.b, "127.0.0.1", 0, "")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}/v1/threads/{self.sid}/browser"
            with urllib.request.urlopen(base + "/snapshot?tab=tab_1_e&w=600", timeout=5) as r:
                self.assertEqual((r.read(), r.headers["cache-control"]), (self.JPEG, "no-store"))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/snapshot", timeout=5)
            self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()


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


class InstallPluginTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_installs_on_a_kobo_and_keeps_other_settings(self):
        from t3bridge import install
        ko = self.root / ".adds" / "koreader"
        (ko / "plugins" / "t3.koplugin").mkdir(parents=True)
        (ko / "plugins" / "t3.koplugin" / "_meta.lua").write_text('return { name = "t3" }')
        (ko / "plugins" / "t3.koplugin" / "stale.lua").write_text("")
        (ko / "settings").mkdir()
        (ko / "settings" / "t3.lua").write_text(
            '-- ./settings/t3.lua\nreturn {\n    ["bridge_url"] = "http://old",\n    ["follow"] = true,\n}\n')
        dest, settings = install.install(self.root, 'http://10.0.0.2:8787', 'a"b\\c')
        self.assertEqual(dest, (ko / "plugins" / "t3.koplugin").resolve())
        self.assertTrue((dest / "main.lua").exists())
        self.assertFalse((dest / "stale.lua").exists())  # replaced, not merged
        text = settings.read_text()
        self.assertIn('["bridge_url"] = "http://10.0.0.2:8787",', text)
        self.assertIn('["bridge_token"] = "a\\"b\\\\c",', text)
        self.assertIn('["follow"] = true,', text)
        self.assertNotIn("http://old", text)

    def test_writes_new_settings_and_refuses_strangers(self):
        from t3bridge import install
        (self.root / "plugins").mkdir()  # given KOReader's folder itself
        _, settings = install.install(self.root, "https://kode.example.com", "")
        self.assertEqual(settings.read_text(),
                         'return {\n    ["bridge_token"] = "",\n    ["bridge_url"] = "https://kode.example.com",\n}\n')
        other = self.root / "plugins" / "t3.koplugin"
        for f in other.iterdir():
            f.unlink()
        (other / "_meta.lua").write_text('return { name = "someone-else" }')
        with self.assertRaises(install.InstallError):
            install.install(self.root, "http://x", "")
        with self.assertRaises(install.InstallError):
            install.install(self.root / "nowhere", "http://x", "")

    def test_bridge_url_needs_a_reachable_host(self):
        from t3bridge.install import InstallError, bridge_url
        self.assertEqual(bridge_url({"BRIDGE_HOST": "100.64.1.2", "BRIDGE_PORT": "8787"}), "http://100.64.1.2:8787")
        for host in ("127.0.0.1", "0.0.0.0", "localhost"):
            with self.assertRaises(InstallError):
                bridge_url({"BRIDGE_HOST": host})



class UpdateTests(unittest.TestCase):
    """Release checks, self-update and rollback against real git repos in a temp folder."""

    def setUp(self):
        import subprocess
        import tempfile
        from pathlib import Path
        self.sp = subprocess
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.origin, self.repo = root / "origin.git", root / "clone"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=root)
        self.git("remote", "add", "origin", str(self.origin))
        for d in ("bridge", "plugin/t3.koplugin"):
            (self.repo / d).mkdir(parents=True)
        self.commit("0.1.1", "first")
        self.base = self.rev("HEAD")
        self.git("push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self.dir.cleanup()

    def git(self, *args, cwd=None):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": __import__("os").environ["PATH"]}
        return self.sp.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True,
                           text=True, env=env).stdout.strip()

    def rev(self, ref):
        return self.git("rev-parse", ref)

    def commit(self, version, msg):
        (self.repo / "plugin/t3.koplugin/_meta.lua").write_text(f'return {{ name = "t3", version = "{version}" }}\n')
        (self.repo / "bridge/v.txt").write_text(f"{version} {msg}")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def release(self, version):
        """A newer release on the remote, made from another clone."""
        other = self.repo.parent / f"rel-{version}"
        self.git("clone", "-q", str(self.origin), str(other), cwd=self.repo.parent)
        (other / "plugin/t3.koplugin/_meta.lua").write_text(f'return {{ name = "t3", version = "{version}" }}\n')
        self.git("commit", "-q", "-am", f"release {version}", cwd=other)
        self.git("tag", f"v{version}", cwd=other)
        self.git("push", "-q", "--tags", "origin", "HEAD:main", cwd=other)

    def updater(self, ok=True, version="0.1.1"):
        import sys
        from t3bridge.update import Updater
        return Updater(repo=self.repo, version=version,
                       test_command=[sys.executable, "-c", f"raise SystemExit({0 if ok else 1})"])

    def test_versions_match(self):
        from t3bridge import __version__
        from t3bridge.update import plugin_version
        self.assertEqual(plugin_version(), __version__)  # bump both for a release

    def test_check_finds_the_newest_release_only(self):
        up = self.updater()
        up.check()
        self.assertIsNone(up.latest)
        self.release("0.1.2")
        self.git("tag", "v0.3.0-rc1", cwd=self.repo)  # not a release (and only local)
        up.check()
        self.assertEqual(up.latest, "v0.1.2")
        info = up.info()
        self.assertEqual((info["release"], info["self_update"], info["plugin"]), ("0.1.2", True, "0.1.1"))
        self.assertIsNone(self.updater(version="0.1.2").check() or self.updater(version="0.1.2").latest)

    def test_update_tests_first_then_fast_forwards_and_rolls_back(self):
        from t3bridge.update import rollback
        self.release("0.1.2")
        up = self.updater()
        up.check()
        up.update_to(up.latest)
        self.assertEqual(self.rev("HEAD"), self.rev("v0.1.2^{commit}"))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")  # still on the branch
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)  # test checkout removed
        self.assertIn("0.1.1", rollback(repo=self.repo))
        self.assertEqual(self.rev("HEAD"), self.base)

    def test_failed_tests_change_nothing(self):
        from t3bridge.update import UpdateError
        self.release("0.1.2")
        up = self.updater(ok=False)
        up.check()
        with self.assertRaises(UpdateError) as e:
            up.update_to(up.latest)
        self.assertIn("tests failed", str(e.exception))
        self.assertEqual(self.rev("HEAD"), self.base)
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_refuses_local_changes_and_diverged_checkouts(self):
        from t3bridge.update import UpdateError
        self.release("0.1.2")
        up = self.updater()
        up.check()
        (self.repo / "bridge/v.txt").write_text("edited")
        self.assertIn("local changes", up.blocker())
        self.assertFalse(up.info()["self_update"])
        self.assertIn("git pull", up.info()["manual"])
        with self.assertRaises(UpdateError):
            up.start(lambda: None)
        self.git("checkout", "--", ".")
        self.commit("0.1.1", "a local commit")
        with self.assertRaises(UpdateError) as e:
            up.update_to(up.latest)
        self.assertIn("by hand", str(e.exception))

    def test_start_runs_in_the_background_and_restarts(self):
        import time as _time
        self.release("0.1.2")
        up = self.updater()
        up.check()
        restarted = threading.Event()
        up.start(restarted.set)
        self.assertTrue(restarted.wait(30))
        self.assertEqual(up.status, "restarting")
        self.assertEqual(self.rev("HEAD"), self.rev("v0.1.2^{commit}"))
        _time.sleep(0)

    def test_failure_can_be_retried_and_a_newer_release_clears_it(self):
        self.release("0.1.2")
        up = self.updater(ok=False)
        up.check()
        for _attempt in range(2):  # a failed update can be tried again
            up.start(lambda: None)
            for _i in range(300):
                if up.status != "updating":
                    break
                time.sleep(0.05)
            self.assertEqual((up.status, up.failed_tag), ("failed", "v0.1.2"))
            self.assertIn("tests failed", up.info()["message"])
        up.check()  # the same release: the failure still applies
        self.assertEqual(up.status, "failed")
        self.release("0.1.3")
        up.check()
        self.assertEqual((up.status, up.latest, up.info().get("message")), ("idle", "v0.1.3", None))

    def test_no_remote_is_quiet(self):
        self.git("remote", "remove", "origin")
        up = self.updater()
        up.check()
        self.assertIsNone(up.latest)
        self.assertTrue(up.last_failed)

    def test_plugin_files_are_served_by_name_only(self):
        import hashlib
        from t3bridge.update import UpdateError, plugin_file, plugin_manifest
        m = plugin_manifest()
        names = [f["path"] for f in m["files"]]
        self.assertIn("main.lua", names)
        self.assertIn("cacert.pem", names)
        main = next(f for f in m["files"] if f["path"] == "main.lua")
        self.assertEqual(hashlib.sha256(plugin_file("main.lua")).hexdigest(), main["sha256"])
        for bad in ("../README.md", ".hidden", "nope.lua", "sub/main.lua"):
            with self.assertRaises(UpdateError):
                plugin_file(bad)


class UpdateHttpTests(HttpTests):
    def setUp(self):
        from t3bridge.update import Updater
        super().setUp()
        self.b.updater = Updater(enabled=False)

    def test_update_routes(self):
        status, body = self.req("/v1/threads")
        self.assertEqual((status, body["update"]["bridge"], body["update"]["release"]),
                         (200, __import__("t3bridge").__version__, None))
        status, manifest = self.req("/v1/plugin")
        self.assertEqual(status, 200)
        self.assertTrue(any(f["path"] == "t3_api.lua" for f in manifest["files"]))
        r = urllib.request.Request(self.base + "/v1/plugin/files/t3_api.lua",
                                   headers={"authorization": "Bearer secret"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            self.assertIn(b"HTTP client", resp.read())
        self.assertEqual(self.req("/v1/plugin/files/..%2Fx")[0], 404)
        self.assertEqual(self.req("/v1/plugin", token="wrong")[0], 401)
        self.assertEqual(self.req("/v1/update", {"action": "update"})[0], 409)  # no restart, no release
        self.assertEqual(self.req("/v1/update", {"action": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
