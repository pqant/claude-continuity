# LLM Watchdog (Phase 10)

A watchdog daemon that keeps a watch over the local model server and the
live Claude Code sessions running on it. It nudges, it never restarts, and
it never touches the DGX box.

## What it is

A **global launchd daemon on the Mac** (one per machine, `KeepAlive`). Each
tick it:

1. probes the model server health endpoint (real `curl`, 5 s timeout);
2. classifies each live session's **turn liveness** from three real
   signals: the session ledger, the session state, and a
   `tmux capture-pane` read of the session's pane;
3. **auto-nudges dead turns** — `tmux send-keys "<nudge text>" Enter` is
   the ONLY pane write the daemon ever performs;
4. detects **retry-loop freeze** (a pane stuck unchanged) and reports it —
   no auto-fix;
5. pushes events to the phone (ntfy).

Recovery is a **prompt, never a restart**: the daemon never kills a
session, never rotates, never reboots anything. **No box-side action of any
kind** (P10-D1) — when the model server itself is unreachable, the daemon
may run a user-configured *local* network-recovery command on the Mac
(see below), but the DGX box is never acted upon.

## Install

```bash
# 1. opt in + point it at the model server (health URL is user-supplied;
#    the docs always show the placeholder, P10-D13):
cc-continuity configure --watchdog-enabled true \
    --watchdog-health http://<DGX-IP>:8000/health

# 2. install the LaunchAgent (refuses while disabled):
cc-continuity watchdog install

# operations:
cc-continuity watchdog start      # kickstart the running LaunchAgent
cc-continuity watchdog stop       # bootout
cc-continuity watchdog status     # plist + launchd state + baked binary
cc-continuity watchdog run        # foreground loop (development)
cc-continuity watchdog uninstall  # remove the LaunchAgent
```

With no health URL (`--watchdog-health off` or empty) the daemon runs in
**liveness-only mode**: health is treated as UP, no health pushes.

## Config

Keys in `~/.claude-continuity/config.json` (defaults in bold):

| key | default | meaning |
|---|---|---|
| `watchdog_enabled` | **`false`** | master switch; `install` refuses while off |
| `watchdog_health_url` | **`""`** | model server health endpoint; empty = liveness-only mode |
| `watchdog_interval_s` | **`300`** | main-loop tick (≈ 5 min) |
| `watchdog_stale_minutes` | **`45`** | ledger-silence threshold for "stale" |
| `watchdog_max_nudges` | **`3`** | unverified nudges before `needs_human` |
| `watchdog_nudge_text` | **`"continue"`** | the nudge prompt sent to the pane |
| `watchdog_network_recovery_cmd` | **`"sudo -n /usr/local/libexec/dgx-network-recovery"`** | local (Mac-side) route recovery run ONLY on a failed health probe; `""` = disabled |

## Behavior

### Health (P10-D4)

- A server restart is detected within **2×interval**: probe every tick,
  **2-fail debounce** — one failed probe is never a DOWN.
- **DOWN** → `dgx_down` push (urgent). While DOWN, nudges are **deferred**
  (`waiting_for_dgx`): a nudge is a model call, and the model is what is
  down.
- **UP again** → `dgx_up` push (high, with downtime) and the deferred
  nudge fires.

### Turn liveness (P10-D6, row order is binding)

| # | condition | outcome | daemon action |
|---|---|---|---|
| 1 | last ledger entry within `stale_minutes` | ALIVE | none |
| 2 | pane changed since the previous tick | ALIVE | none (rendering / tool output) |
| 3 | pane at the prompt AND `turn_active` | **TURN_DEAD** | nudge (send-keys text + Enter) + `turn_dead` push |
| 4 | pane at the prompt, turn ended, error in the tail | **STALLED** | nudge + `turn_dead` push |
| 5 | pane unchanged for 2+ consecutive ticks (input closed) | **FROZEN** | one `freeze_detected` push, **no auto-fix** |
| 6 | pane at the prompt, turn not active, no recent error | IDLE_WAITING | **never nudged** — the user is simply away |

Eligibility dominates age: a session whose state is not `ACTIVE`/`WARNED`
(ROTATING, HANDOFF, AMCL, …) is **skipped — log only**, never acted on.

### Nudge lifecycle (P10-D7)

- First nudge only while health is UP; afterwards, a nudge left sitting in
  the input line is submitted with a **bare Enter** (SEND_ENTER — a bare
  Enter is free even while DOWN; it does not count against
  `max_nudges`).
- **Give up**: `max_nudges` (3) unverified nudges → `needs_human` push,
  then DONE until a human prompt lands.

### Network recovery (P10)

A failed health probe may be a **stale route** (after a DGX or Mac
restart), not a real outage. Only on a failed probe the daemon runs
`watchdog_network_recovery_cmd` (the macOS root wrapper that re-establishes
the route — it briefly tears down the Mac's Ethernet, ~15 s), re-probes for
a **second chance**, and only then counts the tick as a failure. The
script's full `[DGX] ...` output is logged timestamped to
`runtime/watchdog-recovery.log` so it is inspectable inside Claude Code.
A healthy probe never triggers recovery; an empty command disables it.

## Verification (the 11:30 lesson)

A nudge counts as **recovered** only when the session's **ledger records a
`user_prompt` with a ts after the nudge ts** — the landing prompt is the
evidence, never a model's claim. A nudge that sat unsubmitted in the input
line is submitted with a bare Enter on the next unchanged tick.

## Logs

- `~/.claude-continuity/logs/watchdog.log` — one JSONL line per tick
  (health, per-session outcome, events, recovery summary).
- `~/.claude-continuity/logs/errors.log` — tick exceptions; the loop
  **always continues** (P10-D12: a watchdog failure must never break the
  continuity loop).
- `~/.claude-continuity/runtime/watchdog-recovery.log` — full output of
  every network-recovery run (timestamp + cmd + exit + elapsed + output).

## False-positive notes

- Legitimate user-idle (IDLE_WAITING) is **never** nudged.
- A quiet pane needs **2 unchanged ticks** before FROZEN — one idle tick
  is not a freeze.
- An error older than the last 10 non-prompt lines is ignored (it is
  history, not the current turn's failure).
- The daemon never acts on ROTATING/HANDOFF/AMCL sessions (log only).

## Known limitations

- One daemon per Mac (launchd).
- Liveness-only mode when no health URL is configured — no DOWN/UP
  detection, no network recovery trigger.
- The daemon is a separate process and can **NEVER** break the continuity
  loop: worst case is a missed recovery until the next tick; launchd
  `KeepAlive` restarts crashes.
- During a real DGX outage the network recovery runs once per failed-probe
  tick (default: every 5 min) and briefly drops the Mac's network each
  time. Bounded, and that is the point (a stale route is the most common
  outage cause) — set the command to `""` to disable.

## Acceptance record

- 2026-09-24: acceptance passed — scratch project + local fake server;
  evidence: pane capture (`you said: continue`), 3-push inbox
  (DOWN → back → turn died, exact order), watchdog.log
  (dgx_down / dgx_up / turn_dead, single daemon process), ledger
  (`watchdog_nudge` n=1). DGX untouched (P10-D1).
