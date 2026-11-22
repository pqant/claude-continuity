# Changelog

All notable changes to cc-continuity are documented here.

## [Unreleased]

## [0.3.59] - 2026-11-22

### Fixed

- A context-400 overflow no longer bricks a session. When the API rejects the oversized prompt with a 400, the Stop hook never fires, so the model can never write its own handoff, the status line metric freezes at its last value, and the rotator (which reads that metric) never triggers: the watchdog nudged a pane that could only 400 again, forever. Three additive pieces close the loop. A new `overflow` module detects the trailing synthetic "API Error: 400 ... maximum context length" transcript record (excluding the "No response requested." synthetic kind), estimates the context ratio from the last real assistant usage, and recovers deterministically: the checkpoint is finalized by the watchdog itself (no model turn to 400 again) and the successor is spawned. The watchdog's FROZEN path now runs that detection and recovery before nudging, once per target, and falls through to the old Esc/escalation path when detection cannot read the project root. And `read_ratio` now falls back to the transcript-based estimate (logged as `METRIC_FLOOR`) when the metric is stale, instead of parking on `None`, so ratio-driven guards keep working while a pane is frozen. Fresh payloads are never overridden; the thresholds themselves are unchanged (issue #86).

- Chaining [claude-hud](https://github.com/jarrodwatts/claude-hud) (0.15.4+) no longer leaves an unexplained `+.claude-continuity` segment on the status line. HUD 0.15.4 started rendering every `workspace.added_dirs` entry inline after the project name, and our install legitimately registers `~/.claude-continuity` as an additional directory, so the segment appeared the day HUD auto-updated. `cc-continuity doctor` now detects the combination (claude-hud chain, our directory registered, HUD's `display.showAddedDirs` not set to false) and prints the one-line exact config fix; the README documents the interaction too (issue #85).

## [0.3.58] - 2026-11-21

### Fixed

- The two skills that were invisible to the `/cc` slash-menu filter now show up. Claude Code fuzzy-matches the menu against the command name and the description text, so the five managed skills whose descriptions contain "cc-continuity" (checkpoint, status, rotate, off, on) appeared when you typed `/cc`, while `continuity-history` and `continuity-excellence` matched neither and stayed hidden. Both descriptions now carry the keyword, and a new test pins "cc-continuity" into every managed skill description, so a future skill added without it fails CI instead of disappearing from the menu (issue #82).

## [0.3.57] - 2026-11-21

### Added

- `/continuity-excellence` turns a raw vague prompt into a run-ready one. Hand it a rough intent like "find the image files on my local disk" and it perfects the prompt across seven dimensions (explicit goal, scope boundaries, environment and paths, constraints, output format, edge cases, success criteria), fills the gaps it can infer from the session context, then presents your original wording verbatim next to the perfected prompt as one copyable block. It asks exactly once - run it, change it, or cancel - and acts on the answer: run executes the perfected prompt in this session, change applies the edits and re-presents, cancel stops cleanly. The raw prompt never runs; only the approved perfected version does. It is the first behavior skill (no shell command behind it) and carries `disable-model-invocation: true` like rotate/off/on, so only you can trigger the loop. The `doctor` skills count is now derived from the skill registry instead of a hardcoded literal (issue #81).

## [0.3.56] - 2026-11-21

### Fixed

- The README rotation GIF now plays slow enough to follow. The #40 demo cycled the whole rotation story in about 2 seconds, so the frames that actually matter - the end-of-ACTIVE hold, the two `HANDOFF!` frames, `ROTATING` and the fresh successor session - flashed by at ~320 ms and the handoff read as noise. The frames themselves are unchanged (still drawn from the real `render_segment` output, sweep still lively at ~110 ms); the dwell per story frame went up to 500-1000 ms and the loop to ~5 s, and a new test parses the GIF's per-frame delays so the pacing can never silently regress (issue #80).

## [0.3.55] - 2026-11-21

### Fixed

- `doctor` now clears stale project lock files instead of warning about them forever. `lock.release()` keeps `projects/<key>/lock` as the last-holder diagnostic, so every abandoned project left a file with a dead PID behind and the `! stale-locks` line became permanent noise on long-lived machines. Check 14 is self-healing now, following the tmp-residue precedent: when an entry is older than the stale threshold AND its recorded PID is dead, the kernel's flock state is the authority - if the flock is already released nobody can still hold it, so the leftover is removed and reported as `removed <names>`; if the flock is still held (the inherited-fd edge case) the file survives and the check reports `held: <names>` instead of lying about the state (issue #79).

## [0.3.54] - 2026-11-21

### Added

- `cc-continuity install --dry-run` prints the full managed-change plan before anything is written: every managed target (app copy, launcher stub, `~/.local/bin` symlink, `settings.json`, `~/.claude/CLAUDE.md`, the six skills, the login-shell rc block, `config.json`) carries a create/update/unchanged/replace status, and indented `+` bullets spell out exactly what a merge would add (hook events, the chained user status line, env keys, permission rules, additional directories, config keys). The plan walks the same decisions as the real install and aborts with the same messages and a non-zero exit on the conditions that would stop a real run (missing prerequisites, corrupt `settings.json`, a non-symlink `~/.local/bin/cc-continuity`, installing from the app dir); nothing on disk is touched (issue #41).

## [0.3.53] - 2026-11-21

### Fixed

- The reply blink no longer dies after a single look. The focus dwell consumed the reply the moment the user glanced at the tab, so reading the answer and then sending the window to the background without replying left the tab quiet and the pending answer forgotten. The blink poller (mode 0) and the pane animator (mode 1) now catch the window losing focus again while the reply is still unanswered (no newer main-session user prompt, no self-wake, not faded) and re-arm the blink with a fresh settle window, so the tab lights up again until the answer lands; once the user replies the re-arm stops (issue #78).

## [0.3.52] - 2026-11-21

### Added

- The status line now caches its heavy render stages (session lookup, ledger reads, transcript parse, pane capture, pane-data rewrite) between tool boundaries: the ~1/s idle renders reuse the cached facts instead of redoing the work, while the shimmer sweep, the reply settle marker and the payload ratio keep flowing so the line always animates and the HUD numbers keep moving. The mode-1 pane feed only rewrites when the data actually changes, and a corrupt or missing cache always falls back to the full render (issue #77).

## [0.3.51] - 2026-11-20

### Added

- The README embeds a short GIF of the animated status line (docs/assets/cc40-statusline-rotation.gif, 13 frames, under 40 KB): the live context-usage segment with its shimmer sweep, then the rotation handoff moments (`HANDOFF!` and `ROTATING`) and the successor's fresh session. Every frame is drawn from the real `statusline.render_segment()` output, so the README shows exactly what the pane shows (issue #40).

## [0.3.50] - 2026-11-19

### Fixed

- The Stop hook now consumes a handoff draft a WARNED session wrote on its own: after the c1 warning the model wrote `handoff.draft.md`, but the draft was only ever read in HANDOFF_REQUESTED, so the session idled at the prompt with a finished handoff on disk. A turn end in WARNED with a draft present now advances through the normal HANDOFF_REQUESTED path - a valid draft finalizes silently (no red Stop error line), an invalid one takes the existing guidance ladder; BOOTSTRAPPING/ACTIVE sessions never rotate on a self-written draft (issue #76).

## [0.3.49] - 2026-11-19

### Added

- `notify stop|notification --dry-run` renders the exact push without sending or recording anything (the same contract as `signal --dry-run`): the `enabled` state (a disabled or misconfigured push is shown instead of a silent no-op), `title`, `priority` and the body, and the one-push-per-minute bucket stays untouched so a later real push still fires. `watchdog status` now reports the daemon's freshness with a `last heartbeat: <local time> (<N>s ago)` line, read from the stamp the watchdog writes after every full tick (`runtime/watchdog-beat.json`); a missing or corrupt stamp reads `last heartbeat: never` (issue #37).

## [0.3.48] - 2026-11-18

### Added

- `doctor` gained a `config-sanity` check: the rotation gate order `0 < warn_ratio <= rotate_ratio <= hard_ratio < 1`, a plausible `window_tokens` (>= 32768) and an http(s) `notify_endpoint`. A typo like warn 0.80 / rotate 0.75 used to invert the gate silently and rotations fired late or never; the check is warn-only (the values stay the user's call) and names the offending values in its detail (issue #36).

## [0.3.47] - 2026-11-18

### Added

- `history --json` prints the session chain as one JSON array with stable keys (`nnn`, `name`, `session_id`, `created`, `state`) for machine consumption; the state is read from `sessions/<nnn>/state.json` with the same fallback the status rotation line uses, a project without a record prints `[]`, and an unreadable state is reported as null while the default human output stays unchanged (issue #38).

## [0.3.46] - 2026-11-17

### Added

- A second badges row under the title covers the package side: PyPI
  version and monthly-download counters, the MIT license, live CI
  status, supported Python versions and a good-first-issues counter
  that links to the labeled contributor on-ramp (issue #74).

### Changed

- pyproject.toml now declares its PyPI classifiers (Development
  Status, MIT license, Python 3.11/3.12/3.13), which feeds the
  Python-versions badge and enriches the PyPI project page.

## [0.3.45] - 2026-11-17

### Added

- The README opens with a badges row: live shields.io counters for forks,
  stars, open issues and the last commit, each linking to the matching
  GitHub page, plus Code of conduct, Contributions and Security badges
  linking into the community files (issue #73).

## [0.3.44] - 2026-11-17

### Fixed

- A pasted image no longer shows up twice in transcript exports. Claude
  Code writes one paste as two records (the base64 record plus an
  `isMeta` sibling carrying the same `[Image: source: ...]` lines); while
  the image cache was still on disk the sibling embedded the very same
  images and opened as a second User block repeating the whole gallery.
  A sibling whose timestamp and image count both match the block before
  it is the same paste and is now folded away; a genuine second paste
  carries its own timestamp and keeps its own gallery (issue #72).

## [0.3.43] - 2026-11-17

### Added

- `cc-continuity log [blink|errors]` tails the root `logs/` files read-only:
  newest last, default 20 lines, `--lines N` to resize, `--follow` to keep
  streaming appends. Stamps render in local time: explicit-offset UTC stamps
  (`errors.log`) are converted while naive local stamps (`blink.log`, written
  by `time.strftime`) and untimestamped traceback continuation lines pass
  through byte-identical, so nothing shifts twice (issue #35).

## [0.3.42] - 2026-11-15

### Added

- A tag push now opens its own GitHub Release. The release workflow
  extracts the matching `## [<version>]` section from this changelog with
  a stdlib-only `tools/changelog_notes.py` helper (a missing or empty
  section stops the run before any upload) and publishes it as the
  release notes, so the PyPI upload and the GitHub Release come from a
  single workflow run; re-runs refresh the notes instead of failing
  (issue #33). The manual `gh release create` step is retired.

## [0.3.41] - 2026-11-17

### Fixed

- The status line ratio metric is now written under the session
  the pane's hooks actually run as (issue #71, the #42 follow-up).
  After a dead-sid auto-resume Claude Code renders the status line
  with the abandoned startup sid while the hooks keep firing as the
  resumed conversation's sid, so the metric landed on the stale
  sid: the revived pane kept showing a ratio (e.g. `99.0%`) while
  the tracked record never moved (`state ROTATED`) - measurement
  without reaction. The hook entry now stamps a pane-scoped
  binding (`runtime/panes/pane-<TMUX_PANE>.json` with the sid, the
  pane and a timestamp) on every hook event, and the status line
  retargets the metric (and the rendered record) onto it when the
  binding is fresh (same 10-minute gate as metrics), names a
  different BOOTSTRAPPING/ACTIVE session, is not off, and is not
  older than the payload record's creation (same-pane sequential
  launches never inherit a previous session's binding). Outside
  tmux, and in every healthy pane (binding sid equals the payload
  sid), the write stays byte-identical to 0.3.40.

## [0.3.40] - 2026-11-16

### Fixed

- A plain `claude` launch that auto-resumes a DEAD conversation
  after a rotation no longer runs unmonitored (issue #42).
  Claude Code remembers the last opened conversation per project
  (lastSessionId); when that conversation is the rotated-away
  one (or the stalled successor itself, the dead-sid shape), the
  SessionStart resume/fork branch used to re-attach it silently
  to the dead record: no ratio movement from the terminal state,
  the C.4 handoff never delivered, and `request-rotation` from
  such a conversation refused with "already past". Now, when the
  resumed sid is not live (`statusline.is_stale`), the hook
  routes the conversation through the stalled-successor reuse
  machinery (same guards as the 2026-11-03 restart reuse,
  including its age gate): the live conversation anchors to the
  chain tip under the SAME index, receives the C.4 bootstrap
  text, and advances BOOTSTRAPPING to ACTIVE so the normal
  monitoring drives rotation. A fresh metric keeps the ordinary
  re-attach path byte-identical, and every failure path falls
  back to today's behavior.


## [0.3.39] - 2026-11-16

### Changed

- The transcript fold arrows are anchored (issue #70). Both
  triangles now center against the full header width, so the
  arrow sits at the true screen center instead of drifting
  left over the theme button (the controls row spans only
  the left half of the header grid). They carry a red tint
  (a deeper red in light theme, a lighter red in dark
  theme), and the reopen arrow shares the same centered
  slot when the panel is folded, so the button no longer
  jumps when toggling.


### Changed

- The transcript density view (issue #68) got its acceptance
  polish (issue #69). The two panel toggles are plain filled
  triangles now: the up-triangle in the controls row folds
  the header panel into the slim bar, the down-triangle on
  the bar brings it back, and the words live in tooltips.
  Collapse and expand ease over 180 ms instead of snapping.
  With the 2-column mode on, the folded bar only takes the
  width of the left column, so the strip above the content
  stops stretching full width. The fold arrow sits centered
  in the controls row and carries the theme accent color.
  And the "2-column · 75%" mode opens enabled by default
  now; unchecking it is still remembered in your browser.


### Added

- The transcript export gained two density controls in the
  header panel (issue #68). "panel up" folds the meta panel
  into a slim one-line bar (session name + "panel down") so
  the conversation starts at the top of the screen; "panel
  down" restores the full panel. A "2-column · 75%" checkbox
  in the controls row re-flows the blocks into two balanced
  columns at reduced zoom, spanning the full window width;
  message blocks never split across the column break and
  chronology is untouched. Both choices persist in the
  browser (localStorage), and search, theme, sessions
  switcher and the image lightbox behave identically in
  every state.

## [0.3.36] - 2026-11-14

### Fixed

- The lightbox prev/next row no longer shows when there is
  nothing to navigate (issue #67): it appeared under the
  icon chip's note text and beside single-image galleries,
  because the author rule `.lbn{display:flex}` overrode the
  UA `[hidden]{display:none}` so the JS toggle did nothing.
  An explicit `.lbn[hidden]` rule now backs the toggle, and
  `show()` hides the row whenever the open gallery holds
  fewer than 2 images; the i / n counter is unchanged.

## [0.3.35] - 2026-11-14

### Added

- Clicking the dark icon chip (the trace a non-embeddable
  image leaves behind) now opens a small note that says why
  the image is missing: its source file may have been
  removed from disk, exceeded the 4 MB embed cap, or uses an
  unsupported format (issue #66). The chip returns to being
  a real button with that reason in `data-note` (same
  styling, same synced pulse); the note branch clears any
  previous gallery selection and hides the image row, so it
  can never flash a stale image (the #65 guarantee holds).
  On a gallery-free page the overlay is now injected too,
  since the note alone is worth opening.

## [0.3.34] - 2026-11-14

### Fixed

- Clicking the dark icon chip (the trace a non-embeddable
  image leaves behind) no longer opens the lightbox showing
  a stale image from an earlier gallery on the page (issue
  #65). The click handler un-hid the overlay before it
  noticed the chip carries no `data-g`, so the overlay
  appeared with the previous `img.src` still in place. The
  chip now renders as a non-interactive span (same styling,
  same synced pulse) and the handler bails out before
  un-hiding when a chip has no gallery; numbered thumbnails
  still open the lightbox at the exact clicked index.

## [0.3.33] - 2026-11-14

### Added

- The transcript export now shows pasted images as blinking
  thumbnails instead of the plain `image` text chip (issue #64).
  Every embeddable image renders inline as a small thumbnail with
  a numbered badge (1, 2, 3, ...), and all thumbnails pulse in
  sync (sinusoidal opacity plus a slight scale) on a ~45 ms JS
  `setInterval` tick. The pulse is plain JS, not a CSS keyframe
  animation, so macOS "reduce motion" settings cannot silence it.
  Clicking a thumbnail opens the lightbox at that exact image via
  a per-thumbnail index.
- An image that could not be embedded (missing file or above the
  size cap) renders as a solid dark photo icon chip, dropping the
  old emoji-plus-word text trace that was easy to scan past.

## [0.3.32] - 2026-11-14

### Added

- `cc-continuity completion zsh|bash` prints a shell completion
  script for the whole command tree (issue #50). The script is
  generated from the very argparse tree `build_parser` produces, so
  a subcommand added upstream shows up on the next Tab for free;
  level-1 subcommands and their action subcommands are offered.
  Install with one eval line in `~/.zshrc`:
  `eval "$(cc-continuity completion zsh)"`. An unsupported shell
  exits 1 with a clear stderr line naming the supported ones
  instead of printing a half-script, and the output is
  deterministic run to run so the generated text can be diffed.

## [0.3.31] - 2026-11-12

### Added

- `cc-continuity configure --show` prints the effective config the
  way the code sees it (issue #49): one sorted `key value` line per
  setting, defaults filled in where `config.json` omits them, and the
  installer stamp block left out as noise. `--json` prints a single
  sorted JSON object for scripts. A missing config file is not a dead
  end: the defaults-only view prints with a one-line note on stderr
  instead of failing. The command never writes anything, so "is
  turn_clock on?" no longer needs `cat config.json` and knowing which
  keys the code reads.

## [0.3.30] - 2026-11-12

### Fixed

- An out-of-band `cc-continuity signal needs-human` now reaches the
  animated pane while the session idles (issue #63). In progress
  mode 1 the animator owns the blink (the waiting-blink poller is
  deliberately not spawned), but the animator's ~1 s tick only
  consulted the rendered status-line text, the reply flag and the
  pane capture; it never read `ledger.blocked_reason`. The push
  reached the phone, yet the tab never blinked and the Dock never
  bounced during the wait, because status-line renders only happen
  at tool boundaries. The tick now polls the ledger the way the
  reply flag does, painting the blocked chip with a reason-carrying
  gate key; the pane resolves its session directory with the same
  lookup `signal` writes to (writer and reader can no longer
  disagree), and an unresolvable directory keeps the pre-fix
  behaviour for off-project panes.

## [0.3.29] - 2026-11-12

### Fixed

- The tab blink and the Dock bounce survive a backgrounded iTerm2
  (issue #62). Two blind spots had been killing selection prompts
  (AskUserQuestion) while the terminal app was in the background.
  The focus probe asked iTerm2 for its current-session tty
  unconditionally, and iTerm2 answers that query even while
  backgrounded, so the watcher kept believing the user was on the
  tab and suppressed the wait seconds after it armed (live incident:
  bounce armed at 14:47:09, cancelled at 14:47:20 while the user sat
  in another app). The probe now checks which app is actually
  frontmost first (the issue #61 probe); any other app means no
  reader is present, and an unreadable app name keeps the old
  no-info degrade.
- A live selection prompt no longer vanishes from the pane region
  when the input box holds a draft (issue #62). CC closes the option
  box with a border line before the footer, and the old last-option-
  cursor anchor landed on the draft instead, so the prompt never
  armed the blink at all. The region now anchors on the live footer
  and scans upward to the option cursor, crossing at most the one
  closing border; footers quoted in scrollback keep the earlier
  stranding protection.

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
