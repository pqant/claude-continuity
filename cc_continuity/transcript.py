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
from datetime import datetime

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


#: Approved v5 visual design (user-approved 2026-11-06), ported from the
#: throwaway spike; light vars + explicit dark theme + auto via
#: prefers-color-scheme. ASCII only.
_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--muted:#5b6672;--hdr:#f6f8fa;--line:#d8dee4;--user:#cde7d6;--userbg:#dcefe2;--userfg:#166f31;--asst:#eef2fb;--asstfg:#2f5fa8;--mark:#fff1a8;--code:#f0f2f5;--pre-bg:#0d1117;--pre-fg:#e6edf3}
[data-theme=dark]{--bg:#0d1117;--fg:#d5dde6;--muted:#8b949e;--hdr:#161b22;--line:#30363d;--user:#2a4d36;--userbg:#1e3a29;--userfg:#7ce09a;--asst:#151d2b;--asstfg:#8db3f5;--mark:#8a6d1a;--code:#1f2530;--pre-bg:#010409;--pre-fg:#e6edf3}
@media(prefers-color-scheme:dark){html[data-theme=auto]{--bg:#0d1117;--fg:#d5dde6;--muted:#8b949e;--hdr:#161b22;--line:#30363d;--user:#2a4d36;--userbg:#1e3a29;--userfg:#7ce09a;--asst:#151d2b;--asstfg:#8db3f5;--mark:#8a6d1a;--code:#1f2530;--pre-bg:#010409;--pre-fg:#e6edf3}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15.5px/1.6 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--hdr);border-bottom:1px solid var(--line);padding:10px 22px;z-index:9;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.meta{font-size:13px;color:var(--muted);display:grid;grid-template-columns:max-content 1fr max-content 1fr;gap:2px 14px}
.meta .v{color:var(--fg);font-weight:600;padding-left:2px}
.controls{display:flex;gap:12px;align-items:center;margin-top:8px}
input[type=search]{flex:1;max-width:520px;padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:14px}
label{font-size:13px;color:var(--muted);white-space:nowrap}
button{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);cursor:pointer;font-size:13px}
button:hover{border-color:var(--muted)}
#stats{font-size:12px;color:var(--muted);min-width:140px}
main{max-width:1160px;margin:0 auto;padding:10px 28px 80px}
section.block{padding:6px 14px 12px;margin:10px 0;border-radius:10px}
section.block.user{background:var(--userbg)}
section.block.asst{margin-left:46px;border-bottom:1px solid var(--line)}
h2{padding:3px 10px;border-radius:6px;font-size:13px;letter-spacing:.02em;margin:6px 0 8px;width:max-content}
h2.user{background:var(--user);color:var(--userfg)}
h2.asst{background:var(--asst);color:var(--asstfg)}
.ts{float:right;font-weight:normal;color:var(--muted);font-size:12px}
p{margin:.45em 0}
pre{background:var(--pre-bg);color:var(--pre-fg);padding:12px 14px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.92em}
pre code{background:none;padding:0}
ul,ol{margin:.45em 0;padding-left:1.5em}li{margin:.15em 0}
table{border-collapse:collapse;margin:10px 0}th,td{border:1px solid var(--line);padding:5px 10px}th{background:var(--hdr)}
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 1px}
.block.hidden{display:none}
"""

#: In-page search (case-insensitive ``<mark>`` hits, optional block filter
#: that works in both checked and unchecked modes: every run unmarks and
#: clears ``hidden`` first) + theme cycle auto->light->dark in localStorage.
#: Ported verbatim from the approved v5 spike. ASCII only.
_JS = r"""
const esc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
let timer;const q=document.getElementById('q'),f=document.getElementById('f'),
st=document.getElementById('stats'),blocks=[...document.querySelectorAll('.block')];
function unmark(b){b.querySelectorAll('mark').forEach(m=>m.replaceWith(...m.childNodes));b.normalize();}
function markBlock(b,re){let hits=0;const w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT),nodes=[];
while(w.nextNode())nodes.push(w.currentNode);for(const t of nodes){const v=t.nodeValue;if(!v)continue;
let m,last=0,frag=null;re.lastIndex=0;while((m=re.exec(v))){if(!frag)frag=document.createDocumentFragment();
frag.append(v.slice(last,m.index));const mk=document.createElement('mark');mk.textContent=m[0];
frag.append(mk);last=m.index+m[0].length;hits++;}if(frag){frag.append(v.slice(last));t.replaceWith(frag);}}
return hits;}
function run(){const word=q.value.trim(),filter=f.checked;let tot=0,shown=0;
for(const b of blocks){unmark(b);b.classList.remove('hidden');
if(word.length<2)continue;
const re=new RegExp(esc(word),'gi');const h=markBlock(b,re);tot+=h;
if(filter){if(h)shown++;else b.classList.add('hidden');}}
st.textContent=word.length<2?'':(tot?`${tot} marks / ${filter?shown:blocks.length} blocks`:'no results');}
q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(run,150);});
f.addEventListener('change',run);
const bt=document.getElementById('t'),root=document.documentElement;
const modes=['auto','light','dark'];let i=+(localStorage.getItem('th')||0);
root.dataset.theme=modes[i];bt.textContent='theme: '+modes[i];
bt.onclick=()=>{i=(i+1)%3;root.dataset.theme=modes[i];localStorage.setItem('th',i);bt.textContent='theme: '+modes[i];};
"""


def _ts_hms(ts: str | None) -> str:
    """Local HH:MM:SS for an ISO-8601 timestamp; empty when absent or bad."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


