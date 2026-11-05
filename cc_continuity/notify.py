"""Best-effort ntfy push notifications (Phase 9 spec P9-D1/D4/D7).

Hard rule (P9-D7): notification delivery can NEVER break the continuity
loop. ``safe_send`` no-ops when the feature is disabled, builds the message
(spec §5 table), fires ``curl -s -m 5``, catches everything, and logs
failures to ``logs/errors.log``.

Hook/rotator call sites fire-and-forget (detached ``Popen`` — a dead
endpoint must not add latency to the 100 ms hook budget; Gate 8 measured
p95 52 ms). CLI/doctor call sites pass ``wait=True`` (``subprocess.run``,
timeout 6 s) so a failure is observable.

Message format (P9-D4 + issue #7): English, emoji title + multi-line body
(≤ 1000 chars) — a common envelope (proj/host/branch/model/ratio) then
event-specific lines. Content fields (transcript excerpt, notification
text, reasons, summaries) go through ``scrub`` before the clip; the whole
path stays behind the ``notify_content`` config flag. The endpoint is
user-supplied (P9-D1); any ntfy-compatible server works.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import uuid
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


# ---------------------------------------------------------------- envelope
# Spec G1/B2/B3 (issue #7): who/where/what on the first three lines, most
# critical first (phones show ~5). Every part past the proj line is
# best-effort — a missing field is omitted, never an error.


def envelope(*, slug: str, nnn: int, name: str, host: str,
             branch: str | None = None, sha: str | None = None,
             dirty: bool = False, model: str | None = None,
             ratio: float | None = None) -> list[str]:
    """The three envelope lines (pure function of its inputs — B1). An
    unavailable name omits the parenthetical rather than printing ``()``
    (B3: a push with only ``proj=`` is still a good push)."""
    lines = [f"proj={slug} {nnn:03d}" + (f" ({name})" if name else "")]
    host_line = f"host={host}"
    if branch and sha:
        host_line += f" branch={branch} @{sha}" + ("*" if dirty else "")
    lines.append(host_line)
    parts = []
    if model:
        parts.append(f"model={model}")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        parts.append(f"ratio={ratio * 100:.1f}%")
    if parts:
        lines.append(" ".join(parts))
    return lines


def git_state(repo: str | None, *,
              runner=subprocess.run) -> tuple[str, str, bool] | None:
    """(branch, short-sha, dirty) from three ``git -C`` probes (2 s each);
    None on ANY failure — the envelope omits the git parts (B3)."""
    if not repo:
        return None

    def _git(*args: str) -> str:
        res = runner(["git", "-C", str(repo), *args],
                     capture_output=True, text=True, timeout=2)
        if getattr(res, "returncode", 1) != 0:
            raise RuntimeError(f"git {args[0]} rc={res.returncode}")
        return str(res.stdout or "").strip()

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        sha = _git("rev-parse", "--short", "HEAD")
        porcelain = _git("status", "--porcelain")
        if not branch or not sha:
            return None  # empty repo / detached weirdness
        return (branch, sha, bool(porcelain))
    except Exception:
        return None


def metric_env_lines(sid: str | None) -> tuple[str | None, float | None]:
    """(model, ratio) from the sid's statusline metric (B2); (None, None)
    when absent or stale (10-min rule). statusline imported lazily so the
    hook-time import graph stays cycle-free."""
    if not sid:
        return (None, None)
    from . import statusline
    metric = statusline.read_metric(sid)
    if statusline.is_stale(metric) or not isinstance(metric, dict):
        return (None, None)
    model = metric.get("model")
    ratio = metric.get("ratio")
    return (
        model if isinstance(model, str) and model else None,
        ratio if isinstance(ratio, (int, float))
        and not isinstance(ratio, bool) else None,
    )


def build_env(slug, nnn, *, name: str = "", sid: str | None = None,
              repo: str | None = None, host: str | None = None,
              runner=subprocess.run) -> list[str]:
    """Run the collectors and compose the envelope lines; every failure is
    tolerated (runs in the detached notify-emit child, never the hook
    budget — B1)."""
    git = git_state(repo, runner=runner)
    model, ratio = metric_env_lines(sid)
    return envelope(slug=slug, nnn=nnn, name=name,
                    host=host if host is not None else socket.gethostname(),
                    branch=git[0] if git else None,
                    sha=git[1] if git else None,
                    dirty=bool(git and git[2]),
                    model=model, ratio=ratio)


# ---------------------------------------------------------------- scrubber
# Spec B4 (issue #7): mask credentials in every user-content body BEFORE the
# clip, so replacements count toward the cap. Envelope fields are never
# scrubbed. PRIVATE KEY blocks are truncated first (that line and everything
# after it is dropped), then the patterns below run in order.

_PRIVATE_KEY_LINE = re.compile(r"(?im)^.*-----BEGIN [^-]*PRIVATE KEY-----.*$")

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)"
                r"\s*[:=]\s*\S+"), "key=[redacted]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[redacted]"),
    (re.compile(r"(?i)bearer\s+\S{10,}"), "bearer [redacted]"),
)


def scrub(text: str) -> str:
    """Mask known secret shapes in a free-text push body (spec B4).

    Case-insensitive; a ``-----BEGIN ... PRIVATE KEY-----`` line truncates
    the body at that line. Clean text is returned unchanged.
    """
    if not text:
        return text
    m = _PRIVATE_KEY_LINE.search(text)
    if m:
        text = text[: m.start()].rstrip("\n")
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


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


def last_assistant_text(path: str | None, *,
                        limit: int = 300) -> str | None:
    """Scrubbed, clipped excerpt of the LAST assistant message in a JSONL
    transcript (spec B5): string content or list-of-blocks; bad lines are
    skipped (torn tail), None when nothing usable comes back."""
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    best: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [b.get("text") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str)]
            if parts:
                text = "\n".join(parts)
        if text and text.strip():
            best = text
    if best is None:
        return None
    return _clip(scrub(best), limit)


def objective_from(handoff_path: str | None, *,
                   limit: int = 200) -> str | None:
    """First content line under the handoff's ``## Objective`` heading
    (spec G2); None when the heading or a content line is absent."""
    if not handoff_path:
        return None
    try:
        lines = Path(handoff_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    on = False
    for line in lines:
        if line.strip().lower().startswith("## objective"):
            on = True
            continue
        if on:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return _clip(stripped, limit)
    return None


def curl_cmd(endpoint: str, title: str, body: str, priority: str,
             tags: str = "") -> list[str]:
    """P9-D7 wire form: curl -s -m 5, Title/Priority headers (+ Tags when
    set, spec G4), plain body."""
    cmd = ["curl", "-s", "-m", str(int(CURL_TIMEOUT_S)),
           "-X", "POST", endpoint,
           "-H", f"Title: {title}",
           "-H", f"Priority: {priority}"]
    if tags:
        cmd += ["-H", f"Tags: {tags}"]
    cmd += ["--data-raw", body]
    return cmd


def send(endpoint: str, title: str, body: str, priority: str, *,
         event: str = "", wait: bool = False, tags: str = "",
         runner=subprocess.run) -> bool:
    """Fire one push. NEVER raises.

    ``wait=True``: ``subprocess.run`` (timeout 6 s) — True iff exit 0.
    ``wait=False``: detached ``Popen`` (fire-and-forget) — True iff the
    process spawned. Any failure → one errors.log line.
    """
    name = event or title
    try:
        cmd = curl_cmd(endpoint, title, body, priority, tags)
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


def _fmt_minutes(s: float) -> str:
    """Seconds → ``"42m"`` / ``"1h 05m"`` (the ``dgx_up`` downtime)."""
    m = int(s) // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60:02d}m"


BODY_CAP = 1000  # spec G3 (issue #7): ntfy allows 512 KB — the cap stays
                 # only as the template-bug guard (was 200, self-imposed).

#: ntfy ``Tags`` header per event (spec G4, SH-exemplar style).
TAGS: dict[str, str] = {
    "session_started": "rocket",
    "warned": "warning",
    "rotation_pending": "hourglass_flowing_sand",
    "handoff_verified": "white_check_mark",
    "rotation_complete": "arrows_counterclockwise",
    "resumed": "twisted_rightwards_arrows",
    "needs_human": "rotating_light",
    "error": "x",
    "task_done": "white_check_mark",
    "session_end": "black_square_button",
    "stop_done": "white_check_mark",
    "notification": "bell",
    "dgx_down": "electric_plug",
    "dgx_up": "electric_plug",
    "dgx_power_cycle": "electric_plug",
    "turn_dead": "warning",
    "turn_recovered": "warning",
    "freeze_detected": "snowflake",
    "exit_hang": "boom",
    "frozen_nudge": "snowflake",
    "zombie_cleaned": "zombie",
}


def build_message(event: str, *, slug: str, nnn: int,
                  env: list[str] | None = None,
                  **fields) -> tuple[str, str, str]:
    """(title, body, priority) for one lifecycle event (spec §5 + issue #7
    G1–G4). Pure — unit-tested against exact strings. ``env`` carries the
    envelope lines (G1); it is prepended and de-duplicated (an identical
    line never shows up twice). User-content fields are scrubbed before the
    clip (B4). ``ValueError`` on an unknown event or a body over
    ``BODY_CAP`` chars (a template bug must not break the loop;
    ``safe_send`` logs it)."""
    p = f"{nnn:03d}"
    if event == "session_started":
        t, pr = "▶ cc-continuity: session started", "default"
        lines = [f"proj={slug} {p} ({fields['name']})"]
        if fields.get("prev"):
            lines.append(f"prev={fields['prev']}")
        if fields.get("objective"):
            lines.append(f"objective: {scrub(fields['objective'])}")
        if fields.get("next"):
            lines.append(f"next: {scrub(fields['next'])}")
    elif event == "warned":
        t, pr = "⚠ cc-continuity: context warning", "default"
        lines = [(f"proj={slug} {p} ratio={_fmt_ratio(fields['ratio'])} — "
                  f"rotating at {fields['rotate_threshold'] * 100:.0f}%")]
    elif event == "rotation_pending":
        t, pr = "◆ cc-continuity: rotation pending", "high"
        lines = [(f"proj={slug} {p} ratio={_fmt_ratio(fields['ratio'])} — "
                  "no interruption; rotates at turn end")]
    elif event == "handoff_verified":
        t, pr = "✓ cc-continuity: handoff written & verified", "default"
        lines = [f"proj={slug} {p} cp-{p} sha256 OK"]
    elif event == "rotation_complete":
        t, pr = "🔄 cc-continuity: rotation complete", "high"
        lines = [(f"proj={slug} {p}→{fields['nnn_new']:03d} "
                  f"ratio {_fmt_ratio(fields['r0'])}→"
                  f"{_fmt_ratio(fields['r1'])}")]
        if fields.get("objective"):
            lines.append(f"objective: {scrub(fields['objective'])}")
        lines.append(f"resumed: "
                     f"{scrub(fields['naa']) if fields.get('naa') else '—'}")
    elif event == "resumed":
        t, pr = "↻ cc-continuity: resumed", "default"
        lines = [f"proj={slug} {p} VALIDATION OK — continuing from NAA"]
        if fields.get("next"):
            lines.append(f"next: {scrub(fields['next'])}")
    elif event == "needs_human":
        t, pr = "🛑 cc-continuity: BLOCKED — waiting for you", "urgent"
        lines = [(f"proj={slug} {p} "
                  f"reason={_clip(scrub(str(fields['reason'])), 300)} "
                  f"host={fields['host']} — remote in and reply in "
                  f"session {p}")]
    elif event == "error":
        t, pr = f"✗ cc-continuity: {fields['what']} failed", "urgent"
        prefix = (f"proj={slug} {p} "
                  f"detail={_clip(scrub(str(fields['detail'])), 60)} — see ")
        see = str(paths.continuity_home() / "errors.log")
        if len(prefix) + len(see) > BODY_CAP:
            # a long store path (e.g. macOS TMPDIR in tests) would break the
            # body cap: ellipsize the HEAD, keep the tail visible
            see = "…" + see[len(see) - (BODY_CAP - len(prefix) - 1):]
        lines = [prefix + see]
    elif event == "task_done":
        t, pr = "✅ cc-continuity: task complete", "default"
        lines = [f"proj={slug} "
                 f"summary={_clip(scrub(str(fields['summary'])), 200)}"]
    elif event == "stop_done":
        t, pr = "✅ cc-continuity: Claude is done", "default"
        lines = ([f"said: {scrub(str(fields['said']))}"]
                 if fields.get("said") else [f"proj={slug} {p}"])
    elif event == "notification":
        t, pr = "🔔 cc-continuity: Claude needs you", "high"
        lines = ([f"msg: {_clip(scrub(str(fields['msg'])), 700)}"]
                 if fields.get("msg") else [f"proj={slug} {p}"])
    elif event == "session_end":
        t, pr = "■ cc-continuity: session ended", "default"
        lines = [f"proj={slug} {p} reason={fields['reason']}"]
    elif event == "dgx_down":
        t, pr = "🔌 cc-continuity: model server DOWN", "urgent"
        lines = [(f"host={fields['host']} — probing every "
                  f"{fields['interval_min']} min; nudges deferred")]
    elif event == "dgx_up":
        t, pr = "🔌 cc-continuity: model server back", "high"
        lines = [f"down for {_fmt_minutes(fields['downtime_s'])} — "
                 "nudges resume"]
    elif event == "dgx_power_cycle":
        t, pr = "🔌 cc-continuity: DGX power-cycling (auto)", "urgent"
        lines = [(f"down for {_fmt_minutes(fields['downtime_s'])} — relay "
                  "cycled; expect ~15-20 min until models are loaded")]
    elif event == "turn_dead":
        t, pr = "⟲ cc-continuity: turn died — recovering", "high"
        lines = [(f"proj={slug} {p} idle {fields['idle_minutes']}m — "
                  f"nudge 1/{fields['max_nudges']}")]
    elif event == "turn_recovered":
        t, pr = "✅ cc-continuity: turn recovered", "default"
        lines = [(f"proj={slug} {p} nudge {fields['nudge']} "
                  "verified (user_prompt)")]
    elif event == "freeze_detected":
        t, pr = "🧊 cc-continuity: retry-loop freeze?", "high"
        lines = [f"proj={slug} {p} pane stuck, input closed — no auto-fix"]
    elif event == "exit_hang":
        t, pr = "💥 cc-continuity: /exit hang — killed", "high"
        lines = [(f"proj={slug} {p} /exit hung {int(fields['age_s'])}s — "
                  f"killed ({fields['mode']})")]
    elif event == "frozen_nudge":
        if fields.get("nudge") is None:
            # picker variant (Phase 22-C): frozen pane WITHOUT a turn in
            # flight — the daemon sent a single ESC, no nudge
            t, pr = "🧊 cc-continuity: frozen pane — ESC sent", "urgent"
            lines = [(f"proj={slug} {p} frozen {fields['frozen_minutes']}m — "
                      "no turn active")]
        else:
            t, pr = "🧊 cc-continuity: frozen turn — escalated", "urgent"
            lines = [(f"proj={slug} {p} frozen {fields['frozen_minutes']}m — "
                      f"ESC + nudge {fields['nudge']}/{fields['max_nudges']}")]
    elif event == "zombie_cleaned":
        # Phase 23 (fix a): an off session's tmux session was still alive
        # on the orphaned progress-pane (no CC) — the daemon killed it
        t, pr = "🧟 cc-continuity: zombie session cleaned", "default"
        lines = [(f"proj={slug} {p} {_clip(fields['session'], 40)} — "
                  "no CC, killed")]
    else:
        raise ValueError(f"unknown notify event {event!r}")
    body = "\n".join(dict.fromkeys([*(env or []), *lines]))
    if len(body) > BODY_CAP:
        raise ValueError(f"notify body > {BODY_CAP} chars ({event}): "
                         f"{len(body)}")
    return t, body, pr


# ---------------------------------------------------------------- emit pipeline
# Spec B1/B5/B8 (issue #7): hook/rotator/CLI PARENTS do no git/transcript/
# metric work — ``safe_send`` (wait=False) writes a JSON spec to
# ``runtime/notify-queue/`` and spawns ``python -m cc_continuity
# notify-emit``; the child runs ``emit_spec`` (collectors + curl). The
# ``wait=True`` path (CLI/doctor, never a hook) calls ``emit_spec`` inline so
# a failure stays observable. P9-D7: neither side ever raises.


def _queue_dir() -> Path:
    return paths.runtime_dir() / "notify-queue"


def emit_spec(spec: dict, *, cfg: dict | None = None,
              msg_fn=build_message, runner=None,
              unlink: bool = True) -> bool:
    """Build and send one queued push (the detached child's whole job).

    Best-effort enrichment — every failure omits only its own line (B3):
    ``name`` falls back to the sid's session record; ``objective``/``next``
    come from ``fields["handoff_path"]``; ``stop_done`` gets its ``said``
    excerpt from ``fields["transcript_path"]``. ``notify_content`` off
    (B5/B8) strips the content bodies ``said``/``msg``/``reason``/``summary``
    (the scrubber already guards them whenever they ship). NEVER raises;
    the spec file (``spec["_path"]``) is consumed in ``finally``.
    """
    path = spec.get("_path") if isinstance(spec, dict) else None
    event = str(spec.get("event")) if isinstance(spec, dict) else "?"
    try:
        if cfg is None:
            cfg = config.load_config()
        slug = str(spec.get("slug") or "")
        nnn = int(spec.get("nnn") or 0)
        fields = dict(spec.get("fields") or {})
        sid = spec.get("sid") or None
        name = str(spec.get("name") or "")
        if not name and sid:
            from . import statusline
            rec = statusline.find_session_by_id(str(sid))
            if isinstance(rec, dict):
                name = str(rec.get("name") or "")
        if name:
            fields.setdefault("name", name)
        env = build_env(slug, nnn, name=name, sid=sid,
                        repo=spec.get("repo"))
        hp = fields.get("handoff_path")
        if hp:
            fields.setdefault("objective", objective_from(str(hp)))
            fields.setdefault("next", naa_snippet(str(hp), 200))
        if event == "stop_done" and "said" not in fields:
            fields["said"] = last_assistant_text(
                fields.get("transcript_path"))
        if cfg.get("notify_content") is False:
            for k in ("said", "msg", "reason", "summary"):
                fields.pop(k, None)
        title, body, priority = msg_fn(event, slug=slug, nnn=nnn,
                                       env=env, **fields)
        return send(str(cfg.get("notify_endpoint") or "").strip(),
                    title, body, priority, event=event, wait=True,
                    tags=TAGS.get(event, ""), runner=runner or subprocess.run)
    except Exception:
        _log_failure(event, "emit_spec: "
                     + (traceback.format_exc().splitlines()[-1]
                        if __debug__ else ""))
        return False
    finally:
        if path and unlink:
            try:
                Path(str(path)).unlink()
            except OSError:
                pass


def safe_send(cfg: dict, event: str, *, slug: str, nnn: int = 0,
              dedup_key: str | None = None, wait: bool = False,
              spawn=None, **fields) -> bool:
    """The single call-site wrapper (spec §4 + issue #7 B1).

    No-ops when disabled (no spec file, no spawn — Review Focus 5) and
    dedups error events. ``wait=False`` (hooks/rotator): atomically writes
    the spec and spawns the detached ``notify-emit`` child — no
    git/transcript/metric work in the hook budget. ``wait=True``
    (CLI/doctor): resolves sid/repo best-effort and runs ``emit_spec``
    inline (synchronous, observable). NEVER raises. Returns True when a
    push was fired (detached) or sent (wait).
    """
    try:
        if not enabled(cfg):
            return False
        if dedup_key is not None and dedup_seen(dedup_key):
            return False
        spec = {
            "event": event, "slug": slug, "nnn": nnn,
            "name": str(fields.get("name") or ""),
            "sid": (fields.get("sid")
                    or os.environ.get("CC_CONTINUITY_SESSION", "").strip()
                    or None),
            "repo": fields.get("repo") or os.getcwd(),
            "fields": dict(fields),
            "source": str(fields.get("source") or "safe_send"),
        }
        if wait:
            return emit_spec(spec, cfg=cfg)
        qdir = _queue_dir()
        qdir.mkdir(parents=True, exist_ok=True)
        path = qdir / f"{uuid.uuid4().hex}.json"
        atomic.atomic_write_json(path, spec)
        (spawn or subprocess.Popen)(
            [sys.executable, "-m", "cc_continuity", "notify-emit", str(path)],
            stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
            start_new_session=True)
        return True
    except Exception:
        _log_failure(event, "safe_send: "
                   + (traceback.format_exc().splitlines()[-1] if __debug__ else ""))
        return False


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
