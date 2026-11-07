# Changelog

All notable changes to cc-continuity are documented here.

## [Unreleased]

## [0.3.0] - 2026-11-07

### Added

- Visual "waiting for you" blink: while a session waits for user input,
  its iTerm2/Terminal.app tab blinks on its own (ON = the tab's own
  profile color, OFF = black; `blink_no_color: true` switches OFF to a
  red fallback) and the tmux window name alternates `WAIT` (or
  `WAIT[N]` for N waiting panes) with the original name at 0.5 s per
  phase. Color bytes ride OSC 6 written directly to each attached tmux
  client tty, so tmux never swallows them. A crash mid-blink self-heals
  on the next animator start: no tab stays painted and no window stays
  named WAIT. Gated by one config switch, `blink` (default on when tmux
  is detected), plus `blink_interval` and `blink_no_color`; disabling
  restores the previous behavior exactly.
- Status line ASK marker for unsubmitted permission prompts: a window
  blocked on a permission question is labeled distinctly from other
  waiting states in the status line and the progress pane.

## [0.2.0] - 2026-11-06

### Added

- Windows support via WSL (WSL 1 and WSL 2): platform detection and a
  hosting seam (launchd on macOS; a detached tmux session or systemd
  user unit on WSL), `cc-continuity daemon` subcommand with supervisor,
  WSL doctor checks, an optional Windows logon task for daemon
  autostart, and an end-to-end test for the WSL install
  (`tools/e2e_wsl.sh`). Full guide: `docs/WINDOWS.md`.

## [0.1.1] - 2026-10-17

### Fixed

- Release workflow: the PyPI publish job now runs on an ubuntu runner
  (`pypa/gh-action-pypi-publish` is a Docker container action that
  only runs on GNU/Linux).

## [0.1.0] - 2026-10-06

### Shipped

- Zero-touch context rotation: warn/rotate/hard threshold ladder,
  validated handoffs, checkpoint engine, non-invasive git tree
  snapshots (`refs/continuity/*`), full-auto rotator + bootstrap,
  `/continuity-*` in-session commands, crash/reboot resume.
- Managed, reversible installer: `settings.json` merge with backup,
  `CLAUDE.md` block, zsh `claude()` wrapper, six skills, launcher;
  byte-for-byte `uninstall`; `doctor` health checks.
- Lifecycle notifications over ntfy (opt-in): 10 event types,
  ≤ 200-char bodies, secret redaction, needs-human BLOCKED flow,
  unattended mode with loud warning.
- LLM watchdog daemon (opt-in): health probe with 2-fail debounce,
  turn-liveness classification, bounded nudge state machine with
  enriched prompts, exit-hang killer, frozen-turn escalation with
  ESC force-cancel and generation probe, zombie-session cleanup,
  auto power-cycle + log digest relay.
- Status line context segment, progress pane modes, portable defaults
  (opt-in network recovery + power plug), English product strings,
  redaction patterns extensible via `redact_patterns_extra`.
- 1100+ unit/contract tests; stdlib-only runtime; macOS (v1).
