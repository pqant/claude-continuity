# cc-continuity

[![Forks](https://img.shields.io/github/forks/pqant/claude-continuity?label=FORKS&logo=github&labelColor=%23333333&color=%23ff8c00)](https://github.com/pqant/claude-continuity/forks)
[![Stars](https://img.shields.io/github/stars/pqant/claude-continuity?label=STARS&logo=github&labelColor=%23333333&color=%234c1)](https://github.com/pqant/claude-continuity/stargazers)
[![Issues](https://img.shields.io/github/issues/pqant/claude-continuity?label=ISSUES&logo=github&labelColor=%23333333&color=%23007ec6)](https://github.com/pqant/claude-continuity/issues)
[![Last commit](https://img.shields.io/github/last-commit/pqant/claude-continuity?label=LAST%20COMMIT&logo=github&labelColor=%23333333&color=%23e0559a)](https://github.com/pqant/claude-continuity/commits/main)
[![Code of conduct](https://img.shields.io/badge/CODE%20OF%20CONDUCT-ENFORCED-%239146ff?logo=contributorcovenant&labelColor=%23333333)](CODE_OF_CONDUCT.md)
[![Contributions](https://img.shields.io/badge/CONTRIBUTIONS-WELCOME-%23e0559a?logo=git&labelColor=%23333333)](CONTRIBUTING.md)
[![Security](https://img.shields.io/badge/SECURITY-POLICY-%234c1?logo=github&labelColor=%23333333)](SECURITY.md)

[![PyPI](https://img.shields.io/pypi/v/cc-continuity?label=PYPI&logo=pypi&logoColor=white&labelColor=%23333333&color=%23007ec6)](https://pypi.org/project/cc-continuity/)
[![Downloads](https://img.shields.io/pypi/dm/cc-continuity?label=DOWNLOADS&logo=pypi&logoColor=white&labelColor=%23333333&color=%23007ec6)](https://pypi.org/project/cc-continuity/)
[![License](https://img.shields.io/github/license/pqant/claude-continuity?label=LICENSE&logo=github&labelColor=%23333333&color=%234c1)](LICENSE)
[![CI](https://github.com/pqant/claude-continuity/actions/workflows/ci.yml/badge.svg)](https://github.com/pqant/claude-continuity/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/cc-continuity?label=PYTHON&logo=python&logoColor=white&labelColor=%23333333&color=%233776ab)](https://pypi.org/project/cc-continuity/)
[![Good first issues](https://img.shields.io/github/issues/pqant/claude-continuity/good%20first%20issue?label=GOOD%20FIRST%20ISSUES&logo=github&labelColor=%23333333&color=%237057ff)](https://github.com/pqant/claude-continuity/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)

Zero-touch session rotation and supervision for Claude Code.

**Why it exists.** Long sessions die when the context window fills up, and local LLMs make it worse: their window is fixed and small, and the built-in compaction (`/compact`, auto-compact) is a lossy one-shot summary. With a small model it silently drops decisions, rejected approaches and the exact stopping point, and every compaction then summarizes a summary, so the drift compounds. cc-continuity exists to give long work room to breathe inside the window: at a safe threshold it rotates the session with a structured, validated handoff instead of a lossy summary.

The goal is to let an agent drive a planned project end-to-end, from 0 to done, with a human involved only when the agent is truly blocked. This repository is the proof of concept: it was developed with cc-continuity itself, and the full development history is in this git log.

cc-continuity watches the context ratio and, when it crosses a threshold, rotates the session with no user action:

1. the agent finishes the current step,
2. cc-continuity makes it write a validated handoff,
3. the git tree is snapshotted non-invasively (`refs/continuity/*` refs:
   no commits, no tags, no branches touched),
4. the context is cleared and the session renamed,
5. a continuation prompt is injected and the successor session starts
   with `Continuing from cp-<NNN> (<slug>-<NNN+1>).`

An optional **watchdog** daemon keeps long unattended runs recoverable
when the model server (for example a local vLLM box) dies or freezes, and
optional **ntfy** pushes let you watch from your phone.

## What's new

The five most recent updates, newest first. Each line links to its
commit, so you can see what changed lately without digging through the
log:

- **2026-11-24**: `cc-continuity history` now searches the checkpoint archive. `cc-continuity history <query>` finds every handoff whose text contains the keyword (case-insensitive substring) and prints session number, creation date, the first objective line and the matched line; `--nnn` and `--since`/`--until` filter the result, and the `/continuity-history` skill teaches the same usage; the search is read-only and deterministic, the checkpoint write path and the session index are untouched, and eight tests were written first ([commit](https://github.com/pqant/claude-continuity/commit/be179fe)).
- **2026-11-24**: A rotation no longer splits the successor identity. The rotator predicted the next session name as strict `nnn+1` while the claim mints `max(counter, old_nnn) + 1`, so once stale records had advanced the counter the pane title and the old session's ledger froze a name the claim never minted (seen live: the ledger said connector-259 while the real successor session was connector-267); the prediction now mirrors the claim allocator, the completion paths reconcile the ledger entry and log line with the claimed record before writing them, and a claim whose own never-attached `BOOTSTRAPPING` successor already exists adopts it at the same index instead of minting a second name, with three tests written first to pin all three fixes ([commit](https://github.com/pqant/claude-continuity/commit/b60c518)).
- **2026-11-24**: A deferred rotation no longer buries the fresh-prompt recovery evidence. Recovery re-attaches a verified-checkpoint session only when the newest user prompt in the ledger post-dates the checkpoint verification stamp, but a deferred rotation used to append an out-of-band rollback (ROTATING -> CHECKPOINT_VERIFIED) that re-stamped the verification after the user came back: the prompt looked stale, the watchdog kept reporting the session as skipped and the context-400 rotation never dispatched; a rollback entering from ROTATING no longer counts as a new verification, the ledger stays the single source of truth, and a test written first pins that a deferred re-stamp neither buries the fresh prompt nor gates recovery out ([commit](https://github.com/pqant/claude-continuity/commit/203578b)).
- **2026-11-24**: Context-400 recovery now actually completes the rotation. The dispatch works since v0.3.62, but the spawned rotator kept deferring every time: the API 400 kills the turn without firing the Stop hook, so the turn flag was never cleared and the rotator's idle gate read the dead turn as live work, deferred and waited for a Stop that could never come; the recovery now records the finished turn itself (flag off, stop timestamp) right after logging the recovery, the rotator and its idle gate stay untouched, and two tests written first pin the field write and the unlocked idle gate ([commit](https://github.com/pqant/claude-continuity/commit/129422a)).
- **2026-11-23**: Sessions stuck in `CHECKPOINT_VERIFIED` are now actually dispatched to context-400 recovery. The recovery gate accepted `CHECKPOINT_VERIFIED` since v0.3.61, but the watchdog still only reached that gate through screen classification: records outside `ACTIVE`/`WARNED` are skipped as not live, and recovery ran only for sessions classified as alive, turn-dead, stalled or frozen, so a stalled verified checkpoint was never scanned and stayed parked on the oversized prompt; the tick loop now also runs recovery for a skipped verified-checkpoint session while its turn is still in flight, the recovery gate itself (full-auto, one attempt, transcript-tail scan, fresh-prompt check) stays the single source of truth, and three tick-loop integration tests written first pin the dispatch ([commit](https://github.com/pqant/claude-continuity/commit/ff58ebc)).
## How it works

The whole flow in one picture: the automatic session loop, the optional
watchdog, and the opt-in notifications:

<img src="docs/diagrams/flow.light.anime.en.gif#gh-light-mode-only" width="800" alt="cc-continuity flow">
<img src="docs/diagrams/flow.dark.anime.en.gif#gh-dark-mode-only" width="800" alt="cc-continuity flow">

The live look of the status line itself, every frame drawn by the real
renderer: the context-usage segment sweeping, then the rotation handoff
moments and the fresh successor session.

![Animated cc-continuity status line: the context-usage percentage segment with a shimmer sweep, then the rotation moments HANDOFF! and ROTATING as a fresh session takes over](docs/assets/cc40-statusline-rotation.gif)

## Demo

A full recorded run: `install` → five chained sessions → four context
rotations → a completed task, captured end to end. It is a long,
video-style GIF, so it is offered as a download rather than embedded
in the page:

- [demo-high-res.gif](https://github.com/pqant/claude-continuity/releases/download/v0.1.1/demo-high-res.gif) — 896x1920, 20 fps (~20 MB)
- [demo-low-res.gif](https://github.com/pqant/claude-continuity/releases/download/v0.1.1/demo-low-res.gif) — 700x1500, 12 fps (~12 MB)

The demo itself is recorded by
[`tools/demo_gif.sh`](tools/demo_gif.sh) (it needs a local-model
endpoint in `ANTHROPIC_*` env); the cut list and the pipeline that
turned the recording into the GIFs are in
[`tools/demo_segments.txt`](tools/demo_segments.txt) and
[`tools/gif_from_video.sh`](tools/gif_from_video.sh).

## Requirements

- macOS, or Windows 10/11 with WSL (see **Windows (via WSL)**)
- Python ≥ 3.11 (standard library only at runtime)
- tmux and curl on PATH; zsh on macOS (bash is fine on WSL)
- Claude Code ≥ 2.1.209

To verify your setup: `cc-continuity doctor` checks the Claude Code
version, and the WSL e2e script
([`tools/e2e_wsl.sh`](tools/e2e_wsl.sh)) exercises the rotation/hook
path end to end.

## Windows (via WSL)

cc-continuity runs inside a Windows Subsystem for Linux distro: the
Linux side is unchanged — same rotation, same watchdog, same ntfy
pushes — and only the daemon hosting differs. Where macOS uses
launchd, the distro uses a detached tmux session (default) or systemd
user units (WSL 2 with systemd enabled), plus one optional Windows
logon task so the daemons come back when the WSL VM restarts.

- **WSL 2 recommended; WSL 1 works too** (minus the systemd option).
- **Ubuntu 24.04 recommended**; Ubuntu 22.04 works with `python3.11`
  from deadsnakes.
- **Native Windows is out of scope** — the distro is the runtime.

Full guide (requirements, install, watchdog, autostart,
troubleshooting): [`docs/WINDOWS.md`](docs/WINDOWS.md). An end-to-end
test for the WSL install lives in [`tools/e2e_wsl.sh`](tools/e2e_wsl.sh).

## Quick start

From Git:

```bash
git clone https://github.com/pqant/claude-continuity
cd claude-continuity
python3 -m cc_continuity.cli install --window 262144
exec zsh                 # reload the shell (claude() wrapper)
cc-continuity doctor     # everything should be green
```

From PyPI (recommended: `pipx`, the standard way to install Python CLIs):

```bash
pipx install cc-continuity          # or: uv tool install cc-continuity
cc-continuity install
exec zsh
cc-continuity doctor
```

Plain `pip install cc-continuity` also works inside a venv or when
embedding it as a library; `pipx`/`uv tool` put the CLI on PATH without
touching your Python environment.

On Windows (WSL) the same steps run inside the distro (bash as the
login shell); the full WSL guide is [`docs/WINDOWS.md`](docs/WINDOWS.md).

The installer touches (all as identifiable managed blocks, all reversible
via `cc-continuity uninstall`): hooks + status line in
`~/.claude/settings.json`, a managed block in `~/.claude/CLAUDE.md`, the
`claude()` wrapper in `~/.zshrc`, seven `/continuity-*` skills, the
`cc-continuity` launcher on PATH, and the app copy in
`~/.claude-continuity/app/`. A timestamped backup of
`settings.json` is written before any merge.

## Daily use

```bash
cd ~/anything
claude                 # auto-tmux, session named <project>-001
```

Watch the status line: the context ratio climbs (default thresholds
**55% warn → 62% rotate → 72% hard ceiling**). Rotation is zero-touch
(quick-start list above). After a crash, sleep, or reboot: just run
`claude` in the project and say "continue".

Chaining a third-party status line such as [claude-hud](https://github.com/)?
cc-continuity registers `~/.claude-continuity` as an additional directory,
and claude-hud 0.15.4+ renders every additional directory inline after the
project name, so you see an extra `+.claude-continuity` segment. Hide it by
setting `{"display": {"showAddedDirs": false}}` in
`~/.claude/plugins/claude-hud/config.json`; `cc-continuity doctor` prints
that exact hint when it detects the combination (issue #85).

In-session commands: `/continuity-status`, `/continuity-checkpoint`,
`/continuity-rotate`, `/continuity-off` / `/continuity-on`,
`/continuity-history`. Full CLI reference: `docs/USAGE.md`.

## Configuration

Global: `~/.claude-continuity/config.json`, start from
`config.example.json` (all keys, placeholder values). Per-project
override: `.claude/continuity.json` (e.g. `{"enabled": false}` to opt one
project out). `CC_CONTINUITY=0` disables everything for one launch.

## Notifications (ntfy, opt-in)

cc-continuity can push lifecycle events to any ntfy-compatible server, so
you can watch long unattended runs from your phone:

```bash
cc-continuity configure --ntfy https://ntfy.sh/your-topic   # or at install: --ntfy <url>
cc-continuity configure --ntfy off
```

`cc-continuity doctor` sends a test message to verify the endpoint.

| Event | Title | Meaning |
|---|---|---|
| start | `▶ session started` | a session registered itself (first hook or resume claim) |
| warn | `⚠ context warning` | ratio crossed the warning threshold |
| pending | `◆ rotation pending` | ratio crossed the hard threshold; rotates at turn end |
| verified | `✓ handoff written & verified` | checkpoint finalized, sha256 verified |
| complete | `🔄 rotation complete` | the successor session is running |
| resumed | `↻ resumed` | successor consumed the checkpoint |
| blocked | `🛑 BLOCKED, waiting for you` | agent hit a needs-human point (urgent) |
| done | `✅ task complete` | agent finished the task you gave it |
| error | `✗ … failed` | a lifecycle step failed (urgent) |
| end | `■ session ended` | SessionEnd (rotation, /clear, or close) |

**Privacy:** a push is a title + a body of at most 200 characters
containing only the project slug, session number, context ratio, a short
reason/summary you control, and the hostname. No file contents, no
prompts, no secrets; recorded text is secret-redacted on the way out.
Messages only ever go to the endpoint YOU configure, and only when
`notify_enabled` is on.

## Unattended mode (opt-in)

> **⚠️ Loud warning:** `launch --unattended` (or
> `config.json: "unattended": true` for a whole project) spawns the
> session with `claude --dangerously-skip-permissions`: the agent edits
> files and runs commands **without per-action approval**. Use it only
> on machines/projects you trust with that. The `needs-human` mechanism
> (below) is a signal the agent sends, not a permission boundary: it
> enforces and blocks nothing. The default mode is unchanged; nothing
> skips permissions unless you opt in.

```bash
claude --unattended            # via the zsh wrapper (one launch)
cc-continuity launch --unattended
cc-continuity configure --unattended    # project setting (whole chain)
```

Because a rotation is a `/clear` inside the same process, one flag (or
one project setting) carries the entire rotation chain: every successor
session stays unattended.

## When it blocks on you

The agent signals you when it cannot safely continue (a
destructive/irreversible operation, an ambiguous requirement, the same
fix failing three times):

```bash
cc-continuity signal needs-human "<short reason>"
```

That pushes an **urgent** notification and marks the status line
`⛔ BLOCKED` (machine-side truth, visible on any later attach). Remote
in to the pane, reply in the session; the block clears on your next
prompt. On completion the agent runs:

```bash
cc-continuity signal done "<one-line summary>"
```

## Watchdog (opt-in)

A daemon that keeps long runs recoverable (launchd on macOS; a tmux
session or systemd user unit on WSL): a health probe for
your model server (2-fail debounce), per-session liveness classification,
a bounded nudge state machine (the session is told WHY its turn stopped),
an exit-hang killer, frozen-turn escalation, and optional auto
power-cycle of the box + a log digest relayed into the active session.
Off by default; the health URL is yours to supply. Details:
`docs/WATCHDOG.md`.

## Security

Read `SECURITY.md` before installing. It states the trust model: what
code runs with your rights, what the installer merges and how it
uninstalls, and what data ever leaves the machine.

## Limitations

See `LIMITATIONS.md` (an honest list).

## Development

```bash
python3 -m pytest -q     # unit + contract tests
```

Tests redirect all state through `CC_CONTINUITY_HOME` (a temp dir) and
use throwaway git repos; they never touch the real
`~/.claude-continuity` or `~/.claude`. Architecture:
`docs/ARCHITECTURE.md`.

## License

MIT, see `LICENSE`.
