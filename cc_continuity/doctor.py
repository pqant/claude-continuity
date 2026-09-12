"""Health checks (PRD §17). Implemented in Phase 7.

Checks: python ≥ 3.11, tmux present, hook/statusLine/settings entries
present, status line chained correctly, ``CLAUDE_CODE_MAX_CONTEXT_TOKENS``
vs observed ``context_window_size``, stale locks / ``*.tmp-*`` residue,
last entries of ``logs/errors.log``. Also re-runs S9/S10-style tmux checks
after a Claude Code version change (PRD §24).
"""
