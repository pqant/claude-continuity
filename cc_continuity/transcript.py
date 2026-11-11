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
import time
from datetime import datetime
from pathlib import Path

from cc_continuity import atomic, paths

#: One conversation turn: (role, text, ISO-8601 timestamp or None).
Block = tuple[str, str, str | None]

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_MULTI_NL = re.compile(r"\n{3,}")
_LIST_RE = re.compile(r"^\s*([-*]|\d+\.)\s+")
_SEP_ROW_RE = re.compile(r"^[\s:|-]+$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

#: Protocol plumbing dropped outright (issue #54): slash-command XML,
#: task notifications, skill base-directory lines (plus the pre-existing
#: local-command/caveat noise). "Stop hook feedback:" is checked
#: separately - only CONTINUITY-bearing injections are dropped.
_NOISE_PREFIXES = ("<local-command", "Caveat:", "<command-name>",
                   "<command-message>", "<command-args>",
                   "<task-notification>", "Base directory for this skill:")
#: Image metadata lines from pasted attachments (issue #54); they fold
#: into a single chip so the image trace survives without the bulk.
_IMAGE_PREFIXES = ("[Image: source:", "[Image: original")
_IMAGE_CHIP = "\U0001f5bc image"


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
    system/summary records, ``<system-reminder>`` spans, and protocol
    plumbing (slash-command XML, task notifications, Continuity Stop-hook
    feedback, skill base-directory lines, ``No response requested.``)
    never reach the output (issue #54). The CONTINUITY RESUME blob and
    CC's "Continue from where you left off." prompt collapse to one-line
    anchors; image metadata lines fold into a ``🖼 image`` chip (runs of
    chips collapse to one). Runs of 3+ newlines in assistant text
    collapse to exactly two.
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
        if not text or text.startswith(_NOISE_PREFIXES):
            continue
        # Real records are "Stop hook feedback:\nCONTINUITY ..."; only
        # these injections drop, other hook feedback survives (issue #54).
        if text[:120].replace("\n", " ").startswith(
                "Stop hook feedback: CONTINUITY"):
            continue
        if (rec["type"] == "assistant"
                and text.startswith("No response requested.")):
            continue
        # Image metadata lines become a chip; human text in a mixed
        # record survives without them (issue #54).
        kept, had_image = [], False
        for ln in text.split("\n"):
            if ln.startswith(_IMAGE_PREFIXES):
                had_image = True
            else:
                kept.append(ln)
        text = "\n".join(kept).strip()
        if not text:
            if not had_image:
                continue
            text = _IMAGE_CHIP
            if out and out[-1][1] == _IMAGE_CHIP:
                continue
        if text.startswith("CONTINUITY RESUME cp-"):
            m = re.search(r"cp-(\d{3})", text)
            tag = f"cp-{m.group(1)} " if m else ""
            text = f"↻ CONTINUITY RESUME {tag}· handoff.md"
        elif text.startswith("Continue from where you left off."):
            text = "↻ CC resume"
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
.hd{font-size:13px;color:var(--muted);display:grid;grid-template-columns:max-content 1fr max-content 1fr;gap:6px 14px;align-items:center}
.hd .k{font-weight:600;color:var(--fg)}
.hd .v{color:var(--fg);font-weight:600;padding-left:2px}
.row2{display:flex;gap:12px;align-items:center;margin-top:6px;grid-column:1/3;flex-wrap:wrap}
.qbox{width:360px;max-width:100%;padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:14px}
label{font-size:13px;color:var(--fg);font-weight:600;white-space:nowrap}
button{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);cursor:pointer;font-size:13px}
button:hover{border-color:var(--muted)}
select{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:13px;max-width:340px}
select:disabled{opacity:.55}
#stats{font-size:12px;color:var(--muted);min-width:140px}
main{max-width:1160px;margin:0 auto;padding:10px 28px 80px}
section.block{padding:6px 14px 12px;margin:10px 0;border-radius:10px}
section.block.user{background:var(--userbg)}
section.block.asst{margin-left:46px;border-bottom:1px solid var(--line)}
h2{padding:3px 10px;border-radius:6px;font-size:13px;letter-spacing:.02em;margin:6px 0 8px;width:max-content;display:inline-flex;align-items:baseline;gap:8px}
h2.user{background:var(--user);color:var(--userfg)}
h2.asst{background:var(--asst);color:var(--asstfg)}
.ts{font-weight:normal;color:var(--muted);font-size:12px}
.ts:empty{display:none}
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


#: Manual month names for switcher labels: ``strftime("%b")`` follows the
#: machine locale and could emit a non-English month (hygiene requires
#: ASCII/English template strings).
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STEM_DT_RE = re.compile(r"_(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})$")

#: Switcher client script: populate the ``#s`` select from the manifest
#: global (newest first, first ten plus the current page when it falls
#: outside), switch by navigating the SAME viewer template over file://
#: (XHR is blocked there; the loaded page re-runs search/theme itself).
_SWITCH_JS = r"""
(function(){var sel=document.getElementById('s');if(!sel)return;
var all=(window.CC_SESSIONS||[]),cur=sel.getAttribute('data-cur')||'',up=sel.getAttribute('data-up')||'';
function find(a,h){for(var i=0;i<a.length;i++){if(a[i].href===h)return a[i];}return null;}
var top=all.slice(0,10);
if(cur&&!find(top,cur)){top.push(find(all,cur)||{href:cur,label:'(current session)'});}
for(var k=0;k<top.length;k++){var o=document.createElement('option');
o.value=top[k].href;o.textContent=top[k].label;sel.appendChild(o);}
if(cur)sel.value=cur;
sel.disabled=all.length<2;
var lbl=document.getElementById('slbl');
if(lbl)lbl.textContent=all.length>1?'Sessions':'Session';
sel.onchange=function(){if(this.value)location.href=up+this.value;};})();
"""


def _session_entry(html_path: Path, project_dir: Path) -> dict:
    """One manifest entry; ``_k`` is the private sort key (datetime)."""
    stem = html_path.stem
    m = _STEM_DT_RE.search(stem)
    dt = None
    if m:
        try:
            dt = datetime(*[int(g) for g in m.groups()])
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.fromtimestamp(html_path.stat().st_mtime)
        label = stem
    else:
        label = f"{stem[:m.start()]} - {dt:%d} {_MONTHS[dt.month - 1]} {dt:%H:%M}"
    return {"href": html_path.relative_to(project_dir).as_posix(),
            "label": label, "_k": dt}


def collect_sessions(project_dir: Path) -> list[dict]:
    """Every exported session HTML of one project, newest first.

    Scans ``<project>/sessions/*/checkpoint/*.html``. Labels come from the
    stem's ``_YYYY-MM-DD-HHMM`` tail (``name - DD Mon HH:MM``); a stem
    without a parseable tail falls back to the raw stem with file mtime.
    Unreadable files are skipped.
    """
    entries = []
    for p in project_dir.glob("sessions/*/checkpoint/*.html"):
        try:
            entries.append(_session_entry(p, project_dir))
        except OSError:
            continue
    entries.sort(key=lambda e: e["_k"], reverse=True)
    return [{"href": e["href"], "label": e["label"]} for e in entries]


def render_manifest(entries: list[dict]) -> str:
    """``sessions.js`` body: one global assignment, ASCII JSON only."""
    return ("window.CC_SESSIONS="
            + json.dumps(entries, ensure_ascii=True, separators=(",", ":")) + ";\n")


def _checkpoint_project(dest_dir: Path) -> tuple[Path, str] | None:
    """``(project_dir, '../' prefix)`` when dest_dir is a real checkpoint
    dir (``<project>/sessions/<NNN>/checkpoint``); ``None`` otherwise.
    The page sits three levels under the project, hence ``../../../``."""
    try:
        if dest_dir.name != "checkpoint":
            return None
        if dest_dir.parents[1].name != "sessions" or not dest_dir.parents[0].name.isdigit():
            return None
        return dest_dir.parents[2], "../../../"
    except IndexError:
        return None


def update_sessions_manifest(dest_dir: Path) -> None:
    """Best-effort refresh of ``<project>/sessions.js`` after one export.

    Never raises (P9-D7): a manifest problem is one debug line; the export
    pair stands on its own.
    """
    try:
        ctx = _checkpoint_project(dest_dir)
        if ctx is None:
            return
        project, _up = ctx
        body = render_manifest(collect_sessions(project))
        atomic.write_bytes_fsync(project / "sessions.js", body.encode("utf-8"))
    except Exception:
        debug_log(f"manifest skipped: sessions.js for {dest_dir}")


def _ts_hms(ts: str | None) -> str:
    """Local HH:MM:SS for an ISO-8601 timestamp; empty when absent or bad."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


#: Max bytes read from the end of a transcript by the clock helpers. The
#: prompt path must stay fast even when a session grew a huge transcript
#: (base64 tool payloads), so only the tail is ever read (issue #44).
_TAIL_BYTES = 1_000_000


def _tail_lines(path, tail: int = 400) -> list[str] | None:
    """Last ``tail`` lines of a JSONL file, reading at most
    ``_TAIL_BYTES`` bytes from the end. ``None`` when unreadable (missing
    file, directory, bad path type). A partial first line from a mid-line
    read boundary simply fails the caller's JSON parse and is skipped."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "replace")
    except (OSError, TypeError, AttributeError):
        return None
    return chunk.splitlines()[-tail:]


def _last_turn_ts(path, *, tail: int = 400):
    """``(user_ts, first_reply_ts, last_reply_ts)`` of the LAST genuine
    user turn, or ``None`` when no readable pair exists. Records whose
    content is only tool_result blocks ride along with the turn and never
    reset the prompt time."""
    lines = _tail_lines(path, tail=tail)
    if lines is None:
        return None
    user_ts = start_ts = last_ts = None
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("type")
        ts = rec.get("timestamp")
        if t == "user":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list) and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content):
                continue  # a tool result is not a new prompt
            user_ts, start_ts, last_ts = ts, None, None
        elif t == "assistant" and user_ts is not None:
            if start_ts is None:
                start_ts = ts
            last_ts = ts
    if not (user_ts and start_ts and last_ts):
        return None
    return user_ts, start_ts, last_ts


