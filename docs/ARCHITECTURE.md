# Architecture

cc-continuity is a public, open-source companion for Claude Code. This document
explains how the pieces fit together and where each one lives in the code. It is
written for a reader who wants to understand the system, not to configure it
(that is `docs/USAGE.md`).

## 1. Overview

**The problem.** A long Claude Code session eventually runs out of its context
window: the transcript grows until the model can no longer hold the whole
conversation, and the work in flight is lost. A human workaround is to stop,
summarize, and start a fresh session — but that is manual, error-prone, and easy
to forget at the worst moment.

**The approach.** cc-continuity automates that reset as a zero-touch **rotation**.
As a session's context fills, it writes a validated handoff (a "checkpoint"),
clears the current conversation, and boots a successor session that resumes from
the handoff — without the user doing anything. A second, independent subsystem
(the **watchdog**) watches the model server itself and revives a session whose
turn died on a server outage.

**The four moving parts.**

1. **Hooks** — small commands Claude Code runs at lifecycle events (session
   start, tool use, turn end, …). They are the sensors: they read context usage,
   detect thresholds, and record what happened.
2. **The ledger and state machine** — the durable record. Each session keeps an
   append-only event log and a state document; everything else reasons from these.
3. **The rotator** — the actor. When rotation is due it clears the session and
   launches the successor; the successor's **bootstrap** claims the rotation and
   feeds it the handoff.
4. **The watchdog daemon** — a separate background process that probes the model
   server and revives stuck sessions.

**Where state lives.** All of it lives under one root, `~/.claude-continuity/`
(override with `CC_CONTINUITY_HOME`):

```
~/.claude-continuity/
├── config.json                 # merged configuration
├── app/                        # the installed program (launcher + package)
├── runtime/                    # short-lived markers (…/session_id.started)
├── logs/                       # rotator.log, watchdog.log, errors.log
└── projects/<slug>-<hash>/
    ├── project.json            # project record + session counter
    ├── lock                    # the project lock (fcntl flock)
    └── sessions/<NNN>/         # one directory per session
        ├── state.json          # the session's state machine
        ├── ledger.jsonl        # the append-only event log
        ├── handoff.draft.md    # the model's handoff draft (if any)
        └── checkpoint/         # the finalized, verified checkpoint
```

The layout, and the rule that one root holds everything, is in `cc_continuity/paths.py`.

## 2. The ledger and state machine

**The ledger** (`cc_continuity/ledger.py`) is an append-only JSONL file per
session: `sessions/<NNN>/ledger.jsonl`. Hooks fire concurrently, so every append
is a single `os.write` on an `O_APPEND` descriptor — POSIX guarantees the
offset bump and write of one call are atomic, so parallel hooks never
interleave or tear a line. Each entry is given an epoch `ts` if the caller does
not supply one. A corrupted or missing ledger is always read as "empty /
unblocked", never an exception — the record is evidence, not a control plane.

**The state machine** (`cc_continuity/state.py`) is one validated
`transition()` for everything. A session's state lives in `sessions/<NNN>/state.json`.
Every transition takes the **project lock**, validates the move against a
fixed transition table, writes atomically, and appends a `state` event to the
ledger. A transition to the current state is an idempotent no-op, so repeated
hooks are harmless. `ROTATED` is terminal: a session that has been succeeded is
closed.

The states, in the order a rotation walks them:

```
BOOTSTRAPPING → ACTIVE → (WARNED) → ROTATION_PENDING → HANDOFF_REQUESTED
              → CHECKPOINT_WRITING → CHECKPOINT_VERIFIED → ROTATING → ROTATED
```

- `BOOTSTRAPPING` is a fresh session before it is confirmed `ACTIVE`.
- `WARNED` marks that the warn threshold was crossed (informational).
- `HANDOFF_FAILED` is a detour: after three failed handoff drafts the machine
  drops to a **degraded checkpoint** (deterministic facts only) and still
  reaches `CHECKPOINT_VERIFIED`.
- A rotation that aborts (the session is still busy) rolls `ROTATING` back to
  `CHECKPOINT_VERIFIED`; the next turn end retries.

