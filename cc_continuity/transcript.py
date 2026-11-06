"""Session transcript export for the clickable transcript link (issue #8).

At checkpoint finalize the ending session's conversation is exported as a
readable HTML page plus a canonical Markdown file inside the checkpoint
dir; the successor session echoes the HTML's ``file://`` URL as one
``Transcript:`` line in its navigator panel.

This module is strictly best-effort: an export failure must never break
finalize, validation, or resume (P9-D7). Stdlib only, English strings.
"""

from __future__ import annotations

import json
import re

#: One conversation turn: (role, text, ISO-8601 timestamp or None).
Block = tuple[str, str, str | None]

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_MULTI_NL = re.compile(r"\n{3,}")


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
