#!/usr/bin/env python3
"""Step 1 smoke test: list threads, create a throwaway thread, send to it, read it back.

Only ever sends messages to the thread it creates. Deletes that thread at the end
unless --keep is given.

    python3 bridge/api_test.py --project <project id or title> [--model instanceId/model]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t3bridge.core import first_ready_model  # noqa: E402
from t3bridge.t3client import T3Client  # noqa: E402

BUSY = {"queued", "preparing", "starting", "running", "waiting"}  # LIVE_SHELL_STATUSES upstream


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def find_thread(shell: dict, thread_id: str) -> dict | None:
    return next((t for t in shell["threads"] if t["id"] == thread_id), None)


def wait_idle(client: T3Client, thread_id: str, min_items: int, timeout: float = 180) -> dict:
    """Poll the shell snapshot until the thread has new items and is no longer busy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = find_thread(client.shell_snapshot(), thread_id)
        if t and t["status"] not in BUSY and t["itemCount"] >= min_items:
            return t
        time.sleep(2)
    raise TimeoutError(f"thread {thread_id} still busy after {timeout}s")


def item_count(client: T3Client, thread_id: str) -> int:
    t = find_thread(client.shell_snapshot(), thread_id)
    return t["itemCount"] if t else 0


def wait_pending(client: T3Client, thread_id: str, baseline: int, timeout: float = 180) -> dict:
    """Wait for a pending request; fail if the run finishes without one."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = client.pending_requests(thread_id)
        if pending:
            return pending[0]
        t = find_thread(client.shell_snapshot(), thread_id)
        # baseline + 2: our message plus at least one agent item, so the run really started.
        if t and t["status"] not in BUSY and t["itemCount"] >= baseline + 2:
            raise RuntimeError(f"thread went {t['status']} without asking anything")
        time.sleep(2)
    raise TimeoutError("no pending request appeared")


def exercise_requests(client: T3Client, tid: str) -> None:
    # Approval: in approval-required mode a shell command needs the user's OK. Decline it.
    # (Read-only commands such as `echo` may be auto-approved, so ask for a file write.)
    n = item_count(client, tid)
    client.send_message(tid, "Use your file-writing tool to create /tmp/t3kode-approval-test.txt "
                             "containing 'hi'. If it is declined, reply DECLINED and stop.")
    p = wait_pending(client, tid, n)
    req, item = p["request"], p["item"] or {}
    print(f"\n[approval] kind={req['kind']} capability={req['responseCapability']['type']}")
    print(f"  prompt: {str(item.get('prompt') or item.get('title'))[:100]}")
    print(f"  options: {item.get('options')}")
    client.respond_approval(tid, req["id"], "decline")
    t = wait_idle(client, tid, min_items=n + 2)
    print(f"  declined -> thread {t['status']}; last: {(t.get('latestVisibleMessage') or {}).get('text', '')[:60]!r}")

    # Question: ask the agent to use its multiple-choice question tool; pick the first option.
    n = item_count(client, tid)
    client.send_message(tid, "Use your AskUserQuestion tool (multiple choice) to ask me whether I "
                             "prefer red or blue. After I answer, reply with just my choice.")
    p = wait_pending(client, tid, n)
    req, item = p["request"], p["item"]
    q = item["questions"][0]
    labels = [o["label"] for o in q["options"]]
    print(f"\n[question] {q['question']!r} options={labels} multiSelect={q.get('multiSelect')}")
    client.respond_user_input(tid, req["id"], {q["id"]: labels[0]})
    t = wait_idle(client, tid, min_items=n + 3)
    print(f"  answered {labels[0]!r} -> thread {t['status']}; "
          f"last: {(t.get('latestVisibleMessage') or {}).get('text', '')[:60]!r}")


def summarize(item: dict) -> str:
    kind = item.get("type")
    if kind in ("user_message", "assistant_message"):
        who = "you" if kind == "user_message" else "agent"
        return f"{who}: {item.get('text', '').strip()}"
    if kind == "checkpoint":
        return "· checkpoint"
    label = item.get("toolName") or item.get("title") or item.get("input") or ""
    return f"▸ {kind} {str(label)[:60]}"


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.environ.get("T3_TEST_PROJECT"),
                    help="project id or title for the throwaway thread")
    ap.add_argument("--model", default=os.environ.get("T3_TEST_MODEL"),
                    help="instanceId/model (default: T3's default model for the project)")
    ap.add_argument("--keep", action="store_true", help="don't delete the test thread")
    ap.add_argument("--requests", action="store_true",
                    help="also exercise a tool approval (declined) and a multiple-choice question")
    args = ap.parse_args()

    client = T3Client.from_env()
    env = client.environment()
    print(f"server {env['serverVersion']}, orchestration protocol {env['orchestrationProtocolVersion']}")

    # 1. List threads, grouped by project.
    shell = client.shell_snapshot()
    projects = {p["id"]: p for p in shell["projects"]}
    by_project: dict[str, list] = defaultdict(list)
    for t in shell["threads"]:
        by_project[t["projectId"]].append(t)
    print(f"\n[list] {len(shell['threads'])} threads in {len(by_project)} projects")
    for pid, threads in list(by_project.items())[:3]:
        title = projects.get(pid, {}).get("title", pid)
        print(f"  {title}: {len(threads)} threads, e.g. [{threads[0]['status']}] {threads[0]['title'][:50]}")

    if not args.project:
        print("\nPass --project (id or title) to run the write test.")
        return 0
    project = projects.get(args.project) or next(
        (p for p in shell["projects"] if p["title"] == args.project), None)
    if project is None:
        print(f"no project {args.project!r}")
        return 1
    if args.model:
        instance, model = args.model.split("/", 1)
        selection = {"instanceId": instance, "model": model}
    else:
        config = client.server_config()
        settings = config.get("settings") or {}
        override = (settings.get("projectSettingsOverrides") or {}).get(project["id"]) or {}
        selection = (override.get("defaultModelSelection") or project.get("defaultModelSelection")
                     or settings.get("defaultModelSelection") or settings.get("fallbackModelSelection")
                     or first_ready_model(config.get("providers") or []))
        if not selection:
            print("No agent is ready in T3; pass --model instanceId/model")
            return 1

    # 2. Create a throwaway thread with a first message.
    launched = client.launch_thread(
        project["id"],
        "T3 KOde API test (safe to delete)",
        "This is an automated API test. Reply with exactly the word PONG and nothing else. "
        "Do not use any tools.",
        selection,
    )
    tid = launched["threadId"]
    print(f"\n[create] thread {tid} in project {project['title']!r}")
    try:
        t = wait_idle(client, tid, min_items=2)
        print(f"  status {t['status']}, {t['itemCount']} items")

        # 3. Read it.
        items = [e["item"] for e in client.thread_transcript(tid)["items"]]
        print("[read]")
        for it in items:
            print("  " + summarize(it))
        last_ordinal = max(it["ordinal"] for it in items)

        # 4. Send a follow-up to the same (own) thread.
        res = client.send_message(tid, "Now reply with exactly the word PING and nothing else.")
        print(f"\n[send] accepted at sequence {res['sequence']}")
        wait_idle(client, tid, min_items=len(items) + 2)

        # 5. Read only what is new since the last ordinal we saw.
        new = [e["item"] for e in client.thread_transcript(tid)["items"]
               if e["item"]["ordinal"] > last_ordinal]
        print("[read since]")
        for it in new:
            print("  " + summarize(it))

        if args.requests:
            exercise_requests(client, tid)
    finally:
        if args.keep:
            print(f"\nkept thread {tid}")
        else:
            client.delete_thread(tid)
            print(f"\n[cleanup] deleted thread {tid}")
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
