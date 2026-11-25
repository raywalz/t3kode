"""Pull requests the way the device shows them: list rows and a detail page.

Pure functions over T3's `pullRequests.*` results, so they're easy to test.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from .markdown import to_html

_CHECKS = {"passing": "checks pass", "failing": "checks fail", "pending": "checks running"}
_REVIEW = {"approved": "approved", "changes-requested": "changes requested",
           "review-required": "needs review"}

# Check statuses, worst first, with the mark each gets.
_CHECK_ORDER = ["failure", "action-required", "cancelled", "pending", "neutral", "skipped", "success"]
_CHECK_MARK = {"failure": "✗", "action-required": "!", "cancelled": "✗", "pending": "…",
               "neutral": "–", "skipped": "–", "success": "✓"}
_REVIEW_STATE = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes",
                 "COMMENTED": "reviewed", "DISMISSED": "review dismissed"}

MAX_FILES = 300  # file rows on the detail page; the rest are counted


def status(pr: dict) -> str:
    """One line like the app's PR row: "open · checks pass · needs review · conflicts".
    `pr` is a list entry, a detail, or a thread's PR snapshot (same field names)."""
    state = pr.get("state")
    if state == "open" and pr.get("isDraft"):
        state = "draft"
    parts = [state or "not synced"]
    if state in ("open", "draft"):
        parts += [label for key, labels in (("checksState", _CHECKS), ("reviewDecision", _REVIEW))
                  if (label := labels.get(pr.get(key) or ""))]
        if pr.get("mergeability") == "conflicting":
            parts.append("conflicts")
    return " · ".join(parts)


def checks_state(checks: list[dict]) -> str | None:
    """A detail's checks summed up the way list entries carry it."""
    statuses = {c.get("status") for c in checks}
    if not statuses:
        return None
    if statuses & {"failure", "action-required", "cancelled"}:
        return "failing"
    if "pending" in statuses:
        return "pending"
    return "passing"


def review_decision(activity: dict | None) -> str | None:
    """approved / changes-requested from each reviewer's latest review (a detail has no
    review decision of its own), or None when nobody has decided."""
    latest: dict[str, str] = {}
    for c in sorted((activity or {}).get("comments") or [], key=lambda c: c.get("createdAt") or ""):
        state = (c.get("reviewState") or "").upper()
        if c.get("kind") == "review" and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(c.get("author") or {}).get("login") or ""] = state
    if "CHANGES_REQUESTED" in latest.values():
        return "changes-requested"
    if "APPROVED" in latest.values():
        return "approved"
    return None


def detail_status(detail: dict, activity: dict | None) -> str:
    return status(detail | {"checksState": checks_state(detail.get("checks") or []),
                            "reviewDecision": review_decision(activity)})


def row(entry: dict, project_short: str) -> dict:
    """A list entry as the device shows it."""
    author = (entry.get("author") or {}).get("login")
    return {
        "project": project_short,
        "project_title": entry.get("projectTitle"),
        "repo": entry.get("repository"),
        "host": entry.get("host"),
        "number": entry.get("number"),
        "title": entry.get("title") or "",
        "status": status(entry),
        "draft": bool(entry.get("isDraft")),
        "author": author,
        "updated": entry.get("updatedAt"),
    }


def _date(iso: str | None) -> str:
    """'Nov 20' (this year) or 'Nov 20, 2025', in the bridge's local time."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    text = f"{d:%b} {d.day}"
    return text if d.year == datetime.now().year else f"{text}, {d.year}"


def _esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def _who(actor: dict | None) -> str:
    if not actor:
        return "someone"
    login = actor.get("login") or "someone"
    return f"{login} (bot)" if actor.get("isBot") or login.endswith("[bot]") else login


def _first_line(md: str, width: int = 140) -> str:
    """The first line of text in a comment, without markdown or HTML, for bots' comments."""
    for line in md.splitlines():
        line = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|[#*_`>|]", "", line).strip()
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line).strip(" -:")
        if line:
            return line if len(line) <= width else line[:width - 1].rstrip() + "…"
    return ""


def diff_files(patch: str) -> list[dict]:
    """Changed files in a unified diff: [{path, additions, deletions}] in diff order."""
    files: list[dict] = []
    cur = None
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            cur = {"path": m.group(2) if m else line[11:], "additions": 0, "deletions": 0}
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif line.startswith("rename from "):
            cur["from"] = line[12:]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            cur["additions"] += 1
        elif in_hunk and line.startswith("-"):
            cur["deletions"] += 1
    return files


def _section(title: str, body: str) -> str:
    return f'<h3>{_esc(title)}</h3>{body}' if body else ""