**The project record** (`cc_continuity/project.py`) auto-registers on first
contact — there is no `init` command. The project is keyed
`<slug>-<sha8(absolute root)>` so two different checkouts do not collide. The
monotonic session counter lives in `project.json` and moves only under the lock;
it never goes backwards and is never reused, even after a failed rotation.

**Concurrency.** Two primitives, in `cc_continuity/lock.py` and
`cc_continuity/atomic.py`:

- A per-project **fcntl `flock`** (`lock.py`). It is advisory and non-reentrant:
  a code path that already holds it must not wrap a helper that takes it again.
  The rotator has a separate, stale-takeover **rotation lock** so a crashed
  rotator cannot wedge the project.
- **Atomic writes** (`atomic.py`): write to a temp file, `fsync`, then
  `os.replace` over the target. A crash mid-write leaves either the old file or
  the new one, never a half-written one. This is how `state.json`,
  `project.json`, and the finalized checkpoint survive power loss.

## 3. Rotation (the rotator + bootstrap)

Rotation is the reset. It has a detection side and an action side.

**Detection.** `cc_continuity/thresholds.py` reads the session's context usage
(the ratio against the configured window) on the relevant hooks. A rising ratio
crosses three configured thresholds — warn, rotate, hard. Crossing the rotate
threshold moves the session to `ROTATION_PENDING`.

**The handoff request.** At turn end the **Stop** hook (`cc_continuity/hooks/stop.py`)
asks the model to write a handoff draft and, when the threshold says so, spawns
the rotator. The draft lands in `handoff.draft.md`.

**The detached rotator** (`cc_continuity/rotator.py`). Spawned detached (double
`setsid`) so it outlives the hook. Its sequence, in order:

1. Take the project **rotation lock** (stale takeover).
2. Re-read state — it must be `ROTATING` with the matching `rotation_id`.
3. **Idle check**: the turn is inactive and there is no new main-session ledger
   activity since the last stop (subagent work does not count as activity). If
   not idle, roll back to `CHECKPOINT_VERIFIED` and let the next turn end retry.
4. **Input-box protection**: capture the pane, save any in-progress prompt, then
   send `/clear` only once the input box is verified settled-empty. This is the
   guard against clobbering a message the user is still typing.
5. Wait for the successor's `.started` marker, then rename the pane to the
   successor's name.
6. Paste the **continuation prompt** (the bootstrap text) into the new session,
   carrying the saved in-progress prompt as a reference-only note.
7. **Verify** — a `CONTINUITY RESUME <cp>` prompt must land in the successor's
   ledger within the verify window. On success the session is `ROTATED`; the
   steps retry once, then fail soft (stay claimable, notify).

The rotator never kills Claude Code and never deletes checkpoints.

**The bootstrap / claim** (`cc_continuity/bootstrap.py`). In the fresh session,
`SessionStart(clear)` runs `try_claim`: it finds the latest **claimable** session
(`ROTATING`, `AWAITING_MANUAL_CLEAR`, or `CHECKPOINT_VERIFIED`, with no successor
yet), creates the successor's state (`BOOTSTRAPPING`, predecessor = old name),
marks the old session claimed and moves it to `ROTATED`, consumes the matching
checkpoint, validates it, and returns the bootstrap text to inject. The claim is
idempotent — a second claim sees `successor` already set and does nothing.

**Prompt transfer across the rotation window.** Some user prompts can be typed
*after* the checkpoint snapshot froze but *before* the successor starts. Those
would be lost, so the bootstrap re-reads the old session's ledger for
`user_prompt` entries newer than the snapshot and hands them to the successor as
`new_work` (deduplicated against what the handoff already captured).

## 4. The checkpoint

The checkpoint is the durable artifact a successor resumes from. It is built by
`cc_continuity/checkpoint.py` and checked by `cc_continuity/validate.py`.

**What it contains** (`checkpoint/`):

- `snapshot.json` — the **deterministic** part, produced by code only: git facts
  (branch, HEAD, ahead/behind, `status --porcelain`, `diff --stat`), the git tree
  snapshot ref, and an aggregate of the session ledger (files touched, commands
  run, failed commands, tasks, the recent user prompts).
