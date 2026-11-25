"""Bootstrap / claim in the new session (PRD §9 SessionStart, §12, §18).

``try_claim`` (caller holds the project lock) finds the latest session in a
claimable state (``ROTATING`` / ``AWAITING_MANUAL_CLEAR`` /
``CHECKPOINT_VERIFIED``, ``successor`` unset — plan P6-D2), creates the
successor session record (``BOOTSTRAPPING``), marks the old session claimed
(``successor`` set) and moves it to ``ROTATED`` (terminal), consumes the
matching checkpoint, runs ``validate_checkpoint`` (PRD §12) and returns the
C.4 bootstrap text. Called from the ``SessionStart(clear)`` hook and from
the ``UserPromptSubmit`` ``CONTINUITY RESUME`` fallback (P6-D5).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import (atomic, checkpoint, ledger, naming, notify, paths, project,
               state, statusline, thresholds, validate)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


#: States from which SessionStart(clear) may claim a rotation (P6-D2: CV
#: included so a manual /clear after checkpoint verification — e.g. after a
#: failed injection, PRD §18 — still bootstraps).
CLAIMABLE_STATES = frozenset({
    "ROTATING", "AWAITING_MANUAL_CLEAR", "CHECKPOINT_VERIFIED",
})


def find_claimable(key: str,
                   states: frozenset[str] | None = None) -> dict | None:
    """The latest project session a claim may take over (P6-D2).

    ``states`` narrows which claimable states qualify (default
    ``CLAIMABLE_STATES``; the startup claim passes ``{"ROTATING"}`` — a
    plain startup must never steal an AMCL/CV session that is still live
    in the user's terminal). Caller holds the project lock. Returns the
    state record, or None. A project with no record yet (first /clear
    before any registration, P6-D3) and sessions whose state file is
    missing (crash residue) are skipped.
    """
    states = states or CLAIMABLE_STATES
    pfile = paths.project_dir(key) / "project.json"
    try:
        pref = json.loads(pfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    best = None
    for s in pref.get("sessions") or []:
        try:
            rec = state.load_state(key, int(s["nnn"]))
        except (state.StateNotFoundError, ValueError, KeyError):
            continue
        if rec["state"] in states and rec.get("successor") is None:
            best = rec
    return best


def write_started(session_id: str) -> Path:
    """``runtime/<sid>.started`` marker for the rotator (PRD §14)."""
    path = paths.runtime_dir() / f"{session_id}.started"
    atomic.atomic_write_text(path, "started\n")
    return path


def pending_checkpoint(key: str) -> dict | None:
    """``latest_checkpoint`` while ``pending_consumption`` (else None)."""
    try:
        rec = json.loads(
            (paths.project_dir(key) / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cp = rec.get("latest_checkpoint")
    if isinstance(cp, dict) and cp.get("pending_consumption"):
        return cp
    return None


def _decide_name(key: str, slug: str,
                 allow_env: bool = True) -> tuple[str, int]:
    """Wrapper-supplied name wins when it ends in ``-<digits>`` (PRD §13).

    ``allow_env=False`` (Phase 27 resume/fork, Phase 28 CLI late attach):
    the env name belongs to the ABANDONED startup process — the counter
    name wins, or the dead startup's name would be reused.

    The env name must also be FRESH: its sequence strictly above the
    project counter (checked atomically with the increment,
    ``project.fresh_counter``). A long-lived pane/shell carries the env
    value frozen at LAUNCH time; once the counter passes it, a plain
    startup would register a duplicate/stale name while the counter
    advances (regression 2026-10-20: sessions 113/114/120 all named
    "…-101"). Stale → the counter's own name wins.
    """
    if allow_env:
        env_name = os.environ.get("CC_CONTINUITY_NAME", "").strip()
        _base, seq = naming.split_name(env_name)
        if seq is not None:
            nnn, fresh = project.fresh_counter(key, requested=seq)
            if fresh:
                return env_name, nnn
            return naming.logical_name(slug, nnn), nnn
    nnn = project.next_counter(key)
    return naming.logical_name(slug, nnn), nnn


def _tmux_bin() -> str:
    return os.environ.get("CC_CONTINUITY_TMUX_BIN") or "tmux"


def record_tmux_session(key: str, nnn: int) -> None:
    """P10-D9.1: record the pane's tmux session name (the watchdog's
    primary nudge target).

    Only when ``$TMUX`` is set; one fast subprocess (2 s timeout — the
    hook keeps its ~100 ms budget in the common case); every failure
    path is swallowed — this must never break the hook (P10-D12).
    """
    if not os.environ.get("TMUX"):
        return
    try:
        proc = subprocess.run(
            [_tmux_bin(), "display-message", "-p", "#{session_name}"],
            capture_output=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    name = proc.stdout.decode(errors="replace").strip()
    if not name:
        return
    try:
        state.update_fields(key, nnn, tmux_session=name)
    except Exception:
        pass


def register_session(cwd, session_id: str, *, cfg: dict,
                     source: str = "manual",
                     allow_env: bool = False) -> tuple[str, int]:
    """Register a conversation that has no record (late attach).

    Phase 28: ``cc-continuity checkpoint`` / ``request-rotation`` run in
    a session whose conversation was never registered (e.g. a process
    started before the Phase 27 resume registration — 2026-10-03
    connector a7c36f64, unmonitored at 72%). Creates its record the way
    the SessionStart hook does — per-operation locking, since the helpers
    take the project lock themselves and a wrapping ``lock.held`` would
    self-deadlock (flock is not re-entrant): counter name (unless
    ``allow_env``), state ACTIVE (BOOTSTRAPPING when a checkpoint is
    pending), one ``session_start`` ledger entry, the
    ``session_started`` push, tmux re-record.
    Returns ``(key, nnn)``.
    """
    root = project.resolve_root(Path(cwd))
    key = project.project_key_for_root(root)
    record = project.ensure_project(str(cwd))
    sessions = record.get("sessions") or []
    predecessor = sessions[-1]["name"] if sessions else None
    name, nnn = _decide_name(key, record["slug"], allow_env=allow_env)
    initial = "BOOTSTRAPPING" if pending_checkpoint(key) else "ACTIVE"
    state.create_session_state(key, name, nnn, session_id,
                               predecessor=predecessor,
                               initial_state=initial)
    project.record_session(key, name, session_id, nnn)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "session_start", "source": source,
                   "name": name, "nnn": nnn})
    notify.safe_send(cfg, "session_started",
                     slug=notify.project_slug(key), nnn=nnn, name=name,
                     host=notify.host(), prev=predecessor or "")
    record_tmux_session(key, nnn)
    return key, nnn


def _new_work_since_handoff(cp_dir: Path, key: str,
                            old_nnn: int) -> list[str]:
    """The user prompts typed AFTER the checkpoint snapshot was written —
    the rotation window (handoff finalizing / old session still live):
    they land in the OLD session's live ledger, but the handoff is already
    frozen and can no longer contain them, so they would be lost to the
    successor (the rotation-window bug: new prompts landing after the handoff froze).

    Marker: the snapshot's ``created`` (epoch; falls back to the
    checkpoint dir mtime when missing). The snapshot's ``user_prompts``
    marks what the handoff already captured, so a prompt present in BOTH
    (``ts >= created`` and captured — the same-second race) is not
    re-sent: no double-count. ``CONTINUITY RESUME`` prompts (bootstrap
    hand-off) and subagent prompts (``agent_id``) are not user work.
    Bounded: the last 5, each cut to the snapshot's own prompt width.
    Never raises."""
    try:
        snap = json.loads((cp_dir / "snapshot.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snap = {}
    captured = {str(p) for p in snap.get("user_prompts") or []}
    cut = int(getattr(checkpoint, "PROMPT_CHARS", 500))
    try:
        created = datetime.fromisoformat(str(snap["created"])).timestamp()
    except (KeyError, TypeError, ValueError):
        try:
            created = cp_dir.stat().st_mtime
        except OSError:
            return []
    out: list[str] = []
    try:
        entries = ledger.read(paths.session_dir(key, old_nnn))
    except OSError:
        return []
    for e in entries:
        if e.get("type") != "user_prompt" or e.get("agent_id"):
            continue
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or ts < created:
            continue
        text = str(e.get("text", ""))
        if text.startswith("CONTINUITY RESUME"):
            continue
        if text[:cut] in captured:
            continue
        out.append(text[:cut])
    return out[-5:]


def _find_reusable_successor(pref: dict, key: str, old_name: str) -> dict | None:
    """BUG-10: the BOOTSTRAPPING record this claim minted earlier but never
    attached (predecessor == ``old_name``, ``successor`` unset, older than
    ``REUSE_MIN_AGE_S``). Liveness is not probed: a live session is ACTIVE,
    never BOOTSTRAPPING, so adoption cannot hijack a running pane."""
    for s in reversed(pref.get("sessions") or []):
        try:
            rec = state.load_state(key, int(s["nnn"]))
        except (state.StateNotFoundError, ValueError, KeyError):
            continue
        if (rec.get("predecessor") != old_name
                or rec["state"] != "BOOTSTRAPPING"
                or rec.get("successor")):
            continue
        try:
            created = datetime.fromisoformat(rec["created"])
        except (KeyError, TypeError, ValueError):
            continue
        if (datetime.now(timezone.utc) - created).total_seconds() < REUSE_MIN_AGE_S:
            continue
        return rec
    return None


def _tmux_session_names(runner=subprocess.run) -> set | None:
    """Live tmux session names; None when tmux is unavailable or the probe
    failed (unknown, never "empty")."""
    try:
        proc = runner(["tmux", "list-sessions", "-F", "#{session_name}"],
                      capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {ln.strip() for ln in proc.stdout.decode(errors="replace").splitlines()
            if ln.strip()}


def _pane_record_dead(rec: dict, *, runner=subprocess.run) -> bool:
    """Issue #98: liveness probe for a record's recorded pane. True ONLY
    when the record recorded its own tmux pane and that pane is absent
    from the live list. Conservative by construction: no recorded pane,
    tmux unavailable or any probe error → False (treated as alive, today's
    behavior; a live pane is never hijacked)."""
    pane = rec.get("tmux_session")
    if not pane:
        return False
    names = _tmux_session_names(runner=runner)
    if names is None:
        return False
    return pane not in names


def _find_dead_successor(pref: dict, key: str, old_name: str) -> dict | None:
    """Issue #98 (the gap v0.3.65 left open): this predecessor's ACTIVE
    successor (``predecessor == old_name``, ``successor`` unset, older than
    ``REUSE_MIN_AGE_S``) whose recorded pane is DEAD - the attached-then-
    killed successor that ``_find_reusable_successor`` must skip because a
    live session is ACTIVE. Adoption honors the no-index-jumping rule."""
    for s in reversed(pref.get("sessions") or []):
        try:
            rec = state.load_state(key, int(s["nnn"]))
        except (state.StateNotFoundError, ValueError, KeyError):
            continue
        if (rec.get("predecessor") != old_name
                or rec["state"] != "ACTIVE"
                or rec.get("successor")):
            continue
        try:
            created = datetime.fromisoformat(rec["created"])
        except (KeyError, TypeError, ValueError):
            continue
        if (datetime.now(timezone.utc) - created).total_seconds() < REUSE_MIN_AGE_S:
            continue
        if _pane_record_dead(rec):
            return rec
    return None


def try_claim(key: str, *, new_session_id: str, cfg: dict,
              states: frozenset[str] | None = None,
              source: str = "clear") -> dict | None:
    """Claim the latest claimable session for a fresh session (PRD §9).

    ``states`` narrows which claimable states qualify (default
    ``CLAIMABLE_STATES``); ``source`` labels the successor's
    ``session_start`` ledger entry (``"clear"`` for the /clear claim,
    ``"startup"`` for the ROTATING-only startup claim). Caller holds the
    project lock. Returns ``None`` when nothing is claimable (idempotent:
    a second claim sees ``successor`` set). On success returns
    ``{"old_name", "old_nnn", "new_name", "new_nnn", "validation",
    "details", "bootstrap_text"}`` and has: created the successor state
    (BOOTSTRAPPING, predecessor = old name, nnn = old+1 at least) - or,
    when this predecessor's BOOTSTRAPPING successor already exists but was
    never attached, adopted it at the same index (BUG-10, ``reused``) - set
    the old session's ``successor`` and moved it to ROTATED, cleared
    ``pending_consumption`` on the matching latest_checkpoint, appended
    one ``session_start`` ledger entry to the successor, and updated
    project.json (counter / sessions / checkpoint) in one atomic write.
    """
    old = find_claimable(key, states)
    if old is None:
        return None
    pfile = paths.project_dir(key) / "project.json"
    pref = json.loads(pfile.read_text(encoding="utf-8"))
    root = Path(pref["root"])
    old_nnn = int(old["nnn"])
    reuse = _find_reusable_successor(pref, key, old["name"])
    corpse = _find_dead_successor(pref, key, old["name"]) if reuse is None else None
    if reuse is not None or corpse is not None:
        # BUG-10 (connector 258/259 live): this claim's own never-attached
        # successor already sits at its index - adopt it (standing "no
        # session index jumping" rule) instead of minting a second name.
        # Issue #98 adds the attached-then-killed ACTIVE variant, adopted
        # the same way after its recorded pane is probed dead.
        nnn_new = int((reuse or corpse)["nnn"])
        name_new = (reuse or corpse)["name"]
        # Issue #99 (connector-280 live): the successor may carry the
        # off=True marking its abandoned startup got from
        # SessionEnd(reason=resume) (regression 2026-09-27 047). Adoption
        # makes this record the live session, so clear off in the same
        # write - a stale off would leave hooks/rotator/watchdog gating
        # out a real attached pane and the status line rendering "off".
        if corpse is not None:
            state.update_fields_unlocked(key, nnn_new,
                                         session_id=new_session_id,
                                         state="BOOTSTRAPPING",
                                         off=False)
        else:
            state.update_fields_unlocked(key, nnn_new,
                                         session_id=new_session_id,
                                         off=False)
        entry = next((s for s in pref["sessions"]
                      if int(s.get("nnn", -1)) == nnn_new), None)
        if entry is not None:
            entry["session_id"] = new_session_id
    else:
        nnn_new = max(int(pref["counter"]), old_nnn) + 1
        name_new = naming.logical_name(naming.split_name(old["name"])[0], nnn_new)
        state.create_session_state(key, name_new, nnn_new, new_session_id,
                                   predecessor=old["name"],
                                   initial_state="BOOTSTRAPPING")
    state.update_fields_unlocked(key, old_nnn, successor=name_new)
    state._transition_unlocked(key, old_nnn, "ROTATED")
    cp = pref.get("latest_checkpoint")
    if isinstance(cp, dict) and cp.get("nnn") == old_nnn \
            and cp.get("pending_consumption"):
        cp = dict(cp)
        cp["pending_consumption"] = False
        cp["consumed_by"] = name_new
        pref["latest_checkpoint"] = cp
    pref["counter"] = max(int(pref["counter"]), nnn_new)
    if reuse is None and corpse is None:
        pref["sessions"].append({"name": name_new, "session_id": new_session_id,
                                 "nnn": nnn_new, "created": _now()})
    atomic.atomic_write_json(pfile, pref)
    cp_dir = paths.session_dir(key, old_nnn) / "checkpoint"
    status, details = validate.validate_checkpoint(cp_dir, cfg, root=root)
    handoff_file = cp_dir / "handoff.md"
    try:
        handoff = handoff_file.read_text(encoding="utf-8")
    except OSError:
        handoff = ""
    text = thresholds.c4_text(
        new_name=name_new, old_name=old["name"], status=status,
        details=details, handoff=handoff, handoff_path=str(handoff_file),
        inline_max=int(cfg.get("inline_handoff_max_chars", 12000)),
        new_work=_new_work_since_handoff(cp_dir, key, old_nnn))
    start_entry = {"type": "session_start", "source": source,
                   "claimed": old["name"], "validation": status}
    if reuse is not None or corpse is not None:
        start_entry["reused"] = True
        if corpse is not None:
            start_entry["reused_dead"] = True
    ledger.append(paths.session_dir(key, nnn_new), start_entry)
    return {"old_name": old["name"], "old_nnn": old_nnn,
            "new_name": name_new, "new_nnn": nnn_new,
            "validation": status, "details": details,
            "bootstrap_text": text}


#: Index stability (user directive 2026-11-03): a BOOTSTRAPPING successor
#: younger than this may still be mid-rotation (the rotator drives it on a
#: RETRY_POLL_S cadence) — never re-attached, so a legit in-flight successor
#: is never stolen. Restart storms arrive minutes later (human/pane relaunch),
#: far past this window.
REUSE_MIN_AGE_S = 60.0


def try_reuse_stalled(key: str, *, new_session_id: str, cfg: dict,
                      source: str = "startup",
                      allow_same_sid: bool = False) -> dict | None:
    """Re-attach a fresh startup to a STALLED successor — same index.

    The 2026-11-03 connector incident: the rotator's RESUME Enter was
    swallowed by a busy CC, the successor sat BOOTSTRAPPING with a dead sid,
    and every pane restart minted a new empty record (152/153/154 behind a
    stuck 151). User directive: that index creep must never happen again.

    A startup therefore first tries this (caller holds the project lock)
    before registering a new record: the LATEST record must be BOOTSTRAPPING,
    ``successor`` unset, older than ``REUSE_MIN_AGE_S``, and its sid must be
    dead (statusline metric absent/stale — never reported / long silent). A
    live successor (fresh metric) is left alone; a plain register follows.

    ``allow_same_sid`` (issue #42): CC's native auto-resume can bring back
    the stalled successor's OWN dead sid; with the opt-in the sid-equality
    refusal is lifted and the record is re-announced (sid unchanged).

    On success the record keeps its nnn/name; only the sid changes (state +
    project.json), one ``session_start`` ledger entry is appended, and the
    predecessor's C.4 bootstrap text is rebuilt (idempotent re-delivery of
    the handoff). Returns a claim-shaped dict with ``reused=True``, or None.
    """
    pfile = paths.project_dir(key) / "project.json"
    try:
        pref = json.loads(pfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sessions = pref.get("sessions") or []
    if not sessions:
        return None
    last = sessions[-1]
    nnn = int(last["nnn"])
    rec = state.load_state(key, nnn)
    if (rec is None or rec["state"] != "BOOTSTRAPPING"
            or rec.get("successor")
            or (rec.get("session_id") == new_session_id
                and not allow_same_sid)):
        return None
    try:
        created = datetime.fromisoformat(rec["created"])
    except (KeyError, TypeError, ValueError):
        return None
    if (datetime.now(timezone.utc) - created).total_seconds() < REUSE_MIN_AGE_S:
        return None
    if not statusline.is_stale(statusline.read_metric(rec["session_id"])):
        return None
    pred_name = rec.get("predecessor")
    pred = next((s for s in sessions if s.get("name") == pred_name), None)
    if not pred_name or pred is None:
        return None
    old_nnn = int(pred["nnn"])
    # Re-attach: same nnn/name, new sid everywhere. A re-announce (issue
    # #42, own sid) has nothing to move: same guards, C.4 rebuilt below.
    if rec.get("session_id") != new_session_id:
        state.update_fields_unlocked(key, nnn, session_id=new_session_id)
        last["session_id"] = new_session_id
        atomic.atomic_write_json(pfile, pref)
    cp_dir = paths.session_dir(key, old_nnn) / "checkpoint"
    root = Path(pref["root"])
    status, details = validate.validate_checkpoint(cp_dir, cfg, root=root)
    handoff_file = cp_dir / "handoff.md"
    try:
        handoff = handoff_file.read_text(encoding="utf-8")
    except OSError:
        handoff = ""
    text = thresholds.c4_text(
        new_name=rec["name"], old_name=pred_name, status=status,
        details=details, handoff=handoff, handoff_path=str(handoff_file),
        inline_max=int(cfg.get("inline_handoff_max_chars", 12000)),
        new_work=_new_work_since_handoff(cp_dir, key, old_nnn))
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "session_start", "source": source,
                   "claimed": pred_name, "validation": status,
                   "reused": True})
    return {"old_name": pred_name, "old_nnn": old_nnn,
            "new_name": rec["name"], "new_nnn": nnn,
            "validation": status, "details": details,
            "bootstrap_text": text, "reused": True}
