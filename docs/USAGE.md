# cc-continuity — Usage

User-facing usage guide for an installed cc-continuity. For the concept,
the notification/privacy model, and the unattended-mode warning, see
`README.md`; for known boundaries, see `LIMITATIONS.md`; for the
watchdog daemon, see `docs/WATCHDOG.md`.

## 1. Install (one-time)

From Git:

```bash
cd <claude-continuity checkout>
python3 -m cc_continuity.cli install --window 262144
exec zsh                                    # reload the shell (claude() wrapper)
cc-continuity doctor                        # everything should be green
```

From PyPI:

```bash
pip install cc-continuity
cc-continuity install
exec zsh
cc-continuity doctor
```

Options:

- `--window <tokens>` — context window size (default 262144).
- `--ntfy <url>` — enable ntfy pushes at install time (`--ntfy off` to
  disable; omitted on a TTY = interactive prompt). Can be changed later
  with `cc-continuity configure --ntfy <url>`.

What the installer touches (all as managed blocks, all reversible via
`uninstall`): hooks + statusline in `~/.claude/settings.json`, a managed
block in `~/.claude/CLAUDE.md`, the `claude()` wrapper in `~/.zshrc`, the
six `continuity-*` skills, the `cc-continuity` launcher on `PATH`, and the
app copy in `~/.claude-continuity/app/`.

> **Updating:** from PyPI — `pip install --upgrade cc-continuity`, then
> re-run `cc-continuity install` (the binary on PATH is the install
> source; a `cc-continuity install` run from the already-installed app
> copy is refused, because copying it onto itself would delete the live
> app). From Git — `git pull` in the checkout, then re-run the install
> command from there. If the watchdog daemon is installed, force-restart
> it afterwards (Section 4, `kickstart -k`).

## 2. Daily use

```bash
cd ~/anything
claude                                      # auto-tmux, named <project>-001
```

- The zsh wrapper decides per launch: pass-through for `-p`/`--version`/
  subcommands, or a new tmux session `cc-<slug>-<rand4>` running
  `claude -n <slug>-NNN` (no `-n` added if you pass your own
  `-n`/`--name`/`-r`/`--resume`/`-c`/`--continue`).
- Watch the status line: the context ratio climbs (default thresholds:
  **55% warn → 62% rotate → 72% hard ceiling**).
- Rotation is zero-touch: the agent finishes the current step → writes a
  validated handoff → snapshots the git tree (non-invasive
  `refs/continuity/*`) → `/clear` → renames the session → a continuation
  prompt is injected. The successor's first line is
  `Continuing from cp-<NNN> (<slug>-<NNN+1>).`
- **After a crash / sleep / reboot:** just run `claude` in the project.
  Claude is told a pending checkpoint exists; say "continue" (or run
  `/continuity-status`).
- Watch long runs from your phone: ntfy lifecycle pushes (event table and
  privacy notes in `README.md`).

Tuning: edit `~/.claude-continuity/config.json` (Section 6).

## 3. In-session commands

| Command | Effect |
|---|---|
| `/continuity-status` | context ratio, session state, next session name |
| `/continuity-checkpoint` | write + finalize a checkpoint now, keep going |
| `/continuity-rotate` | rotate at the next boundary |
| `/continuity-off` / `/continuity-on` | disable / re-enable for this session |
| `/continuity-history` | the session chain (last 20) |

The agent also signals you at blocked points (destructive/irreversible
operation, ambiguous requirement, same fix failing 3×):

```bash
cc-continuity signal needs-human "<short reason>"   # urgent push, ⛔ BLOCKED
cc-continuity signal done "<one-line summary>"      # completion push
```

Remote in to the pane and reply; the block clears on your next prompt
(details in `README.md`).

## 4. CLI reference

### Sessions

```bash
cc-continuity status [--json]        # current project/session summary
cc-continuity history [-n N]         # session chain (default 20)
cc-continuity off                    # disable cc-continuity for this session
cc-continuity on                     # re-enable
cc-continuity signal needs-human "<reason>" | done "<summary>"
```

### Checkpoint

```bash
cc-continuity checkpoint             # build + finalize a checkpoint now
cc-continuity request-rotation       # move the current session to ROTATION_PENDING
cc-continuity validate-draft <file>  # validate a handoff draft against the template
cc-continuity validate --checkpoint <dir>
cc-continuity rotate --session <sid> --pane <pane> --rotation-id <id>
                                     # rotator entry — called by the Stop hook, not manual
```

If the running conversation has no record yet (e.g. its process started
before the Phase 27 resume registration), `checkpoint` /
`request-rotation` register it first (counter name, ledger
`source=manual`) and then proceed (Phase 28).

### Install and configuration

```bash
cc-continuity install [--window N] [--ntfy URL|off]
cc-continuity uninstall [--purge]     # --purge also deletes ~/.claude-continuity
cc-continuity configure [--ntfy URL|off] [--unattended]
                       [--watchdog-health URL|off] [--watchdog-enabled true|false]
cc-continuity doctor [--json]         # health checks
```

### Watchdog (details: `docs/WATCHDOG.md`)