def prev_reply_clock(path, *, tail: int = 400) -> str | None:
    """``prev reply HH:MM:SS -> HH:MM:SS (2m34s)`` for the LAST genuine
    user turn (issue #44), machine-local, from CC's own transcript JSONL
    tail so the timing matches the transcript HTML export. The duration
    spans prompt send to final reply, so it reads as the task's wall
    time: ``47s`` under a minute, ``2m34s`` beyond. Missing file, bad
    path type or no readable user/assistant pair -> None (never raises);
    the UserPromptSubmit hook then shows the prompt clock alone."""
    ts = _last_turn_ts(path, tail=tail)
    if ts is None:
        return None
    user_ts, start_ts, last_ts = ts
    s, e = _ts_hms(start_ts), _ts_hms(last_ts)
    if not (s and e):
        return None
    try:
        dur = (datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
               - datetime.fromisoformat(str(user_ts).replace("Z", "+00:00")))
    except ValueError:
        return None
    secs = max(int(dur.total_seconds()), 0)
    return f"prev reply {s} -> {e} ({_fmt_secs(secs)})"


def _fmt_secs(secs: int, sep: str = "") -> str:
    """``47s`` under a minute, ``2m34s`` beyond, ``1h2m3s`` past an hour
    (zero-padded seconds). ``sep`` sits between units (issue #51: the
    HUD passes ``" "``; the prompt clock keeps the compact ``""``)."""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{sep}{secs % 60:02d}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h{sep}{rem // 60}m{sep}{rem % 60:02d}s"


def prev_task_duration(path, *, tail: int = 400) -> str | None:
    """Wall time of the LAST genuine user turn as ``39m 15s`` (issues #45, #51):
    prompt send to final reply, the same span ``prev_reply_clock``
    reports. For the status line HUD, which shows it the moment the turn
    ends. Missing file, bad path type or no readable user/assistant pair
    -> None (never raises); the HUD then simply omits the segment."""
    ts = _last_turn_ts(path, tail=tail)
    if ts is None:
        return None
    user_ts, _start_ts, last_ts = ts
    try:
        dur = (datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
               - datetime.fromisoformat(str(user_ts).replace("Z", "+00:00")))
    except ValueError:
        return None
    return _fmt_secs(max(int(dur.total_seconds()), 0), sep=" ")


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
    ext = switch_js = ""
    # Unwired pages keep empty grid cells so auto-flow columns stay aligned.
    switch = "<span></span><span></span>"
    if meta.get("manifest_src"):
        src = html.escape(str(meta["manifest_src"]), quote=True)
        cur = html.escape(str(meta.get("self_href") or ""), quote=True)
        up = src[:src.rfind("/") + 1]
        ext = f'<script src="{src}"></script>\n'
        switch = (f'<label for="s" id="slbl">Sessions</label>'
                  f'<select id="s" data-cur="{cur}" data-up="{up}"></select>')
        switch_js = _SWITCH_JS
    sections = []
    for role, text, ts in blocks:
        cls = "user" if role == "user" else "asst"
        label = "User" if role == "user" else "Assistant"
        sections.append(
            f'<section class="block {cls}"><h2 class="{cls}">{label} '
            f'<span class="ts">{_ts_hms(ts)}</span></h2>'
            f'<div class="body">{md_to_html(text)}</div></section>')
    return (
        '<!doctype html><html data-theme="auto"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{_meta_val(meta, "stem")} - session transcript</title>'
        f'<style>{_CSS}</style></head><body>\n'
        '<header><div class="hd">\n'
        f'<span class="k">Session Name</span><span class="v">{_meta_val(meta, "name")} (cp-{_meta_val(meta, "nnn")})</span>\n'
        f'<span class="k">Model</span><span class="v">{_meta_val(meta, "model")}</span>\n'
        f'<span class="k">Project</span><span class="v">{_meta_val(meta, "project")}</span>\n'
        f'<span class="k">Ratio at rotation</span><span class="v">{ratio_s}</span>\n'
        f'<span class="k">Branch</span><span class="v">{_meta_val(meta, "branch")} @ {_meta_val(meta, "head")}</span>\n'
        f'<span class="k">Session Time</span><span class="v">{_meta_val(meta, "span")}</span>\n'
        '<label for="q">Search</label>\n'
        '<input type="search" id="q" class="qbox" '
        'placeholder="marks all matches, optional block filter">\n'
        f'{switch}\n'
        '<div class="row2"><label><input type="checkbox" id="f"> '
        'matching blocks only</label>\n'
        '<button id="t">theme</button><span id="stats"></span></div></div></header>\n'
        f'<main>{"".join(sections)}</main>\n'
        f'{ext}<script>{_JS}{switch_js}</script></body>')


def resolve_transcript_path(metric: dict | None, root: Path, sid: str | None,
                            *, home: Path | None = None) -> Path | None:
    """Live transcript JSONL for a session: metric hit, derived fallback.

    The statusline metric's ``transcript_path`` wins when it is a non-empty
    str pointing at an existing file; otherwise derive
    ``<home>/.claude/projects/<cwd-slug>/<sid>.jsonl``. ``None`` when
    neither exists (B2: omit the line, never raise).
    """
    if isinstance(metric, dict):
        p = metric.get("transcript_path")
        if isinstance(p, str) and p and Path(p).is_file():
            return Path(p)
    if not sid:
        return None
    base = home if home is not None else Path.home()
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(root))
    derived = base / ".claude" / "projects" / slug / f"{sid}.jsonl"
    return derived if derived.is_file() else None


