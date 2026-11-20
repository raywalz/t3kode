"""Small markdown → HTML converter for agent messages (stdlib only).

Covers what coding agents write: paragraphs, headings, bold/italic, inline code,
fenced code blocks, nested lists, block quotes, tables, rules, and links.
The output targets KOReader's HTML widget (MuPDF), so it stays plain:
no classes beyond a few, no scripts, no images.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline(text: str) -> str:
    """Escape, then apply inline markup. Code spans are protected from the rest."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash, text)
    text = html.escape(text, quote=False)
    # [label](url): keep the label, drop the URL (links can't be followed on the device).
    text = re.sub(r"\[([^\]]+)\]\((?:[^()\s]|\([^()]*\))+\)", r"<u>\1</u>", text)
    text = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<b>\2</b>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def _render_list(items: list[tuple[int, bool, str]]) -> str:
    """items: (indent, ordered, html) in order. Nested by indent."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)
    for indent, ordered, body in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            out.append("</li>")
        out.append(f"<li>{body}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para():
        if para:
            out.append("<p>" + "<br/>".join(inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush_para()
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            flush_para()
            mark = fence.group(1)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                body.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(body), quote=False) + "</pre>")
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_para()
            level = min(len(heading.group(1)) + 2, 6)  # h3–h6: messages, not documents
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _HR.match(line):
            flush_para()
            out.append("<hr/>")
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            head = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            t = ["<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("".join(t) + "</table>")
            continue
        if _QUOTE.match(line):
            flush_para()
            body = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                body.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>" + to_html("\n".join(body)) + "</blockquote>")
            continue
        if _LIST.match(line):
            flush_para()
            items: list[tuple[int, bool, str]] = []
            while i < len(lines):
                m = _LIST.match(lines[i])
                if m:
                    indent = len(m.group(1).expandtabs(4))
                    items.append((indent, m.group(2)[0].isdigit(), inline(m.group(3))))
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    # Continuation line of the previous item.
                    ind, o, body = items[-1]
                    items[-1] = (ind, o, body + " " + inline(lines[i].strip()))
                    i += 1
                else:
                    break
            out.append(_render_list(items))
            continue
        para.append(line.strip())
        i += 1
    flush_para()
    return "".join(out)


def text_html(text: str) -> str:
    """Plain text (your own messages, notes) as HTML: escaped, line breaks kept."""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p, quote=False).replace("\n", "<br/>") + "</p>" for p in paras)
