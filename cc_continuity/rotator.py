"""Full-auto rotator (PRD §11). Implemented in Phase 6.

Spawned detached by the Stop hook: project rotation lock (flock, stale
takeover) → re-read state (must be ROTATING with the same rotation_id) →
idle check (settle_ms, turn_active, no ledger activity) → ``C-u`` →
``send-keys /clear`` + Enter → wait for ``runtime/<sid>.started`` →
``/rename <slug>-<NNN+1>`` → continuation prompt via
``set-buffer``/``paste-buffer -p -d`` → verify ``CONTINUITY RESUME`` in the
new session's ledger within 15 s → ROTATION_COMPLETED or (retry once)
ROTATION_FAILED + notify. Never kills Claude Code, never deletes
checkpoints.
"""
