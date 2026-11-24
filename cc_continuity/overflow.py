"""Context-400 brick recovery (connector-258, 2026-11-22).

Evidence (read-only, under $HOME - never delete): the connector session
258 ledger (``~/.claude-continuity/projects/connector-59910243/sessions/
258/ledger.jsonl``) shows the Stop hook NEVER fired on the context-400
turn (no ``stop`` entries after the trailing ``user_prompt`` rows - only
``watchdog_freeze`` + ``session_end``), and the transcript
(``~/.claude/projects/-Users-eralp-arid-connector/
64329cd9-5e27-4f5c-807b-d651f382f4eb.jsonl``, rows 433/438/454) carries
the signature: a ``type == "assistant"`` record whose ``message`` has
``model == "<synthetic>"`` and a text block
``API Error: 400 This model's maximum context length is 262144 tokens.
However, you requested 32000 output tokens and your prompt contains at
least 230145 input tokens...``. Every later request 400s - the model can
never write the handoff itself, so recovery is deterministic: the C.7
checkpoint (HANDOFF_FAILED -> finalize) + ``rotator.spawn``.

Row 447 of the same file carries the OTHER synthetic kind
(``"No response requested."``) - the marker conditions below exclude it.

The watchdog calls :func:`scan_tail_400` on its FROZEN classification
(the Stop hook cannot host detection - it never fires here) and
:func:`recover_400` on a hit; :mod:`cc_continuity.thresholds` uses
:func:`estimate_ratio` as the METRIC_FLOOR when the status-line metric is
stale (a frozen pane stops rendering, so the metric freezes at its last
value and every ratio-driven guard parks on None).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from . import (checkpoint, ledger, notify, paths, rotator, state, statusline,
               transcript)

#: Bytes read from the transcript tail per detection probe.
_TAIL_BYTES = 65536

#: The growing read windows for estimate_ratio (bytes); ``None`` = whole file.
_GROW_OFFSETS = (_TAIL_BYTES, 1 << 20, 8 << 20, None)

_400_MARKER = "API Error: 400"
_MARKER_RE = re.compile(r"maximum context length|prompt is too long")
_WINDOW_RE = re.compile(r"maximum context length is (\d+) tokens")
_INPUT_RE = re.compile(r"at least (\d+) input tokens")

#: The states recovery may drive (anything else - a rotation already in
#: flight, a terminal session - is a no-op skip).
_RECOVERABLE_STATES = frozenset({"BOOTSTRAPPING", "ACTIVE", "WARNED"})


def _log(detail: str) -> None:
    """One ``errors.log`` line tagged ``context400:``; never raises
    (transcript.debug_log pattern)."""
    try:
        with open(paths.logs_dir() / "errors.log", "a",
                  encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] "
                    f"context400: {detail}\n")
    except OSError:
        pass


def _assistant_messages_reverse(path: Path):
    """``message`` dicts of assistant records, newest first; the read window
    grows (64KB, 1MB, 8MB, whole file) until the file start is covered.
    Malformed JSON lines are skipped (a torn tail is not brick evidence)."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    for off in _GROW_OFFSETS:
        read_from = 0 if off is None or size <= off else size - off
        try:
            with open(path, "rb") as f:
                f.seek(read_from)
                data = f.read()
        except OSError:
            return
        if read_from > 0:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        msgs = []
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(rec, dict) and rec.get("type") == "assistant"
                    and isinstance(rec.get("message"), dict)):
                msgs.append(rec["message"])
        yield from reversed(msgs)
        if read_from == 0:
            return


