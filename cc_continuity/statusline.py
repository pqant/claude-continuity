"""Status line entry — context metric (PRD §7). Implemented in Phase 2.

Reads the status line JSON from stdin, computes
``used_tokens = input + cache_creation + cache_read``, writes
``runtime/<session_id>.json`` atomically, chains the user's previous status
line command (saved as ``chained_statusline`` by the installer), and appends
our segment: ``⟳ 61% ok`` / ``⟳ 63% PENDING→proj-002`` / ``⟳ 73% HANDOFF!`` /
``⟳ off``. Must stay fast (< 50 ms of our own work).
"""