```bash
cc-continuity watchdog install | uninstall | start | stop | status | run
cc-continuity watchdog digest run | install | uninstall | start | stop | status
```

- `run` / `digest run` execute the loop in the foreground (debugging).
- `watchdog start` on an already-running daemon is a **no-op**. To force a
  restart (e.g. to load newly deployed code):
  `launchctl kickstart -k gui/$(id -u)/com.claude-continuity.watchdog`
  (digest agent: `com.claude-continuity.watchdog-digest`).

### Pane and hardware

```bash
cc-continuity progress-pane [window_id]   # mode-1 20 fps progress line (pane animator)
cc-continuity power-plug state | on | off | cycle | serve
cc-continuity power-plug install | uninstall | start | stop | status
```

`power-plug` drives a TP-Link Kasa HS100 relay (Phase 13 box
auto-recovery); `serve` runs the resident localhost HTTP endpoint the
watchdog calls.

### Launch

```bash
cc-continuity launch [--unattended] [claude_args...]
```

Backend of the `claude()` wrapper — decides tmux / direct / pass-through.
`claude --unattended` (via the wrapper) or
`cc-continuity configure --unattended` (project-wide) spawn sessions with
`claude --dangerously-skip-permissions` — read the loud warning in
`README.md` first.

### Internals (wired by the installer — do not run by hand)

```bash
cc-continuity statusline             # status line entry
cc-continuity hook <event>           # hook entry
```

## 5. Environment variables

| Variable | Effect |
|---|---|
| `CC_CONTINUITY=0` | disable cc-continuity for one launch |
| `CC_CONTINUITY_NO_TMUX=1` | skip tmux for one launch (assisted mode) |
| `CC_CONTINUITY_NAME=<name>` | record/session name for one launch |
| `CC_CONTINUITY_FORCE_INTERACTIVE=1` | force the interactive launch decision |
| `CC_CONTINUITY_HOME=<dir>` | relocate all state (used by tests) |
| `CC_CONTINUITY_TMUX_BIN` / `CC_CONTINUITY_CLAUDE_BIN` | binary overrides |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | default context window when none is configured |

## 6. Configuration

Per-project opt-out: `.claude/continuity.json` with `{"enabled": false}`.

Global config `~/.claude-continuity/config.json` — main keys (defaults):

| Key | Default | Meaning |
|---|---|---|
| `window_tokens` | 262144 | context window size |
| `warn_ratio` / `rotate_ratio` / `hard_ratio` | 0.55 / 0.62 / 0.72 | threshold ladder |
| `full_auto` | true | zero-touch rotation on |
| `git_snapshot` / `snapshot_retention` | true / 20 | tree snapshots in `refs/continuity/*` |
| `notify_enabled` / `notify_endpoint` | false / "" | ntfy pushes |
| `unattended` | false | project-wide `--dangerously-skip-permissions` |
| `watchdog_enabled` | false | liveness daemon on/off |
| `watchdog_health_url` | "" | model-server health probe ("" = liveness-only) |
| `watchdog_interval_s` / `watchdog_max_nudges` | 300 / 3 | tick period / nudge cap |
| `watchdog_nudge_text` | "Continue." | first line of the watchdog nudge (your override wins) |
| `watchdog_network_recovery_cmd` | "" | shell command run on a failed health probe (opt-in, your rights) |
| `power_plug_ip` / `power_plug_port` | "" / 9999 | smart-plug LAN endpoint for auto power-cycle (opt-in) |
| `redact_patterns_extra` | [] | extra secret-redaction regexes appended to the generic defaults |

Full key list: `config.example.json`.

## 7. On-disk layout

```
~/.claude-continuity/
├── config.json               # global config
├── projects/<key>/
│   ├── project.json
│   └── sessions/<nnn>/
│       ├── state.json        # state machine
│       ├── ledger.jsonl      # append-only event log (the durable record)
│       └── checkpoint/       # handoff.md + git snapshot ref
├── app/                      # installed package (launcher code)
├── logs/                     # watchdog.log (JSON per tick), events.log, digest
└── trash/<date>/             # dated trash of removed records
```

## 8. Operations and recovery

- `cc-continuity doctor` — health checks; re-run after Claude Code updates.
- **Updating the code:** install from the dev tree (Section 1), then force-
  restart the daemon if the watchdog is installed (Section 4, `kickstart -k`).
- Watchdog activity: `tail -f ~/.claude-continuity/logs/watchdog.log` — one
  JSON line per tick: `health`, per-session liveness outcome
  (`ALIVE`/`FROZEN`/`SKIP`/`UNRESOLVABLE`/...), and events.
- The on-disk ledger is the durable record; ntfy pushes are best-effort
  (no retry queue) — `LIMITATIONS.md`.
- After a crash/reboot: run `claude` in the project and say "continue"
  (Section 2).

## 9. Out of scope

- Not supported: `-p`/print mode, the Agent SDK, `--bg`, the desktop app,
  web sessions.
- Known limitations (launch name-prediction race, native auto-compact
  deliberately off, local inference-box stalls, degraded checkpoints,
  best-effort pushes): `LIMITATIONS.md`.