- `handoff.md` — the meta header + the model-written sections (or a degraded
  header) + an auto-generated appendix of the deterministic state.
- `state.json`, `ledger.jsonl` — copies of the session's state and ledger at the
  moment of the checkpoint.
- `SHA256SUMS` — checksums of the four content files.

**Finalization is atomic.** `finalize()` runs the whole build under the project
lock: it walks the state machine to `CHECKPOINT_WRITING`, writes the files into a
`checkpoint.tmp-<pid>/` directory with per-file `fsync`, computes `SHA256SUMS`,
**re-reads and verifies** (checksums, JSON parses, heading order, project and
session identity), and only then `os.replace`s the tmp dir to `checkpoint/`. Only
after that does the session move to `CHECKPOINT_VERIFIED` and
`project.json`'s `latest_checkpoint` pointer get set. A crash at any point leaves
either the previous valid checkpoint or a stale tmp dir — never a half-verified
checkpoint in a verified state.

**Degraded checkpoint.** If the model never produced a valid handoff draft (or
after three failed attempts), the checkpoint is still built from the
deterministic parts only, with a header telling the successor to reconstruct
intent from the user prompts and changed files rather than trusting a narrative.

**Validation in the new session** (`cc_continuity/validate.py`). The bootstrap
compares the checkpoint against the live project: project path, branch, HEAD
(same / fast-forwarded / rewound / diverged), and the working-tree tree hash vs
the snapshot tree. A tree difference is a real **MISMATCH** only when HEAD did not
fast-forward (after an advance, changed files are the expected trace of the moved
work). It also checks that files named in the handoff's *Next Atomic Action* exist
(tokens the action is about to *create* are exempt). The result is a single line:
`VALIDATION: OK` or `VALIDATION: MISMATCH` plus details.

**The git tree snapshot** (`cc_continuity/gitsnap.py`) records the working tree
as a git tree object under `refs/continuity/*` — it does **not** touch the user's
branches or history, and prunes old snapshots to a retention count.

## 5. The watchdog daemon

The watchdog is a separate long-running process that protects sessions from the
model server failing underneath them. It has a strict two-layer split, and that
split is the design rule of the whole subsystem:

- **`cc_continuity/watchdog.py` is a pure decision core — zero I/O.** It
  classifies liveness, debounces health, and decides nudge policy from inputs
  alone. It never touches curl, tmux, the ledger, or the store.
- **`cc_continuity/watchdogd.py` owns every side effect.** It is the I/O loop
  (`cc-continuity watchdog run`, driven by the launchd agent). It feeds the core
  once per tick and acts on the core's answers. The hard rule: **the watchdog must
  never break the continuity loop** — every I/O call is wrapped, and a tick
  exception becomes one line in `errors.log` and the loop continues.

**The tick loop** (`watchdogd.run_forever`) runs one loop with two cadences:
a **5-second fast pass** (the exit-hang killer and zombie-session cleanup) on
every iteration, and the **full tick** (probe + per-session scan) only when the
configured interval — 300 s by default — has elapsed.

What the full tick does, per live session:

- **Health probe + debounce** (`HealthState`): the server is `DOWN` only after
  two consecutive failed probes, so a single blip is not an outage. A
  failed probe may first run a configured **network-recovery** command and
  re-probe before counting the fail.
- **Liveness matrix** (`classify`): given the state record, the last ledger
  activity, and the current/previous pane capture, it returns one of
  `ALIVE`, `IDLE_WAITING`, `TURN_DEAD`, `STALLED`, `FROZEN`, or `SKIP`. A fresh
  ledger or a changing pane means `ALIVE`; a pane at the prompt with a mid-turn
  death is `TURN_DEAD`; an error at the tail is `STALLED`; two unchanged ticks
  with the input closed is `FROZEN`.
- **Nudge state machine** (`Episode` + `nudge_action`): a bounded set of nudges
  (default 3). A nudge is a model call, so it is only sent while health is `UP`.
  Verification is ledger-based — a `user_prompt` newer than the last nudge proves
  it landed. If the nudge text is sitting unsubmitted in the input box, the
  daemon sends a bare `Enter` instead of re-typing it. Giving up after the cap
  fires a `needs-human` push.