def _record_text(msg: dict) -> str:
    """Concatenated text blocks of one assistant message (str content
    tolerated; anything else reads as empty)."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict)
                         and isinstance(b.get("text"), str))
    return ""


def scan_tail_400(path: Path | str | None) -> dict | None:
    """The context-400 brick signature on the LAST assistant record.

    Returns ``{"window", "input_tokens", "text_head"}`` when the newest
    assistant record is a synthetic 400 (``model == "<synthetic>"``, text
    carries ``API Error: 400`` plus the context marker), else None.
    Only the newest assistant record is examined - a ``user`` record after
    it means a new prompt arrived and the brick is broken; trailing
    ``system``/``last-prompt``/``file-history-snapshot`` records are not
    assistant records and are skipped (connector-258 tail shape).
    """
    if path is None:
        return None
    msg = next(_assistant_messages_reverse(Path(path)), None)
    if msg is None or msg.get("model") != "<synthetic>":
        return None
    text = _record_text(msg)
    if _400_MARKER not in text or not _MARKER_RE.search(text):
        return None
    wm = _WINDOW_RE.search(text)
    im = _INPUT_RE.search(text)
    return {"window": int(wm.group(1)) if wm else None,
            "input_tokens": int(im.group(1)) if im else None,
            "text_head": text[:200]}


def _tokens(usage: dict, field: str) -> int:
    v = usage.get(field)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    return 0


def estimate_ratio(path: Path | str | None,
                   window_tokens: object) -> float | None:
    """Transcript-side ratio for the METRIC_FLOOR (statusline.extract_metric
    formula): the newest assistant record with a NON-ZERO usage,
    ``used = input + cache_creation + cache_read`` over the configured
    window. Synthetic 400 records carry all-zero usage and are skipped;
    no usable record (or no positive window) -> None. Never raises."""
    if not isinstance(window_tokens, (int, float)) \
            or isinstance(window_tokens, bool) or window_tokens <= 0:
        return None
    if path is None:
        return None
    try:
        for msg in _assistant_messages_reverse(Path(path)):
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            used = (_tokens(usage, "input_tokens")
                    + _tokens(usage, "cache_creation_input_tokens")
                    + _tokens(usage, "cache_read_input_tokens"))
            if used > 0:
                return round(used / float(window_tokens), 4)
    except Exception:  # a decision helper never raises (P10-D12 contract)
        return None
    return None


def _fresh_prompt_after_checkpoint(key: str, nnn: int) -> bool:
    """BUG-3 (cp-258 live): True when the newest ledger ``user_prompt``
    post-dates the last ``state`` event into CHECKPOINT_VERIFIED - the
    user re-attached a verified checkpoint and typed again, then the API
    400'd the turn (Stop never fires, pane freezes). The ledger is the
    single source: state.json "updated" is unreliable (UserPromptSubmit
    touches it without a state change). BUG-8 (cp-265 live, connector-258):
    a deferred rotation re-stamps with an out-of-band ``ROTATING ->
    CHECKPOINT_VERIFIED`` revert; that rollback is NOT a new verification,
    so such entries never move ``verified_ts`` (they used to bury the
    fresh prompt and gate recovery out forever)."""
    try:
        entries = ledger.read(paths.session_dir(key, nnn))
    except OSError:
        return False
    prompt_ts: float | None = None
    verified_ts: float | None = None
    for entry in entries:
        etype = entry.get("type")
        if etype == "user_prompt":
            prompt_ts = entry.get("ts") or 0
        elif (etype == "state"
              and entry.get("to") == "CHECKPOINT_VERIFIED"
              and entry.get("from") != "ROTATING"):
            verified_ts = entry.get("ts") or 0
    return (prompt_ts is not None and verified_ts is not None
            and prompt_ts > verified_ts)


def recover_400(key: str, nnn: int, session_id: str | None, pane: str,
                cfg: dict, root: Path, info: dict | None) -> str:
    """Deterministic recovery from a context-400 brick (stop.py _degrade
    route, minus the hook context the daemon has none of):
    HANDOFF_REQUESTED -> HANDOFF_FAILED -> finalize (C.7 checkpoint,
    -> CHECKPOINT_VERIFIED) -> rotator.spawn -> ROTATING. The spawned
    rotator self-exits on its state re-read when a concurrent claim raced
    us (PRD 11.2). BUG-3: a CHECKPOINT_VERIFIED session whose newest
    user_prompt post-dates the checkpoint stamp (re-attached, user back,
    400 tail) skips the whole finalize walk - the checkpoint is already
    verified - and drives spawn -> ROTATING directly. NEVER raises:
    returns "recovered", "skipped:<state>" or "failed:<ExcName>".
    """
    from . import thresholds  # lazy: thresholds imports this module
    try:
        rec = state.load_state(key, nnn)
        verified = False
        if rec["state"] not in _RECOVERABLE_STATES:
            if not (rec["state"] == "CHECKPOINT_VERIFIED"
                    and _fresh_prompt_after_checkpoint(key, nnn)):
                return f"skipped:{rec['state']}"
            verified = True
        if not verified:
            thresholds.advance(key, nnn, "HANDOFF_REQUESTED", cfg=cfg)
            state.transition(key, nnn, "HANDOFF_FAILED")
            metric = statusline.read_metric(session_id) if session_id else None
            checkpoint.finalize(key, nnn, cfg=cfg, root=Path(root),
                                metric=metric)
        rotation_id = str(uuid.uuid4())
        rotator.spawn(session_id or "", pane, rotation_id)
        try:
            state.transition(key, nnn, "ROTATING", rotation_id=rotation_id)
        except state.InvalidTransitionError as e:
            _log(f"ROTATING transition raced for {key} {nnn:03d} "
                 f"(spawned rotator self-exits): {e}")
        info = info or {}
        ledger.append(paths.session_dir(key, nnn),
                      {"type": "context400_recovered",
                       "window": info.get("window"),
                       "input_tokens": info.get("input_tokens")})
        # BUG-7: a context-400 kills the turn WITHOUT a Stop hook, so
        # hooks/stop.py never clears turn_active/last_stop_ts and the
        # spawned rotator's PRD 11.3 idle gate defers forever waiting for a
        # "next Stop" that can never come. Mirror the Stop hook's field
        # write here - the turn is provably dead (400 tail; fresh-prompt
        # gate ran first). AFTER the ledger append so "no ledger activity
        # since last Stop" sees the recovery itself as the last event.
        state.update_fields(key, nnn, turn_active=False,
                            last_stop_ts=time.time())
        notify.safe_send(
            cfg, "context400", slug=notify.project_slug(key), nnn=nnn,
            dedup_key=f"context400:{nnn:03d}",
            what="context-400 auto-rotation",
            detail=f"window={info.get('window')} "
                   f"input={info.get('input_tokens')}")
        _log(f"recovered {key} {nnn:03d} pane={pane} "
             f"window={info.get('window')} input={info.get('input_tokens')}")
        return "recovered"
    except (state.InvalidTransitionError, checkpoint.CheckpointError,
            rotator.RotatorSpawnError, OSError) as e:
        _log(f"recovery failed for {key} {nnn:03d}: {e}")
        return f"failed:{type(e).__name__}"
