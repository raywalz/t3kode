"""Turn T3 data into the small, plain-text shapes the device shows.

Pure functions only, so they're easy to test against recorded transcripts.
E-ink fonts may lack emoji, so output sticks to common symbols (▸ •).
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex

from .markdown import text_html, to_html

LIVE_STATUSES = {"queued", "preparing", "starting", "running", "waiting"}

TOOL_MARK = "▸"
NOTE_MARK = "•"


def short_id(long_id: str) -> str:
    """Stable 8-character alias for an opaque T3 ID."""
    digest = hashlib.sha1(long_id.encode()).digest()
    return base64.b32encode(digest).decode()[:8].lower()


def thread_state(thread: dict) -> str:
    """needs_you | working | failed | done | idle"""
    if thread.get("pendingRuntimeRequest"):
        return "needs_you"
    status = thread.get("status")
    if status in LIVE_STATUSES:
        return "working"
    if status == "failed":
        return "failed"
    if status == "idle" and not thread.get("latestRunCompletedAt"):
        return "idle"
    return "done"


def is_unread(thread: dict) -> bool:
    finished = thread.get("latestRunCompletedAt")
    visited = thread.get("lastVisitedAt")
    return bool(finished) and (not visited or visited < finished)


# ---- text cleanup -----------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.M)
_FENCE = re.compile(r"^```.*$\n?", re.M)
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$\n?", re.M)


def clean_text(text: str) -> str:
    """Strip the markdown that reads badly as plain text."""
    text = _FENCE.sub("", text)
    text = _LINK.sub(lambda m: m.group(1) if m.group(1) != m.group(2) else m.group(2), text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\1", text)
    text = _TABLE_SEP.sub("", text)
    text = _CODE.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}{NOTE_MARK} ", text)
    return text.strip()


# ---- tool calls -------------------------------------------------------------

_SKIP_TOOLS = {"ToolSearch", "TodoWrite", "AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
_WRAPPERS = {"sudo", "timeout", "time", "nohup", "env", "exec", "xargs", "nice"}


def _base(path: str) -> str:
    return posixpath.basename(str(path).rstrip("/")) or str(path)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
_PROGRAM = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
_BUILTINS = {"cd", "set", "export", "echo", "true", "false", "printf", "test", "[", "[[",
             "source", ".", "unset", "local", "read", "exit", "return", "done", "fi",
             "for", "in", "esac", "case"}
_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!"}
_SEPARATORS = {"&&", "||", ";", "&", "(", ")", "{", "}"}


def _tokens(command: str) -> list[str]:
    # Newlines separate commands. Inside quotes the swap only changes the quoted text.
    lex = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|(){}")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes
        return command.split()


def command_programs(command: str) -> list[str]:
    """The programs a shell command starts, skipping builtins, env/sudo wrappers,
    pipe filters (`| head`), and heredoc bodies. ssh/scp keep their host."""
    command = _HEREDOC.sub("", command)
    out: list[str] = []
    start, after_pipe = True, False
    words: list[str] = []

    def take(words: list[str], piped: bool) -> None:
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in _WRAPPERS
                         or words[0] in _KEYWORDS or re.match(r"^-|^\d+[smh]?$", words[0])):
            words.pop(0)
        if piped or not words or words[0] in _BUILTINS:
            return
        prog = _base(words[0])
        if not _PROGRAM.match(prog):
            return
        if prog in ("ssh", "scp") and len(words) > 1:
            host = next((w for w in words[1:] if not w.startswith("-")), "")
            host = host.split("@")[-1].split(":")[0]
            prog = f"{prog} {host}" if host and _PROGRAM.match(host) else prog
        if prog not in out:
            out.append(prog)

    for tok in _tokens(command):
        if tok in _SEPARATORS or tok == "|" or tok.startswith(("|", ";", "&")):
            if words:
                take(words, after_pipe)
            words = []
            after_pipe = tok in ("|", "|&")
            start = True
            continue
        if start:
            words.append(tok)
    if words:
        take(words, after_pipe)
    return out


def describe_tool(item: dict) -> tuple[str, list[str]] | None:
    """(verb, objects) for a tool-like item, or None to leave it out."""
    kind = item.get("type")
    if kind == "command_execution":
        if item.get("input") == "Preparing workspace":
            return None
        return "ran", command_programs(item.get("input", "")) or ["a command"]
    if kind == "file_change":
        return "edited", [_base(item.get("fileName", "a file"))]
    if kind == "web_search":
        return "searched", ["the web"]
    if kind == "file_search":
        return "searched", ["files"]
    if kind == "subagent":
        return "delegated", ["a task"]
    if kind == "dynamic_tool":
        name = item.get("toolName") or "a tool"
        args = item.get("input") if isinstance(item.get("input"), dict) else {}
        if name in _SKIP_TOOLS:
            return None
        if name == "Read":
            return "read", [_base(args.get("file_path", "a file"))]
        if name in _EDIT_TOOLS:
            return "edited", [_base(args.get("file_path") or args.get("notebook_path") or "a file")]
        if name in ("Grep", "Glob"):
            return "searched", ["files"]
        if name == "WebSearch":
            return "searched", ["the web"]
        if name == "WebFetch":
            url = str(args.get("url", ""))
            return "fetched", [re.sub(r"^https?://([^/]+).*$", r"\1", url) or "a page"]
        if name == "Bash":
            return "ran", command_programs(str(args.get("command", ""))) or ["a command"]
        if name in ("Agent", "Task"):
            return "delegated", ["a task"]
        # mcp__server__tool and server.tool → tool
        short = re.split(r"__|\.", name)[-1]
        return "used", [short]
    return None


def summarize_tools(group: list[tuple[str, list[str], bool]], width: int = 90) -> str:
    """One line for a run of tool calls: '▸ ran ssh kobo, grep · edited a.py (1 failed)'."""
    by_verb: dict[str, list[str]] = {}
    failed = 0
    for verb, objs, ok in group:
        bucket = by_verb.setdefault(verb, [])
        for o in objs:
            if o not in bucket:
                bucket.append(o)
        failed += 0 if ok else 1
    parts = []
    for verb, objs in by_verb.items():
        shown = ", ".join(objs[:3])
        if len(objs) > 3:
            shown += f" +{len(objs) - 3}"
        parts.append(f"{verb} {shown}" if shown else verb)
    line = f"{TOOL_MARK} " + " · ".join(parts)
    if failed:
        line += f" ({failed} failed)"
    return line if len(line) <= width else line[: width - 1] + "…"


# ---- transcript -------------------------------------------------------------


def _note(item: dict) -> str | None:
    kind = item.get("type")
    if kind == "notification":
        return item.get("summary") or item.get("title")
    if kind == "system_notice":
        return item.get("message")
    if kind == "error":
        failure = item.get("failure") or {}
        return "Error: " + str(failure.get("message") or item.get("title") or "unknown")[:300]
    if kind == "run_interrupt_result":
        return "Stopped"
    if kind == "secret_request":
        return f"Asked for a secret ({item.get('label', 'value')}). Answer it on another device."
    if kind == "link_request":
        return "Asked to link an environment. Answer it on another device."
    if kind == "handoff":
        return f"Switched to {item.get('toModel') or 'another model'}"
    if kind == "approval_request":
        return f"Approval: {item.get('prompt') or item.get('reason') or item.get('requestKind')}"
    if kind == "user_input_request":
        lines = []
        answers = (item.get("questionAnswer") or {}).get("answers") or {}
        for q in item.get("questions", []):
            a = answers.get(q.get("id"))
            if isinstance(a, list):
                a = ", ".join(map(str, a))
            lines.append(f"Q: {q.get('question')}" + (f" → {a}" if a else ""))
        return "\n".join(lines) or None
    return None


_TOOL_KINDS = {"command_execution", "file_change", "web_search", "file_search", "subagent",
               "dynamic_tool"}
_SILENT = {"reasoning", "checkpoint", "todo_list", "compaction", "thread_created", "fork",
           "run_interrupt_request"}


def render_items(items: list[dict]) -> list[dict]:
    """Turn items → entries [{o, role: you|agent|tool|note, text}], tool runs collapsed.

    `o` is the ordinal of the entry's last item, so `o > cursor` finds new entries.
    In-progress assistant text is left out; it appears once it finishes.
    """
    entries: list[dict] = []
    group: list[tuple[str, list[str], bool]] = []
    group_o = 0

    def flush():
        nonlocal group
        if group:
            line = summarize_tools(group)
            entries.append({"o": group_o, "role": "tool", "text": line, "html": text_html(line)})
            group = []

    for item in items:
        kind = item.get("type")
        o = item.get("ordinal", 0)
        if kind in _TOOL_KINDS:
            tool = describe_tool(item)
            if tool is not None:
                group.append((tool[0], tool[1], item.get("status") != "failed"))
                group_o = o
            continue
        if kind in _SILENT:
            continue
        if kind == "assistant_message":
            if item.get("streaming") or not item.get("text", "").strip():
                continue
            flush()
            entries.append({"o": o, "role": "agent", "text": clean_text(item["text"]),
                            "html": to_html(item["text"])})
        elif kind == "proposed_plan":
            if item.get("streaming"):
                continue
            flush()
            plan = item.get("markdown", "")
            entries.append({"o": o, "role": "agent", "text": "Plan:\n" + clean_text(plan),
                            "html": "<h4>Plan</h4>" + to_html(plan)})
        elif kind == "user_message":
            text = item.get("text", "").strip()
            if not text:
                continue
            flush()
            if item.get("createdBy", "user") == "user":
                entries.append({"o": o, "role": "you", "text": text, "html": text_html(text)})
            else:
                # Sent by an agent or by T3 (task prompts, wake-ups). Shown, but marked.
                clipped = text if len(text) <= 600 else text[:599] + "…"
                entries.append({"o": o, "role": "note", "text": clipped, "html": text_html(clipped)})
        else:
            note = _note(item)
            if note is None:
                if kind in _SILENT or kind is None:
                    continue
                # Unknown kind: show that something happened rather than hide it.
                group.append((str(kind).replace("_", " "), [], item.get("status") != "failed"))
                group_o = o
                continue
            flush()
            entries.append({"o": o, "role": "note", "text": note, "html": text_html(note)})
    flush()
    return entries


def preview(entries: list[dict], width: int = 80) -> str:
    """Last agent line, for the thread list."""
    for e in reversed(entries):
        if e["role"] in ("agent", "note"):
            line = " ".join(e["text"].split())
            return line if len(line) <= width else line[: width - 1] + "…"
    return ""