def detail_html(detail: dict, activity: dict | None, files: list[dict] | None,
                breakdown: dict | None, threads: list[dict]) -> str:
    """The detail page. `threads`: [{id (short), title}] linked to the PR in T3."""
    d = detail
    out = [f"<p><b>{_esc(d.get('title'))}</b></p>"]
    st = detail_status(d, activity)
    out.append(f"<p>{_esc(st)}</p>")

    meta = []  # the repository and number are in the title bar
    if d.get("author"):
        meta.append(f"by {_esc(_who(d['author']))}")
    meta.append(f"opened {_date(d.get('createdAt'))}")
    if d.get("mergedAt"):
        meta.append(f"merged {_date(d['mergedAt'])}")
    elif d.get("closedAt"):
        meta.append(f"closed {_date(d['closedAt'])}")
    else:
        meta.append(f"updated {_date(d.get('updatedAt'))}")
    out.append(f'<p class="note">{" · ".join(meta)}</p>')

    branch = f"{_esc(d.get('headBranch'))} → {_esc(d.get('baseBranch'))}"
    if d.get("baseComparison") == "behind" and d.get("behindBy"):
        branch += f" ({d['behindBy']} behind)"
    out.append(f'<p class="note">{branch}</p>')
    n = d.get("changedFiles") or 0
    size = f"+{d.get('additions', 0)} −{d.get('deletions', 0)} in {n} file{'' if n == 1 else 's'}"
    if breakdown:
        kinds = [f"{k} +{v['additions']} −{v['deletions']}" for k, v in breakdown.items()
                 if isinstance(v, dict) and (v.get("additions") or v.get("deletions"))]
        if len(kinds) > 1:
            size += " (" + ", ".join(kinds) + ")"
    out.append(f'<p class="note">{_esc(size)}</p>')
    if d.get("labels"):
        out.append('<p class="note">Labels: ' + ", ".join(_esc(lb.get("name")) for lb in d["labels"]) + "</p>")
    if d.get("autoMergeEnabled"):
        out.append('<p class="note">Auto-merge is on.</p>')

    if threads:
        out.append(_section("Threads", "".join(
            f'<p><a href="t3:thread:{t["id"]}">▸ {_esc(t["title"] or "Untitled")}</a></p>' for t in threads)))

    checks = sorted(d.get("checks") or [], key=lambda c: (
        _CHECK_ORDER.index(c["status"]) if c.get("status") in _CHECK_ORDER else 0, c.get("name") or ""))
    if checks:
        rows = []
        for c in checks:
            mark = _CHECK_MARK.get(c.get("status"), "•")
            desc = c.get("description")
            rows.append(f"<p>{mark} {_esc(c.get('name'))}"
                        + (f' <span class="note">· {_esc(desc)}</span>' if desc else "") + "</p>")
        passed = sum(1 for c in checks if c.get("status") in ("success", "skipped", "neutral"))
        out.append(_section(f"Checks ({passed}/{len(checks)} passed)", "".join(rows)))

    reviewers = [r for r in (activity or {}).get("reviewers") or d.get("reviewers") or []]
    if reviewers:
        out.append(_section("Reviewers", "<p>" + ", ".join(_esc(_who(r)) for r in reviewers) + "</p>"))

    body = (d.get("body") or "").strip()
    out.append(_section("Description", to_html(body) if body else '<p class="note">No description.</p>'))

    if activity:
        convo = []
        for c in sorted(activity.get("comments") or [], key=lambda c: c.get("createdAt") or ""):
            who = _who(c.get("author"))
            what = _REVIEW_STATE.get((c.get("reviewState") or "").upper(), "")
            if c.get("kind") == "review-comment" and c.get("path"):
                what = f"on {c['path']}"
            head = f"<b>{_esc(who)}</b>" + (f" {_esc(what)}" if what else "") + f" · {_date(c.get('createdAt'))}"
            text = (c.get("body") or "").strip()
            if (c.get("author") or {}).get("isBot") or who.endswith("(bot)"):
                line = _first_line(text)
                convo.append(f'<div class="fold"><p>{head}{": " + _esc(line) if line else ""}</p></div>')
                continue
            convo.append(f'<div class="entry"><div class="label">{head}</div>'
                         + (to_html(text) if text else "") + "</div>")
        if activity.get("commentsTruncated"):
            convo.append(f'<p class="note">Showing {len(activity.get("comments") or [])} of '
                         f'{activity.get("commentCount")} comments.</p>')
        out.append(_section("Conversation", "".join(convo)))

        threads_html = []
        for th in activity.get("reviewThreads") or []:
            where = th.get("path") or ""
            if th.get("line"):
                where += f":{th['line']}"
            tags = [t for t, on in (("resolved", th.get("isResolved")), ("outdated", th.get("isOutdated"))) if on]
            head = f"<b>{_esc(where)}</b>" + (f" ({', '.join(tags)})" if tags else "")
            comments = th.get("comments") or []
            if th.get("isResolved") or th.get("isOutdated"):
                n_c = th.get("commentCount") or len(comments)
                threads_html.append(f'<div class="fold"><p>{head} · {n_c} comment{"" if n_c == 1 else "s"}</p></div>')
                continue
            parts = [f'<div class="label">{head}</div>']
            for c in comments:
                parts.append(f"<p><b>{_esc(_who(c.get('author')))}</b> · {_date(c.get('createdAt'))}</p>"
                             + to_html((c.get("body") or "").strip()))
            threads_html.append('<div class="entry">' + "".join(parts) + "</div>")
        out.append(_section("Review comments", "".join(threads_html)))

        commits = activity.get("commits") or []
        if commits:
            out.append(_section(f"Commits ({len(commits)})", "".join(
                f"<p><code>{_esc((c.get('oid') or '')[:7])}</code> {_esc(c.get('messageHeadline'))}</p>"
                for c in commits)))

    if files:
        rows = []
        for f in files[:MAX_FILES]:
            name = _esc(f["path"])
            if f.get("from"):
                name = f"{_esc(f['from'])} → {name}"
            rows.append(f'<p>{name} <span class="note">+{f["additions"]} −{f["deletions"]}</span></p>')
        more = max(len(files), d.get("changedFiles") or 0) - min(len(files), MAX_FILES)
        if more > 0:
            rows.append(f'<p class="note">…and {more} more.</p>')
        out.append(_section(f"Files ({max(len(files), d.get('changedFiles') or 0)})", f'<div class="tool">{"".join(rows)}</div>'))

    return "".join(out)
