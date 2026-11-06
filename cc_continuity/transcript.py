"""Session transcript export for the clickable transcript link (issue #8).

At checkpoint finalize the ending session's conversation is exported as a
readable HTML page plus a canonical Markdown file inside the checkpoint
dir; the successor session echoes the HTML's ``file://`` URL as one
``Transcript:`` line in its navigator panel.

This module is strictly best-effort: an export failure must never break
finalize, validation, or resume (P9-D7). Stdlib only, English strings.
"""

from __future__ import annotations

import html
import json
import re

#: One conversation turn: (role, text, ISO-8601 timestamp or None).
Block = tuple[str, str, str | None]

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_MULTI_NL = re.compile(r"\n{3,}")
_LIST_RE = re.compile(r"^\s*([-*]|\d+\.)\s+")
_SEP_ROW_RE = re.compile(r"^[\s:|-]+$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def md_inline(s: str) -> str:
    """Inline markdown on escaped text: code spans, bold, italic."""
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    return s


def _is_table_start(lines: list[str], i: int) -> bool:
    return ("|" in lines[i] and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1] or "x"))


def md_to_html(md: str) -> str:
    """Block-level markdown subset to an HTML fragment.

    Fenced code to <pre>, ``#``..``######`` to h2..h6 (h1 maps to h2),
    pipe tables (first row <th>), ``-``/``*``/``1.`` lists, consecutive
    plain lines joined into one <p>. Unrecognized input is escaped text,
    never live HTML.
    """
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
            continue
        if ln.startswith("#"):
            lvl = min(len(ln) - len(ln.lstrip("#")) + 1, 6)
            out.append(f"<h{lvl}>" + md_inline(ln.lstrip("#").strip()) + f"</h{lvl}>")
            i += 1
            continue
        if _is_table_start(lines, i):
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i]:
                if not _SEP_ROW_RE.match(lines[i].strip().strip("|")):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            tbl = ["<table>"]
            for n, r in enumerate(rows):
                tag = "th" if n == 0 else "td"
                tbl.append("<tr>" + "".join(f"<{tag}>{md_inline(c)}</{tag}>" for c in r) + "</tr>")
            out.append("".join(tbl) + "</table>")
            continue
        if _LIST_RE.match(ln):
            ordered = bool(re.match(r"^\s*\d+\.", ln))
            items = []
            while i < len(lines) and _LIST_RE.match(lines[i]):
                items.append("<li>" + md_inline(_LIST_RE.sub("", lines[i]).strip()) + "</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if ln.strip():
            para = []
            while (i < len(lines) and lines[i].strip()
                   and not lines[i].startswith("#")
                   and not lines[i].strip().startswith("```")
                   and not _is_table_start(lines, i) and not _LIST_RE.match(lines[i])):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>" + md_inline(" ".join(para)) + "</p>")
            continue
        i += 1
    return "\n".join(out)


def parse_conversation(lines: list[str]) -> list[Block]:
    """Text turns of user/assistant records in order, noise removed.

    Unparseable JSONL lines are skipped silently; tool-use payloads,
    system/summary records, ``<system-reminder>`` spans, and
    local-command/caveat noise never reach the output. Runs of 3+
    newlines in assistant text collapse to exactly two.
    """
    out: list[Block] = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        else:
            continue
        text = "\n".join(t for t in parts if t and t.strip())
        if not text:
            continue
        text = _REMINDER_RE.sub("", text).strip()
        if not text or text.startswith(("<local-command", "Caveat:")):
            continue
        if rec["type"] == "assistant":
            text = _MULTI_NL.sub("\n\n", text)
        ts = rec.get("timestamp")
        out.append((rec["type"], text, ts if isinstance(ts, str) else None))
    return out
