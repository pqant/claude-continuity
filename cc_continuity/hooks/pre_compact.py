"""PreCompact hook (PRD §9) — emergency deterministic snapshot (Phase 5,
spec §6.6).

Never blocks, prints nothing. Under the project lock: load the state +
project record, build the full deterministic snapshot (git snapshot
included; failures land in ``snapshot_error``), atomically write it to
``sessions/<NNN>/emergency-snapshot.json`` — the safety net in case the
rotation protocol never completes. One COMPACT_BEFORE_ROTATION line is
logged while the state is still before CHECKPOINT_VERIFIED (the user
should know a rotation was skipped). Subagent payloads are skipped
(spec D5).
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import atomic, checkpoint, lock, paths, state, statusline
from .common import HookContext, log_event

_POST_CHECKPOINT = frozenset({
    "CHECKPOINT_VERIFIED", "ROTATING", "AWAITING_MANUAL_CLEAR", "ROTATED",
})


def handle(ctx: HookContext) -> dict | None:
    if ctx.session is None or ctx.payload.get("agent_id"):
        return None
    key, nnn = ctx.session["project_key"], int(ctx.session["nnn"])
    with lock.held(key):
        rec = state.load_state(key, nnn)
        pfile = paths.project_dir(key) / "project.json"
        project_record = json.loads(pfile.read_text(encoding="utf-8"))
        metric = statusline.read_metric(ctx.session["session_id"])
        snapshot = checkpoint.build_snapshot(
            key=key, nnn=nnn, root=Path(project_record["root"]), cfg=ctx.cfg,
            metric=metric, degraded=False, created=rec["updated"],
            state_record=rec, project_record=project_record)
    atomic.atomic_write_json(ctx.session_dir / "emergency-snapshot.json",
                             snapshot)
    if rec["state"] not in _POST_CHECKPOINT:
        log_event(f"COMPACT_BEFORE_ROTATION state={rec['state']} nnn={nnn:03d}")
    return None
