"""Installer / uninstaller (PRD §17). Implemented in Phase 7.

``install --window <tokens>``: prerequisite checks; copy app to
``~/.claude-continuity/app/`` + symlink ``~/.local/bin/cc-continuity``;
merge (never replace) ``~/.claude/settings.json`` (hooks, statusLine chain,
``CLAUDE_CODE_MAX_CONTEXT_TOKENS``, permissions — home-relative
``~/.claude-continuity/**`` form per S12) with timestamped backup;
managed block in ``~/.claude/CLAUDE.md`` (Appendix A); user skills;
managed block in ``~/.zshrc`` (Appendix D); run doctor. Idempotent: entries
are identifiable by the substring ``cc-continuity``. ``uninstall`` restores
the chained status line and keeps ``projects/`` unless ``--purge``.

Only this module may touch ``~/.claude/settings.json``,
``~/.claude/CLAUDE.md`` and ``~/.zshrc`` (PRD §0.6).
"""
