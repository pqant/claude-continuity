"""Bootstrap validation in the new session (PRD §12). Implemented in Phase 4.

``cc-continuity validate --checkpoint <dir>`` checks: project path exists and
matches; branch/HEAD vs the snapshot (same / fast-forwarded / diverged);
current working-tree snapshot tree sha vs checkpoint tree sha; if different,
the ≤ 50 differing paths; files named in "Next Atomic Action" exist.
Output: one line ``VALIDATION: OK`` or ``VALIDATION: MISMATCH`` + details.
"""
