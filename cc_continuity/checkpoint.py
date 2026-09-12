"""Checkpoint engine (PRD §10). Implemented in Phase 4.

- Draft validation: 9 template headings in order (Appendix B), required
  sections non-empty, ≤ ``handoff_max_words`` words, no redact-pattern
  secrets (PRD §10.1).
- Deterministic snapshot (PRD §10.2) produced by code only.
- Git tree snapshot via :mod:`cc_continuity.gitsnap`.
- Finalization (PRD §10.4): render handoff.md into ``checkpoint.tmp-<pid>/``,
  fsync, SHA256SUMS, re-read/verify, ``os.replace`` → ``checkpoint``; only
  then ``CHECKPOINT_VERIFIED`` + ``latest_checkpoint`` pointer.
- Degraded checkpoint (deterministic parts only, C.7 header) after 3 failed
  handoff attempts.
"""