- **Outage recovery**: on a `DOWN → UP` transition, every eligible session still
  mid-turn is nudged, because its turn died with the model.
- **Frozen-turn escalation**: a pane frozen with a turn in flight is escalated
  once it outlives the configured minutes — an `ESC` force-cancel, a
  frozen-form nudge, and a **generation probe** (a real 5-token completion, which
  catches a hung model server that still answers `/health` with 200).
- **Auto power-cycle**: on sustained `DOWN` past a threshold, with a configured
  smart-plug endpoint and a clear cooldown window, the daemon power-cycles the
  box. A failed attempt does not consume the cooldown.
- **Exit-hang killer** (fast pass): if a live Claude process has hung on `/exit`
  past the grace (the marker is in the transcript, no real activity after it,
  the process is still alive and is this session's own), it is killed, and the
  now-orphaned tmux session is cleaned up.
- **Zombie-session cleanup** (fast pass): an `off` session whose tmux session is
  still alive on an orphaned pane (no Claude process in it, not shared with a
  live record, past a short grace) is reaped.

The watchdog pushes lifecycle events (server down/up, power cycle, turn dead,
frozen, recovered, exit-hang, zombie cleaned) through the same ntfy path as the
rotation, and a **digest relay** (`cc_continuity/watchdog_digest.py`) pipes the
watchdog log into the most recently active session so the recovery is visible in
the conversation.

## 6. Configuration

`cc_continuity/config.py` layers three sources, later wins:

1. **Built-in defaults** — the `DEFAULTS` table.
2. **Global config** — `~/.claude-continuity/config.json`.
3. **Project override** — `.claude/continuity.json`, searched from the working
   directory up to the git root (outside a repo, only the cwd itself).

Two cross-cutting rules:

- **`CC_CONTINUITY=0`** in the environment disables everything, regardless of
  files.
- The **window size** (`window_tokens`) falls back to
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` and then to a built-in default when no config
  file sets it.

`config.example.json` in the repository root lists every key with its default
(or an illustrative placeholder for the two server-specific ones); key semantics
live in `docs/USAGE.md`.

## 7. Installation and the trust surface

The installer (`cc_continuity/install.py`) is the only code allowed to touch the
user's `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, and `~/.zshrc`.

**What it installs:**

- Copies the program into `~/.claude-continuity/app/` and symlinks
  `~/.local/bin/cc-continuity` to the app's **launcher stub**. The stub's shebang
  pins the *absolute* Python the install ran under (a portable `env python3`
  broke the launchd agent, whose minimal `PATH` resolves to the system Python).
- **Merges** (never replaces) `~/.claude/settings.json`: it backs up the file
  with a timestamp first, then adds its hook entries, the status-line command
  (chaining any existing status line), the context-window env var, and a small
  set of permission allow-rules for the `~/.claude-continuity` store.
  `cc_continuity/settings_merge.py` is the pure merge/unmerge; it records the
  file's JSON formatting so uninstall can re-dump byte-for-byte.
- Inserts a **managed block** into `~/.claude/CLAUDE.md` (the continuity
  instructions the model follows) and into `~/.zshrc` (the `claude()` wrapper).
  `cc_continuity/managed.py` implements the marker-delimited upsert/remove with
  the round-trip invariant that removing a block you just inserted returns the
  original text.
- Installs the user **skills** and writes `config.json` (defaults + the install
  manifest).

**The trust surface.** Everything the installer changes is identifiable (a
`cc-continuity` substring or a marker block) and reversible. **Uninstall is the
manifest-driven inverse of install**: it removes exactly the entries and blocks
it added, restores the previous status line and env, and restores the user's
original files **byte-for-byte**. `projects/` and `logs/` survive uninstall
unless `--purge` is given. `cc-continuity doctor` re-verifies the install
(launcher path, store permissions, etc.).

**The launchd agent** (opt-in, watchdog) is rendered by `watchdogd.plist_render`
and installed with `launchctl bootstrap` as a `KeepAlive` LaunchAgent that runs
`cc-continuity watchdog run`. Its label, plist path, and the
install/uninstall/kickstart/status helpers all live in `watchdogd.py`.

**The launcher / wrapper.** `cc_continuity/launch.py` backs the zsh `claude()`
wrapper. It decides, purely and exhaustively, whether a `claude <args>` call
should be passed straight through (disabled, non-interactive, print mode, a
known subcommand or info flag, or a user-supplied name/resume flag) or launched
in a new tmux session with an auto-predicted logical name. The *authoritative*
name assignment happens later, under the lock, at `SessionStart`.

## 8. Module map

The package is `cc_continuity/`. Core state and coordination:

| Module | Responsibility |
|---|---|
| `paths.py` | The state-store layout; all roots derive from here. |
| `lock.py` | The project `flock` and the stale-takeover rotation lock. |
| `atomic.py` | Atomic temp + fsync + `os.replace` writes, dir fsync. |
| `project.py` | Project auto-registration, key, monotonic session counter. |
| `state.py` | The per-session state machine (one validated `transition`). |
| `ledger.py` | The append-only per-session JSONL event log. |
| `naming.py` | Logical session names and the counter padding. |

Rotation and checkpoints:

| Module | Responsibility |
|---|---|
| `thresholds.py` | Context-ratio threshold detection and prompt builders. |
| `hooks/` | The Claude Code hook handlers (see below). |
| `rotator.py` | The detached full-auto rotator (clear, rename, inject, verify). |
| `bootstrap.py` | Successor claim, registration, prompt transfer. |
| `checkpoint.py` | The checkpoint engine: snapshot, render, finalize, verify. |
| `validate.py` | Bootstrap-time checkpoint vs live-project validation. |
| `gitsnap.py` | Non-invasive git tree snapshots under `refs/continuity/*`. |

Notification, redaction, and display:

| Module | Responsibility |
|---|---|
| `notify.py` | The ntfy event sender (bodies, dedup, redaction). |
| `redact.py` | Secret redaction over the configured patterns. |
| `statusline.py` | The status-line context segment. |
| `progress_pane.py` | The progress-pane display modes. |

Watchdog:

| Module | Responsibility |
|---|---|
| `watchdog.py` | **Pure decision core** — zero I/O (liveness, debounce, nudge policy). |
| `watchdogd.py` | The daemon I/O loop, probes, kill/cleanup, launchd plist. |
| `watchdog_digest.py` | The watchdog-log digest relay into the active session. |
| `power_plug.py` | The localhost smart-plug endpoint client. |

Installation and CLI:

| Module | Responsibility |
|---|---|
| `install.py` | Installer / uninstaller (the only module that edits user files). |
| `settings_merge.py` | Pure `settings.json` merge/unmerge + format detection. |
| `managed.py` | Managed marker blocks for `CLAUDE.md` and `.zshrc`. |
| `skills.py` | Install/uninstall of the bundled user skills. |
| `launch.py` | The `claude()` wrapper launch plan and execution. |
| `config.py` | Layered config: defaults ← global ← project. |
| `doctor.py` | Install health checks. |
| `cli.py` / `__main__.py` | The `cc-continuity` command-line entry point. |

The `hooks/` handlers, one per Claude Code event, are the thin entry points that
read the event, update state/ledger, and (for Stop) drive rotation:
`session_start.py`, `user_prompt.py`, `pre_tool_use.py`, `tool_use.py`,
`pre_compact.py`, `stop.py`, `task.py`, `session_end.py`, plus shared helpers in
`common.py`.

## 9. Platform seam

v1 targets **macOS**. The only OS-bound surface is the **launchd** integration —
the watchdog LaunchAgent's plist rendering, path, and the
`launchctl bootstrap / bootout / kickstart / print` calls — all isolated in
`watchdogd.py` behind small functions. Everything else (state store, locks,
atomic writes, hooks, rotator, checkpoint, the pure watchdog core) is
platform-neutral POSIX/Python. Porting to **Linux/systemd** means adding a
systemd unit file and a `launchd`-shaped interface around the daemon; the core
would be unchanged.
