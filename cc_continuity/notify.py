"""Best-effort ntfy push notifications (Phase 9 spec P9-D1/D4/D7).

Hard rule (P9-D7): notification delivery can NEVER break the continuity
loop. ``safe_send`` no-ops when the feature is disabled, builds the message
(spec §5 table), fires ``curl -s -m 5``, catches everything, and logs
failures to ``logs/errors.log``.

Hook/rotator call sites fire-and-forget (detached ``Popen`` — a dead
endpoint must not add latency to the 100 ms hook budget; Gate 8 measured
p95 52 ms). CLI/doctor call sites pass ``wait=True`` (``subprocess.run``,
timeout 6 s) so a failure is observable.

Message format (P9-D4): English, emoji + one-line title + compact body
(≤ 200 chars, mobile-readable). Fields: project slug, session NNN,
ratio/reason/summary when relevant, hostname. No secrets, no file
contents. The endpoint is user-supplied (P9-D1); any ntfy-compatible
server works.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from subprocess import DEVNULL, SubprocessError

from . import atomic, config, paths

CURL_TIMEOUT_S = 5.0  # P9-D7: curl -m 5

_DEDUP_FILE = "notify-dedup.json"
_DEDUP_MAX = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_failure(event: str, detail: str) -> None:
    """``notify: <event> failed: <detail>`` to errors.log. Never raises."""
    try:
        path = paths.logs_dir() / "errors.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] notify: {event} failed: {detail}\n")
    except OSError:
        pass


def enabled(cfg: dict) -> bool:
    """Feature on: master switch AND notify_enabled AND non-empty endpoint."""
    if not config.is_enabled(cfg):
        return False
    if not cfg.get("notify_enabled"):
        return False
    return bool(str(cfg.get("notify_endpoint") or "").strip())


def host() -> str:
    return socket.gethostname()


def project_slug(key: str) -> str:
    """Slug from the project record (lock-free read); falls back to key."""
    try:
        pref = json.loads(
            (paths.project_dir(key) / "project.json").read_text(encoding="utf-8"))
        return pref.get("slug") or key
    except (OSError, ValueError, AttributeError):
        return key


def _clip(s: object, n: int) -> str:
    """Collapse whitespace; truncate to n chars ('...' ellipsis when cut)."""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def naa_snippet(handoff_path: str, n: int = 60) -> str:
    """First n chars of the handoff's 'Next Atomic Action' body ('' when the
    file or section is absent)."""
    try:
        lines = Path(handoff_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    out, on = [], False
    for line in lines:
        if line.strip().lower().startswith("## next atomic action"):
            on = True
            continue
        if on:
            if line.startswith("## "):
                break
            if line.strip():
                out.append(line.strip())
    return _clip(" ".join(out), n)


def curl_cmd(endpoint: str, title: str, body: str, priority: str) -> list[str]:
    """P9-D7 wire form: curl -s -m 5, Title/Priority headers, plain body."""
    return ["curl", "-s", "-m", str(int(CURL_TIMEOUT_S)),
            "-X", "POST", endpoint,
            "-H", f"Title: {title}",
            "-H", f"Priority: {priority}",
            "--data-raw", body]


def send(endpoint: str, title: str, body: str, priority: str, *,
         event: str = "", wait: bool = False,
         runner=subprocess.run) -> bool:
    """Fire one push. NEVER raises.

    ``wait=True``: ``subprocess.run`` (timeout 6 s) — True iff exit 0.
    ``wait=False``: detached ``Popen`` (fire-and-forget) — True iff the
    process spawned. Any failure → one errors.log line.
    """
    name = event or title
    try:
        cmd = curl_cmd(endpoint, title, body, priority)
        if wait:
            proc = runner(cmd, capture_output=True,
                          timeout=CURL_TIMEOUT_S + 1)
            if proc.returncode != 0:
                _log_failure(name, f"curl exit {proc.returncode}")
                return False
            return True
        subprocess.Popen(cmd, stdin=DEVNULL,
                         stdout=DEVNULL,
                         stderr=DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, SubprocessError) as e:
        _log_failure(name, f"{type(e).__name__}: {e}")
        return False


def _dedup_path() -> Path:
    return paths.runtime_dir() / _DEDUP_FILE


def dedup_seen(key: str) -> bool:
    """True when ``key`` was already pushed (records it on first sight).

    Row 8 dedup (spec §5): one push per error event. The file is capped at
    ``_DEDUP_MAX`` oldest entries; a write failure just disables dedup for
    that call (double push acceptable — the push is best-effort).
    """
    path = _dedup_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() \
            else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if key in data:
        return True
    data[key] = time.time()
    while len(data) > _DEDUP_MAX:
        data.pop(min(data, key=data.get))
    try:
        atomic.atomic_write_json(path, data)
    except OSError:
        pass
    return False


def _fmt_ratio(r: object) -> str:
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        return f"{r * 100:.1f}%"
    return "—"


def build_message(event: str, *, slug: str, nnn: int,
                  **fields) -> tuple[str, str, str]:
    """(title, body, priority) for one lifecycle event (spec §5, verbatim
    templates). Pure — unit-tested against exact strings. ``ValueError`` on
    an unknown event or a body over 200 chars (a template bug must not
    break the loop; ``safe_send`` logs it)."""
    p = f"{nnn:03d}"
    if event == "session_started":
        t, pr = "▶ cc-continuity: session started", "default"
        b = f"proj={slug} {p} ({fields['name']}) host={fields['host']}"
    elif event == "warned":
        t, pr = "⚠ cc-continuity: context warning", "default"
        b = (f"proj={slug} {p} ratio={_fmt_ratio(fields['ratio'])} — "
             f"rotating at {fields['rotate_threshold'] * 100:.0f}%")
    elif event == "rotation_pending":
        t, pr = "⟳ cc-continuity: rotation pending", "high"
        b = (f"proj={slug} {p} ratio={_fmt_ratio(fields['ratio'])} — "
             "no interruption; rotates at turn end")
    elif event == "handoff_verified":
        t, pr = "✓ cc-continuity: handoff written & verified", "default"
        b = f"proj={slug} {p} cp-{p} sha256 OK"
    elif event == "rotation_complete":
        t, pr = "🔄 cc-continuity: rotation complete", "high"
        b = (f"proj={slug} {p}→{fields['nnn_new']:03d} "
             f"ratio {_fmt_ratio(fields['r0'])}→{_fmt_ratio(fields['r1'])} "
             f"resumed: {fields['naa'] or '—'}")
    elif event == "resumed":
        t, pr = "↻ cc-continuity: resumed", "default"
        b = f"proj={slug} {p} VALIDATION OK — continuing from NAA"
    elif event == "needs_human":
        t, pr = "🛑 cc-continuity: BLOCKED — waiting for you", "urgent"
        b = (f"proj={slug} {p} reason={_clip(fields['reason'], 80)} "
             f"host={fields['host']} — remote in and reply in session {p}")
    elif event == "error":
        t, pr = f"✗ cc-continuity: {fields['what']} failed", "urgent"
        b = (f"proj={slug} {p} detail={_clip(fields['detail'], 60)} — see "
             f"{paths.continuity_home() / 'errors.log'}")
    elif event == "task_done":
        t, pr = "✅ cc-continuity: task complete", "default"
        b = f"proj={slug} summary={_clip(fields['summary'], 80)}"
    elif event == "session_end":
        t, pr = "■ cc-continuity: session ended", "default"
        b = f"proj={slug} {p} reason={fields['reason']}"
    else:
        raise ValueError(f"unknown notify event {event!r}")
    if len(b) > 200:
        raise ValueError(f"notify body > 200 chars ({event}): {len(b)}")
    return t, b, pr


def on_state_enter(cfg: dict | None, key: str, nnn: int, new_state: str,
                   ratio: float | None = None) -> None:
    """spec §5 rows 2/3 — fired by ``thresholds.advance()`` when a walk
    ENTERS WARNED or ROTATION_PENDING (catches direct steps AND mid-walk
    entries, e.g. ACTIVE → ROTATION_PENDING → HANDOFF_REQUESTED). No-op for
    any other state or ``cfg=None``. Never raises."""
    if cfg is None or new_state not in ("WARNED", "ROTATION_PENDING"):
        return
    try:
        slug = project_slug(key)
        if new_state == "WARNED":
            safe_send(cfg, "warned", slug=slug, nnn=nnn, ratio=ratio,
                      rotate_threshold=float(cfg.get("rotate_ratio", 0.62)))
        else:
            safe_send(cfg, "rotation_pending", slug=slug, nnn=nnn,
                      ratio=ratio)
    except Exception:
        _log_failure(new_state, "on_state_enter: "
                   + (traceback.format_exc().splitlines()[-1] if __debug__ else ""))


def safe_send(cfg: dict, event: str, *, slug: str, nnn: int = 0,
              dedup_key: str | None = None, wait: bool = False,
              **fields) -> bool:
    """The single call-site wrapper (spec §4): reads config, no-ops when
    disabled, dedups error events, builds the message, fires curl
    (fire-and-forget unless ``wait``), logs failures. NEVER raises.
    Returns True when a push was actually fired."""
    try:
        if not enabled(cfg):
            return False
        if dedup_key is not None and dedup_seen(dedup_key):
            return False
        title, body, priority = build_message(event, slug=slug, nnn=nnn,
                                              **fields)
        return send(str(cfg.get("notify_endpoint") or "").strip(),
                    title, body, priority, event=event, wait=wait)
    except Exception:
        _log_failure(event, "safe_send: "
                   + (traceback.format_exc().splitlines()[-1] if __debug__ else ""))
        return False
