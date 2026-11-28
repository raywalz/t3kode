"""Usage stats: what the agents cost and how many tokens they used, from T3's
`server.getUsageSummary` (the numbers on the app's Usage page).

Pure functions over its result, so they're easy to test.
"""

from __future__ import annotations

import html
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PERIODS = {"today": "Today", "week": "Last 7 days", "month": "This month"}

# Rows cheaper than this fold into one "Other" row.
SMALL_COST = 0.01


def local_time_zone() -> str:
    """The bridge machine's IANA zone (T3 buckets days in it), or UTC."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz and not tz.startswith("/"):
        return tz
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return "UTC"


def window(period: str, today: date) -> tuple[date, date]:
    """First and last day (inclusive) of `period`, ending today."""
    if period == "today":
        return today, today
    if period == "month":
        return today.replace(day=1), today
    return today - timedelta(days=6), today


def _tokens(totals: dict) -> int:
    return sum(int(totals.get(k) or 0) for k in
               ("uncachedInputTokens", "cachedInputTokens", "cacheCreationTokens", "outputTokens"))


def money(usd: float) -> str:
    if usd >= 1000:
        return f"${usd:,.0f}"
    if 0 < usd < 0.01:
        return "<$0.01"
    return f"${usd:,.2f}"


def _esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def _day_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%a} {d:%b} {d.day}"


def _hour_label(hour_start: str, tz: str) -> str:
    """`hourStart` is a UTC instant; the hour is shown in the summary's zone."""
    try:
        t = datetime.fromisoformat(hour_start.replace("Z", "+00:00"))
        if t.tzinfo:
            t = t.astimezone(ZoneInfo(tz))
    except (ValueError, KeyError):
        return hour_start
    return f"{t.hour:02d}:00"


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


def summary_html(summary: dict, period: str, fmt_tokens) -> str:
    """The Usage page for one period. `fmt_tokens`: how token counts are written (9.5k, 1.2m)."""
    base = fmt_tokens

    def fmt_tokens(n: float) -> str:  # noqa: F811 - months run into billions
        return f"{n / 1e9:.1f}".removesuffix(".0") + "b" if n >= 999_950_000 else base(n)

    buckets = [b for b in summary.get("buckets") or [] if isinstance(b, dict)]
    sources = [s for s in summary.get("sources") or [] if isinstance(s, dict)]
    cost = sum(b.get("costUsd") or 0 for b in buckets)
    tokens = sum(_tokens(b.get("totals") or {}) for b in buckets)
    sessions = sum(int(s.get("distinctSessions") or 0) for s in sources)

    since, until = summary.get("sinceDay"), summary.get("untilDay")
    span = _day_label(since) if since == until else f"{_day_label(since)} to {_day_label(until)}"
    out = [f"<p><b>{money(cost)}</b> · {fmt_tokens(tokens)} tokens"
           + (f" · {sessions} session{'' if sessions == 1 else 's'}" if sessions else "") + "</p>",
           f'<p class="note">{_esc(span)}</p>']
    if not buckets:
        out.append("<p>No agent usage in this period.</p>")

    by_kind = defaultdict(int)
    saved = 0.0
    for b in buckets:
        for k, v in (b.get("totals") or {}).items():
            by_kind[k] += int(v or 0)
        saved += b.get("cacheSavingsUsd") or 0
    if tokens:
        parts = [f"{fmt_tokens(by_kind['uncachedInputTokens'])} input",
                 f"{fmt_tokens(by_kind['cachedInputTokens'])} cache read",
                 f"{fmt_tokens(by_kind['cacheCreationTokens'])} cache write",
                 f"{fmt_tokens(by_kind['outputTokens'])} output"]
        line = ", ".join(parts)
        if saved >= 0.01:
            line += f". Caching saved {money(saved)}."
        out.append(f'<p class="note">{_esc(line)}</p>')

    # By model, the costliest first; the cheap tail in one row.
    models: dict[tuple, list] = defaultdict(lambda: [0.0, 0])
    for b in buckets:
        m = models[(b.get("provider") or "?", b.get("model") or "?")]
        m[0] += b.get("costUsd") or 0
        m[1] += _tokens(b.get("totals") or {})
    if models:
        ranked = sorted(models.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
        keep = [kv for kv in ranked if kv[1][0] >= SMALL_COST]
        small = [kv for kv in ranked if kv[1][0] < SMALL_COST]
        if len(ranked) <= 6 or len(small) < 2:
            keep, small = ranked, []
        rows = [[model, money(c), fmt_tokens(t)] for (_p, model), (c, t) in keep]
        if small:
            rows.append([f"{len(small)} others", money(sum(c for _k, (c, _t) in small)),
                         fmt_tokens(sum(t for _k, (_c, t) in small))])
        out.append("<h3>By model</h3>" + _table(["Model", "Cost", "Tokens"], rows))

    # Providers, when there's more than one.
    providers: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for b in buckets:
        p = providers[b.get("provider") or "?"]
        p[0] += b.get("costUsd") or 0
        p[1] += _tokens(b.get("totals") or {})
    if len(providers) > 1:
        rows = [[p, money(c), fmt_tokens(t)]
                for p, (c, t) in sorted(providers.items(), key=lambda kv: -kv[1][0])]
        out.append("<h3>By provider</h3>" + _table(["Provider", "Cost", "Tokens"], rows))

    # Over time: hours for today, days otherwise; the latest first.
    hourly = period == "today"
    slots: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for b in buckets:
        key = (b.get("hourStart") or "") if hourly else (b.get("day") or "")
        if key:
            s = slots[key]
            s[0] += b.get("costUsd") or 0
            s[1] += _tokens(b.get("totals") or {})
    if len(slots) > 1:
        tz = summary.get("timeZone") or "UTC"
        label = (lambda k: _hour_label(k, tz)) if hourly else _day_label
        rows = [[label(k), money(c), fmt_tokens(t)] for k, (c, t) in sorted(slots.items(), reverse=True)]
        out.append(f"<h3>{'By hour' if hourly else 'By day'}</h3>"
                   + _table(["Hour" if hourly else "Day", "Cost", "Tokens"], rows))

    notes = []
    unpriced = sum(int(b.get("unpricedRecords") or 0) for b in buckets)
    if unpriced:
        notes.append(f"{unpriced} record{'' if unpriced == 1 else 's'} had no price, so the cost is low.")
    for s in sources:
        if s.get("status") in ("failed", "partial"):
            fp = s.get("fingerprint") or {}
            what = "couldn't be read" if s["status"] == "failed" else "was only partly read"
            msg = f"{fp.get('provider') or 'A provider'}'s history {what}"
            notes.append(msg + (f": {s['message']}" if s.get("message") else "."))
    if any(s.get("refreshing") for s in sources):
        notes.append("T3 is still scanning; Refresh shows newer numbers.")
    pricing = summary.get("pricing") or {}
    if pricing.get("status") == "unavailable":
        notes.append("T3 couldn't load model prices, so costs may be missing.")
    notes.append("Costs are estimates from public API prices, as in T3's Usage page.")
    out.append("".join(f'<p class="note">{_esc(n)}</p>' for n in notes))
    return "".join(out)