def debug_log(detail: str) -> None:
    """One ``errors.log`` line tagged ``transcript:``; never raises.

    Best-effort by contract: finalize's outer guard (Task 6) and every
    skip path inside this module log through here, so an export problem
    is a log line, never an exception (P9-D7).
    """
    try:
        with open(paths.logs_dir() / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] transcript: {detail}\n")
    except OSError:
        pass


def _parse_ts(ts: str | None) -> datetime | None:
    """Aware datetime for an ISO-8601 string, ``None`` when absent/bad."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def render_markdown(blocks: list[Block], meta: dict) -> str:
    """Canonical Markdown export: ``# <stem>`` title, then one section
    per block (``## User``/``## Assistant`` + local time + raw text)."""
    out = [f"# {meta.get('stem') or 'session'}", ""]
    for role, text, ts in blocks:
        label = "User" if role == "user" else "Assistant"
        clock = _ts_hms(ts)
        out.append(f"## {label}" + (f" {clock}" if clock else ""))
        out.append("")
        out.append(text)
        out.append("")
    return "\n".join(out)


def export_session(jsonl_path: Path, dest_dir: Path, meta: dict,
                   *, cap_bytes: int = 5 * 1024 * 1024,
                   final_dir: Path | None = None) -> str | None:
    """Best-effort readable export of one session JSONL (B3/B4).

    Reads at most ``cap_bytes`` (past the cap the trailing partial line is
    dropped and a final ``[truncated]`` block is appended), then writes
    ``<stem>.md`` and ``<stem>.html`` into ``dest_dir`` with fsync'd plain
    writes and returns the stem. ``span``/``stem`` are derived from the
    first/last block timestamps (local; ``stem`` falls back to now) and
    added to a meta copy. ``final_dir`` (issue #16): where the pair will
    live when ``dest_dir`` is only a staging dir (finalize's
    ``checkpoint.tmp-*``); the switcher wiring uses it and the in-place
    manifest refresh is skipped (the mover refreshes at the final dir).
    Never raises: unreadable source, empty parse, or
    any write failure logs one debug line, leaves no half-written pair,
    and returns ``None``.
    """
    try:
        try:
            oversize = jsonl_path.stat().st_size > cap_bytes
            with open(jsonl_path, "rb") as f:
                raw = f.read(cap_bytes)
        except OSError:
            debug_log(f"export skipped: unreadable source {jsonl_path}")
            return None
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if oversize and lines:
            lines.pop()  # trailing partial line cut by the cap
        blocks = parse_conversation(lines)
        if not blocks:
            debug_log(f"export skipped: empty conversation {jsonl_path}")
            return None
        if oversize:
            blocks = blocks + [("assistant", "[truncated]", None)]
        first = last = None
        for _, _, ts in blocks:
            stamp = _parse_ts(ts)
            if stamp:
                first = first or stamp
                last = stamp
        name = meta.get("name") or "session"
        if first is not None:
            span = (f"{first.astimezone():%Y-%m-%d %H:%M} - "
                    f"{last.astimezone():%Y-%m-%d %H:%M}")
            stem = f"{name}_{first.astimezone():%Y-%m-%d-%H%M}"
        else:
            span = "n/a"
            stem = f"{name}_{datetime.now().astimezone():%Y-%m-%d-%H%M}"
        export_meta = dict(meta)
        export_meta["stem"] = stem
        export_meta["span"] = span
        eff = final_dir or dest_dir
        ctx = _checkpoint_project(eff)
        if ctx is not None:
            project, up = ctx
            export_meta["manifest_src"] = up + "sessions.js"
            export_meta["manifest_up"] = up
            export_meta["self_href"] = (eff.relative_to(project).as_posix()
                                        + "/" + stem + ".html")
        dest_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        try:
            md_path = dest_dir / f"{stem}.md"
            atomic.write_bytes_fsync(md_path,
                                     render_markdown(blocks, export_meta).encode("utf-8"))
            written.append(md_path)
            html_path = dest_dir / f"{stem}.html"
            atomic.write_bytes_fsync(html_path,
                                     render_html(blocks, export_meta).encode("utf-8"))
            written.append(html_path)
        except Exception:
            for p in written:
                try:
                    p.unlink()
                except OSError:
                    pass
            debug_log(f"export skipped: write failed for {stem}")
            return None
        # Issue #16: refresh the per-project manifest the switcher reads.
        # When exporting into a staging dir the mover refreshes at the
        # final dir instead. Guarded belt-and-braces; never raises.
        if final_dir is None:
            try:
                update_sessions_manifest(dest_dir)
            except Exception:
                debug_log(f"manifest skipped: refresh failed for {stem}")
        return stem
    except Exception:
        debug_log(f"export skipped: unexpected error for {jsonl_path}")
        return None
