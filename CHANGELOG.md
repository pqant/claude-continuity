# Changelog

All notable changes to cc-continuity are documented here.

## [Unreleased]

## [0.3.28] - 2026-11-12

### Added

- A Dock bounce is no longer swallowed by macOS (issue #61): the
  system only bounces a BACKGROUNDED application, so the
  edge-triggered `RequestAttention=yes` written while iTerm2 is
  frontmost (the user still on another window or tab) vanished with
  no way to notice the wait later. The session poller and the pane
  animator now watch the frontmost app on their ~1 s blink tick
  (`AppBounce` with a `System Events` AppleScript probe) and re-write
  a still-armed bounce exactly once per foreground-to-background
  transition. Holding in the background re-fires nothing, and probe
  failures hold state (safe degrade to the pre-fix behavior).

## [0.3.27] - 2026-11-12

### Added

- The reply-ready blink now bounces the iTerm2 Dock icon too (issue
  #60): when a turn finishes and its tab waits to be read,
  `RequestAttention=yes` reaches the client ttys just like during
  needs-human waits, so a finished turn catches the eye while the user
  works in another app. Set `"blink_reply_attention": false` in the
  config to keep turn-ends tab-blink only. The needs-human,
  permission/selection and blocked bounces shipped in 0.3.26 are
  unchanged.

## [0.3.26] - 2026-11-12

### Added

- Needs-human and blocked waits now also bounce the iTerm2 Dock icon
  (issue #59), so the state is visible while the user works in another
  app's window: an unconsumed `needs_human` ledger reason (a
  `signal needs-human` or a watchdog stall), a permission or selection
  prompt, or the blocked marker arm `RequestAttention=yes` on the
  client ttys via OSC 1337. The bounce is cancelled with `no` when the
  wait is answered or the tab has been read (focus dwell), and a
  crashed poller's stranded bounce is healed by the startup sweep. The
  reply-ready blink deliberately stays tab-only: a bounce on every
  finished reply would be noise. Emulators without the OSC ignore the
  bytes.

## [0.3.25] - 2026-11-12

### Fixed

- Dead image chips no longer open next to the live one in exported
  transcripts (issue #58): one pasted image is written as two user
  records, and when the image cache is pruned before the export the
  sibling that carries only the `[Image: source: <path>]` line
  embedded nothing yet still opened its own non-clickable chip. An
  empty chip-only record that directly follows a block with a gallery
  now folds into that gallery; with no adjacent gallery it survives
  as a plain chip so the trace of the unrecoverable paste remains.

## [0.3.24] - 2026-11-12

### Fixed

- Auto-rotation no longer stalls forever after a verified handoff
  (issue #57): the Stop hook sets the stop timestamp and only then
  logs the handoff gate decision, and the rotator's idle check counted
  that `gate` ledger entry as session activity. The rotation deferred
  with "next Stop retries", the bounded retry ticks re-read the same
  frozen entry and gave up, and the Stop that ends the handoff turn
  is the last Stop that ever comes - the pane could sit in
  `CHECKPOINT_VERIFIED` indefinitely (observed: 34 minutes idle).
  `gate` entries are now excluded from the activity scan exactly like
  `state` bookkeeping; a real `user_prompt` after the Stop timestamp
  still defers the rotation.

## [0.3.23] - 2026-11-12

### Added

- The `🖼 image` chip is now clickable for records without base64 too
  (issue #56): when a record carries only the `[Image: source: <path>]`
  text line, the export reads that file from disk and embeds it as a
  `data:` URI, so the pasted image opens in the lightbox as well. The
  read happens at export time (rotation), because the
  `~/.claude/image-cache` directory is pruned later. Missing, pruned,
  non-whitelisted-extension or over-cap files (4 MB of base64 each)
  keep the plain non-clickable chip, and records that already embed a
  base64 block never re-read the path, so nothing duplicates.

## [0.3.22] - 2026-11-11

### Added

- The `🖼 image` chip in the exported HTML transcript is now clickable
  (issue #55): clicking it opens the real pasted image in a lightbox
  modal right on the page, with prev/next buttons and an `i / n`
  counter (backdrop click, the close button, or Escape dismisses it).
  The bytes come from the session JSONL's base64 blocks and are
  embedded as `data:` URIs, so the export stays a single
  self-contained HTML file. A folded run of chips shares one merged
  gallery; mixed records keep their human text and trail the chip.
  Non-whitelisted media types, non-base64 sources and over-cap
  images (4 MB of base64 each) keep the plain non-clickable chip,
  and pages without images are byte-identical to 0.3.21.

## [0.3.21] - 2026-11-11

### Fixed

- The exported HTML transcript no longer crowds the conversation with
  protocol plumbing (issue #54): slash-command XML from `/clear` and
  `/rename`, `<task-notification>` blocks, Continuity Stop-hook
  feedback injections, skill base-directory lines and assistant
  "No response requested." acknowledgements are dropped outright; the
  CONTINUITY RESUME blob and CC's "Continue from where you left off."
  prompt collapse to one-line anchors (`↻ CONTINUITY RESUME cp-NNN ·
  handoff.md`, `↻ CC resume`); image metadata lines fold into a
  single `🖼 image` chip (runs of chips collapse to one, human text
  in a mixed record survives without the metadata). A 2026-11-11
  scan of the project JSONLs counted eight such noise classes
  totaling ~325 records that had been rendered as bulky user bubbles.

## [0.3.20] - 2026-11-11

### Fixed

- A deferred rotation no longer waits forever (issue #53): when the
  rotator deferred a rotation because the agent was still active after
  the Stop, it relied on the next Stop event arriving - and if the turn
  had already ended, no next Stop ever came. Observed 2026-11-11: a
  pane idled ~16 minutes at 79% ROTATING with no way to progress. The
  defer now arms a detached, bounded self-retry tick: a short delay
  later it re-checks the session and, when the state is still
  `CHECKPOINT_VERIFIED` with the same `rotation_id` and the session is
  idle, it drives the rotation itself through the same rotator the
  Stop hook uses. The Stop-driven route stays primary; the
  rotation_id guard makes the tick idempotent (a fresh Stop re-arm or
  a completed rotation stands it down), the attempt chain is capped
  and giving up never touches state. Opt out with
  `"retry_deferred": false` in the config.

## [0.3.19] - 2026-11-11

### Fixed

- A DEGRADED handoff now tells the still-alive pre-rotation agent that
  the file is frozen (issue #52): the C7 header gains the directive
  "This file is finalized. Do not edit it and do not start any more
  work in this session; end your turn now." Right after a DEGRADED
  finalization on 2026-11-11 an old session reached back into the
  finalized file and attempted edits it no longer owns; the banner
  carried no order of its own for that agent. The sentence is pinned
  as a literal in the tests (a wording drift fails CI); the verified
  (non-degraded) path is untouched and verify keeps its exact-match
  check on the same constant.

## [0.3.18] - 2026-11-11

### Changed

- The status line HUD prev-task segment is now readable (issue #51):
  `⏱ Previous task completion 39m 15s` - full label, spaced units and
  an hour shape (`1h 2m 03s`, `47s`) instead of `⏱ prev task 39m15s`.
  Still opt-in via `turn_clock` and still carried on the Active Usage
  main text only (blink marker substring-matching untouched); the turn
  clock prompt line (#44) keeps its compact `(1h2m03s)` shape
  byte-identical.

## [0.3.17] - 2026-11-11

### Added

- `cc-continuity status [slug]` (issue #48): renders the same status
  block (rotation line and `--json` included) for any registered
  project, from anywhere. Slug resolution is shared with
  `cc-continuity ratio` (one helper, identical error lines), so the
  two commands can never point at different projects. The no-slug
  path keeps its cwd behavior byte-identical.

## [0.3.16] - 2026-11-11

### Added

- New `cc-continuity ledger [n]` (issue #46): the session ledger
  finally has a CLI reader. Gate decisions (#39), state transitions
  and prompts print newest-first as compact single lines
  (`HH:MM:SS type key=value…`), newest N (default 20, `n <= 0` falls
  back to 20). Values are flattened to one line and capped at 60
  characters. Session resolution mirrors `status` (#32): live marker
  first, newest registered session as fallback; a project with no
  resolvable session exits 1 with a clear message. Read-only: the
  command never writes and touches no hook path.

## [0.3.15] - 2026-11-11

### Added

- `cc-continuity ratio --json [slug]` (issue #34) prints exactly one
  JSON object - `slug`, `session_id`, `ratio`, `ts`, `stale` - for
  scripts and the watchdog. The plain human line remains the default,
  and failure paths keep the plain stderr line + exit 1 so callers
  parse stdout only on success.

## [0.3.14] - 2026-11-11

### Added

- `cc-continuity status` now carries a rotation summary line
  (issue #32): `rotation   ACTIVE · ratio 65.0% · last crossed
  rotate` - state, live ratio and the last threshold crossed in one
  line, in both human and `--json` output (new `rotation` object).
  The ratio resolves through the same fallback chain as
  `cc-continuity ratio` (#28), so the two commands can no longer
  disagree on the number.

## [0.3.13] - 2026-11-10

### Added

- The status line HUD now shows the previous task's wall time when
  `turn_clock` is on (issue #45): the `◆ …` segment reads
  `◆ 64% Active Usage ⏱ prev task 39m15s` on a normal turn, updated
  the moment the turn ends (Claude Code re-renders the status line
  continuously). The duration reuses the guarded transcript tail
  parser from #44 (prompt send to final reply, byte-capped read); a
  missing or unreadable transcript simply omits the segment, and the
  first turn of a session has none. Rotation/handoff states and all
  animation rules are untouched.

## [0.3.12] - 2026-11-10

### Fixed

- The reply half of the turn clock now appears in the pane
  (issue #44): the pinned Claude Code renderer only displays hook
  `systemMessage` for the prompt-side channel, so the Stop hook's
  clock line shipped in #43 was never visible (the hook itself ran
  fine, proven by ledger entries and an empty error log). Reply
  timing now rides the UserPromptSubmit line: each new prompt prints
  `cc-continuity: prompt HH:MM:SS | prev reply HH:MM:SS -> HH:MM:SS (2m34s)`,
  with the previous turn read from the transcript JSONL tail (a
  byte-capped read keeps the prompt path fast) and the duration
  rendered human-readable (`47s` under a minute). The first prompt of
  a session prints the prompt time only, and the never-rendered Stop
  clock block was removed.

## [0.3.11] - 2026-11-10

### Added

- Turn clock (issue #43): with `"turn_clock": true` in
  `~/.claude-continuity/config.json` (opt-in, default off), the pane
  shows when a prompt was sent and when the reply ran. UserPromptSubmit
  prints the prompt time as an informational line; a normal turn end
  prints `prompt HH:MM:SS | reply HH:MM:SS -> HH:MM:SS (Ns)` parsed from
  CC's own transcript JSONL, so the timing matches what the transcript
  HTML export shows. Missing or malformed transcripts print nothing and
  the Stop hook never fails. Times render in machine-local time.

## [0.3.10] - 2026-11-10

### Added

- The session ledger now records every rotation gate decision
  (issue #39): one compact `type: gate` entry with the action
  (guide/deny/block/silent), the state transition, the ratio when
  ratio-driven, and the C-text variant used (c2/c3/c9/c6). Auditing
  gate behaviour no longer needs binary-level reverse-engineering.
  Allowlisted passes record nothing, and a failed record write never
  affects hook behaviour.

## [0.3.9] - 2026-11-10

### Fixed

- `install` now pins `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=95` (issue #31).
  It previously wrote `CLAUDE_CODE_MAX_CONTEXT_TOKENS` but never the
  auto-compact override, so a fresh machine started the rotation race
  against native auto-compact at its default position; the doctor
  rotation-env check (#27) flagged the gap on every such machine. The
  override is written only when absent, so a user-set value always
  wins, and uninstall still restores `settings.json` exactly (the
  reinstall carry-over now covers this key too).

## [0.3.8] - 2026-11-10

### Added

- `cc-continuity signal --dry-run done|needs-human "text"` (issue #30):
  prints the resolved title, priority and body of the push - plus
  whether notifications are enabled at all - without touching the
  network or the ledger. Formatting checks no longer require a real
  push to the user's phone.

## [0.3.7] - 2026-11-10

### Changed

- README quickstart (issue #29): the PyPI path now recommends
  `pipx install cc-continuity` (with `uv tool install` as the
  alternative), the standard way to install Python CLIs; plain
  `pip install` stays documented for venv and library use.

## [0.3.6] - 2026-11-10

### Added

- `cc-continuity ratio [slug]` (issue #28): prints the latest known
  context ratio of the current (or named) project on one line, e.g.
  `claude-continuity 0.774`. Removes the manual metric-file reading
  from rotation trial bookkeeping and debugging; missing metrics or an
  unknown/ambiguous slug exit non-zero with a one-line message.

## [0.3.5] - 2026-11-10

### Added

- doctor rotation-env check (issue #27): `doctor` now verifies the
  rotation prerequisites in `~/.claude/settings.json` -
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (recommended 95, keeps native
  auto-compact behind the custom gate) and
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` (the window the ratio references).
  Drift is reported with the exact recommended/expected values; the
  check is warn-only, so a healthy install stays all-green.

## [0.3.4] - 2026-11-09

### Fixed

- Rotation stop gate (issue #26): a routine turn-end rotation no longer
  blocks the stop when the handoff draft is already valid - the
  rotation now finalizes and rotates silently. Claude Code renders
  every Stop hook block as a red "Stop hook error:" line (the prefix
  is hardcoded in the binary), so the routine case must never emit a
  block; blocks remain reserved for a missing or invalid draft after
  the model received its guidance.

## [0.3.3] - 2026-11-09

### Fixed

- Rotation gate notices (issue #25): a routine hard-ratio crossing no
  longer denies the in-flight tool call - the handoff instruction now
  arrives as a neutral context message and the user sees an
  informational note instead of a red "Error" line. When a gate denial
  is truly warranted (the model continues after being told), the full
  guidance likewise shows as an informational message while the model
  gets a short actionable reason.

## [0.3.2] - 2026-11-09

### Added

- Transcript session switcher (issue #16): every export refreshes a
  per-project `sessions.js` manifest, and the viewer header renders a
  dropdown of the newest exports (the top ten plus the current page),
  working over plain `file://` without a server.

### Fixed

- Transcript viewer header: the Sessions switcher now always sits in
  the same column as the Session Time field (the header is one shared
  grid), the search box keeps a fixed 360px width instead of
  stretching across the page, and every header label renders bold and
  high-contrast.

## [0.3.1] - 2026-11-08

### Added

- Architecture documentation: `docs/diagrams/cc-continuity-architecture.html`,
  a self-contained interactive diagram of modules, dependencies and data
  flows with guided views, dark/light themes and PNG/SVG export, plus its
  source spec `cc-continuity-architecture.architecture.json`.

### Fixed

- Tab blink notifications (issues #20 to #23): a fresh waiting situation is
  suppressed instantly when the tab is already focused; the reply flag is
  held through the settle window instead of being cleared early; the
  animator blink key carries the unconsumed reply ts; and the default
  reply settle window is now 5 s instead of 15 s.
- Progress pane: frames are padded by rendered cells so wide glyphs never
  wrap.

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