def _meta_val(meta: dict, key: str) -> str:
    """Escaped meta value for the header, ``n/a`` when missing."""
    val = meta.get(key)
    if val is None or val == "":
        return "n/a"
    return html.escape(str(val))


def render_html(blocks: list[Block], meta: dict) -> str:
    """One complete readable HTML page for the conversation (v5 design).

    ``meta`` keys (str unless noted): ``name, nnn, project, branch, head,
    model, ratio (float|None), stem, span``. Sticky labeled header, search
    + matching-blocks filter, theme toggle; user/assistant text blocks
    only, no block indices.
    """
    ratio = meta.get("ratio")
    ratio_s = "n/a" if ratio is None else f"{float(ratio):.1%}"
    sections = []
    for role, text, ts in blocks:
        cls = "user" if role == "user" else "asst"
        label = "User" if role == "user" else "Assistant"
        sections.append(
            f'<section class="block {cls}"><h2 class="{cls}">{label}'
            f'<span class="ts">{_ts_hms(ts)}</span></h2>'
            f'<div class="body">{md_to_html(text)}</div></section>')
    return (
        '<!doctype html><html data-theme="auto"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{_meta_val(meta, "stem")} - session transcript</title>'
        f'<style>{_CSS}</style></head><body>\n'
        '<header><div class="meta">\n'
        f'<span class="k">Session Name</span><span class="v">{_meta_val(meta, "name")} (cp-{_meta_val(meta, "nnn")})</span>\n'
        f'<span class="k">Model</span><span class="v">{_meta_val(meta, "model")}</span>\n'
        f'<span class="k">Project</span><span class="v">{_meta_val(meta, "project")}</span>\n'
        f'<span class="k">Ratio at rotation</span><span class="v">{ratio_s}</span>\n'
        f'<span class="k">Branch</span><span class="v">{_meta_val(meta, "branch")} @ {_meta_val(meta, "head")}</span>\n'
        f'<span class="k">Session Time</span><span class="v">{_meta_val(meta, "span")}</span>\n'
        '</div>\n'
        '<div class="controls"><input type="search" id="q" '
        'placeholder="search - marks all matches, optional block filter">\n'
        '<label><input type="checkbox" id="f"> matching blocks only</label>\n'
        '<button id="t">theme</button><span id="stats"></span></div></header>\n'
        f'<main>{"".join(sections)}</main>\n'
        f'<script>{_JS}</script></body>')
