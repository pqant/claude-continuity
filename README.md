# cc-continuity

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

## How it works

The whole flow in one picture: the automatic session loop, the optional
watchdog, and the opt-in notifications:

<img src="docs/diagrams/flow.light.anime.en.gif#gh-light-mode-only" width="800" alt="cc-continuity flow">
<img src="docs/diagrams/flow.dark.anime.en.gif#gh-dark-mode-only" width="800" alt="cc-continuity flow">

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

From PyPI:

```bash
pip install cc-continuity
cc-continuity install
exec zsh
cc-continuity doctor
```

On Windows (WSL) the same steps run inside the distro (bash as the
login shell); the full WSL guide is [`docs/WINDOWS.md`](docs/WINDOWS.md).

The installer touches (all as identifiable managed blocks, all reversible
via `cc-continuity uninstall`): hooks + status line in
`~/.claude/settings.json`, a managed block in `~/.claude/CLAUDE.md`, the
`claude()` wrapper in `~/.zshrc`, six `/continuity-*` skills, the
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
