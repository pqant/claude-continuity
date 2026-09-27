# LLM Watchdog (Phase 10)

A watchdog daemon that keeps a watch over the local model server and the
live Claude Code sessions running on it. It nudges, it never restarts a
session, and — since Phase 13 — it powers the DGX back on after a
sustained outage (the one sanctioned box-side action; see **Auto
power-cycle**).

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
session, never rotates, never reboots a *session* host. The original
P10-D1 rule ("no box-side action of any kind") is **superseded by Phase
13**: when the model server itself is unreachable the daemon may run a
user-configured *local* network-recovery command on the Mac (see below),
and — when the outage outlasts `power_cycle_after_s` and a Kasa power
plug is configured — it power-cycles the DGX through the plug
(see **Auto power-cycle**, design 7899).

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

# digest relay (Phase 13 — the watchdog log piped into Claude Code):
cc-continuity watchdog digest run        # one digest pass (foreground)
cc-continuity watchdog digest install    # launchd periodic agent (120 s)
cc-continuity watchdog digest start      # kickstart (run one pass now)
cc-continuity watchdog digest status     # plist + launchd state
cc-continuity watchdog digest stop       # bootout
cc-continuity watchdog digest uninstall  # remove the agent
```

Note: `watchdog start` on an already-running `KeepAlive` agent is a
**no-op** — a full restart is `watchdog stop` + `launchctl bootstrap
gui/<uid> <plist>` (a kickstart does not re-spawn a running job).

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
| `watchdog_nudge_only_most_recent` | **`false`** | when on, at most one session per tick may be nudged — the focus session (see *Nudge focus*, Phase 16); off = legacy nudge-all |
| `watchdog_network_recovery_cmd` | **`"sudo -n /usr/local/libexec/dgx-network-recovery"`** | local (Mac-side) route recovery run ONLY on a failed health probe; `""` = disabled |
| `power_plug_ip` / `power_plug_port` | **`"192.0.2.99"` / `9999`** | the Kasa HS100 on the DGX power path (the plug's own LAN port); empty IP disables auto power-cycle |
| `power_cycle_off_s` | **`8`** | relay-off duration during a cycle |
| `power_cycle_after_s` | **`600`** | sustained DOWN before the auto power-cycle fires (10 min covers box boot + model load) |
| `power_cycle_cooldown_s` | **`1800`** | minimum spacing between successful cycles (30 min — no cycle loops) |
| `watchdog_digest_push_cap_s` | **`300`** | at most one digest injection per this window (file logging is uncapped) |

## Behavior

### Health (P10-D4)

- A server restart is detected within **2×interval**: probe every tick,
  **2-fail debounce** — one failed probe is never a DOWN.
- **DOWN** → `dgx_down` push (urgent). While DOWN, nudges are **deferred**
  (`waiting_for_dgx`): a nudge is a model call, and the model is what is
  down.
- **UP again** → `dgx_up` push (high, with downtime) and the deferred
  nudge fires. Phase 15 additionally **nudges any eligible session still
  mid-turn** (`turn_active`) whose ledger is fresh — a turn that died on the
  outage's API error and `classify()` masks as ALIVE (see *Outage recovery*).

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

### Outage recovery (Phase 15)

A turn that dies on the outage's API error leaves `turn_active` True with **no
`stop`** — so `last_ledger_ts` stays at the turn-start prompt and, for an
outage shorter than `stale_minutes`, `classify()` returns **ALIVE** (row 1)
before any pane check. The normal nudge path (rows 3–4) never sees it, and it
sat dead through `dgx_up` until a human prompt (regression: 2026-09-26 22:14
outage, session 044). On the DOWN→UP **transition tick** the daemon now nudges
each eligible session still `turn_active` with a **fresh** ledger (the
ALIVE-masked set); a stale ledger is left to the normal TURN_DEAD/STALLED path
so the two never double-nudge the same tick. The pass fires only on the
transition tick, so a recovery turn in flight is not re-nudged.

### Nudge focus (Phase 16)

With `watchdog_nudge_only_most_recent` on, **at most one session per tick
may be nudged**: the *focus* session — the most-recently-active
NUDGE_ELIGIBLE session (max `last_ledger_ts`), which is exactly the session
the digest relay talks to (the restriction mirrors
`watchdog_digest._best_candidate` so only the session the digest talks to
may be revived). Every other eligible session stays dormant and does not
burn its `max_nudges` budget. The sessions share ONE DGX model: a tick that
nudged every stalled session at once put concurrent load on that shared
model, slowing — or destabilizing — exactly the session that matters (the
active one). The focus gates BOTH nudge sites: the per-session
TURN_DEAD/STALLED path and the Phase 15 dgx_up recovery pass (at most one
stuck mid-turn session is recovered per transition tick — the one the
digest would target anyway). Off (default) = legacy behavior: every
eligible session may be nudged.

### Pane resolution (Phase 17)

The nudge target is the session's tmux pane. The hook-recorded
`tmux_session` field wins **only while it still exists** in the live
`tmux list-sessions` output. The regression (2026-09-27, sessions
046/047): `claude --continue` resumes the old conversation in a **NEW**
tmux session that the startup-only recording never saw — the field kept
pointing at a dead session name while the live session had a different
one, and every nudge/digest sat `CAPTURE_FAILED` for 40+ minutes while
the session looked fine. Three parts:

1. **Stale-field fallback** (`resolve_pane`): a field that no longer
   exists in the live list falls through to the prefix match
   `cc-<slug>-` — exactly one match → use it; zero, several, or no tmux
   server → `None` (UNRESOLVABLE: log, never nudge). With no tmux server
   the field's existence cannot be verified, so the field is kept (legacy
   behavior) and the failed capture is logged `CAPTURE_FAILED` — the
   honest label.
2. **Re-recording**: `SessionStart` resume/fork re-records
   `tmux_session` to the session that actually launched the conversation
   (attach only — no registration, no state change).
3. **Zombie cleanup**: `SessionEnd reason=resume` marks the abandoned
   startup session off — `claude --continue` registers a new sid and then
   abandons it when it resumes the earlier conversation, which would
   otherwise leave an ACTIVE zombie that watchdog/digest scans keep
   treating as a live candidate (session 047).

### Network recovery (P10)

A failed health probe may be a **stale route** (after a DGX or Mac
restart), not a real outage. Only on a failed probe the daemon runs
`watchdog_network_recovery_cmd` (the macOS root wrapper that re-establishes
the route — it briefly tears down the Mac's Ethernet, ~15 s), re-probes for
a **second chance**, and only then counts the tick as a failure. The
script's full `[DGX] ...` output is logged timestamped to
`runtime/watchdog-recovery.log` so it is inspectable inside Claude Code.
A healthy probe never triggers recovery; an empty command disables it.

### Auto power-cycle (Phase 13, design 7899)

A prolonged outage (a failed recovery means the box itself is dead, not
just the route) is healed by cutting its power:

- **Gate**: health DOWN for at least `power_cycle_after_s` (10 min —
  covers box boot + model load, so a slow boot is never cycled) AND the
  last successful cycle is at least `power_cycle_cooldown_s` old (30 min)
  AND `power_plug_ip` is set.
- **Action**: `POST http://127.0.0.1:9999/cycle` — the localhost
  power-plug endpoint (its own LaunchAgent) drives the Kasa relay
  off → `power_cycle_off_s` → on, every step verified.
- **Cooldown is recorded ONLY on a successful cycle**
  (`runtime/power-cycle.json`, file-based so a launchd restart cannot
  arm an immediate re-cycle). A FAILED attempt (endpoint down, plug
  unreachable) does NOT consume the cooldown — it retries next tick; a
  broken plug must not block recovery for 30 minutes.
- **Push**: `dgx_power_cycle` (urgent) only on a successful cycle start;
  failures are log-line + event (surfaced via the digest relay) — one
  push per failed attempt would spam the phone.

### Digest relay (Phase 13 — the watchdog log in Claude Code)

The watchdog's actions used to be invisible inside a Claude Code session
(the user could not see the daemon act during an outage). A launchd
**periodic** agent (`com.claude-continuity.watchdog-digest`,
`StartInterval` 120 s, `RunAtLoad`, **no KeepAlive**) runs
`cc-continuity watchdog digest run` every 2 min:

- reads the new JSONL entries of `logs/watchdog.log` since the last
  processed ts (first run **seeds to now** — a week of history is never
  replayed);
- keeps the **notable** ones: `health != UP` (DOWN, cold-start UNKNOWN,
  error ticks), or a `dgx_down`/`dgx_up`/`power_cycle`/`power_cycle_*`
  event, or a network recovery that ran;
- formats a digest of ≤ 8 lines (header + ≤ 5 entry lines, most recent
  first when truncated + footer);
- **always** appends it to `runtime/watchdog-digest.log`;
- then, outside the `watchdog_digest_push_cap_s` window (5 min), injects
  it into the **ONE most-recently-active live session's pane**
  (max `last_ledger_ts` among ACTIVE/WARNED; pane via the daemon's
  `resolve_pane`) using tmux load-buffer + paste-buffer (bracketed
  paste — newlines land in the input box without submitting) + one
  Enter: a single queued prompt, same mechanism as a mid-turn user
  message. No eligible session, unresolvable pane, or failed inject →
  the digest is still file-logged, and a failed inject records no push
  cap (next run retries).
- **mid-turn protection (Phase 14)**: if that target session is
  `turn_active` (mid-turn), the injection is SKIPPED and `last_ts` is held
  back (no advance) — the next 2-min tick re-reads the notable window and
  retries. The user wants the digest ACTIONED, not queued behind a busy
  turn (an outage digest queued into a mid-turn session is lost the moment
  a rotation runs).

Display filtering: `capture_failed:*` / `unresolvable:*` events (the
chronic dead-session noise, repeated every tick) are dropped from the
digest display so they cannot drown the outage signal.

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
- `~/.claude-continuity/runtime/watchdog-digest.log` — every digest
  (always appended, injection or not).
- `~/.claude-continuity/runtime/watchdog-digest-state.json` — the
  relay's cursor (`last_ts`) and push-cap clock (`last_push_ts`).
- `~/.claude-continuity/runtime/power-cycle.json` — ts of the last
  successful auto power-cycle (the cooldown state).

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
  (`watchdog_nudge` n=1). DGX untouched (P10-D1 — superseded below).
- 2026-09-26: Phase 13 live — during the 07:58–08:41 DGX outage the
  daemon ran 5 network recoveries (08:09/08:14/08:19/08:30/08:35, all
  exit=0) and the health machine tracked the flap
  (DOWN 08:14 → false UP 08:25 → DOWN 08:35 → UP 08:40); auto
  power-cycle + digest relay deployed and verified live (forced digest
  pass injected the outage timeline into a live session, `pushed: true`;
  the tmux 3.7b `load-buffer` stdin rejection was caught by that live
  run and fixed with the file-argument form).
- 2026-09-27: **Phase 15 live re-test PASSED** — the first live proof
  of the dgx_up recovery path (its regression, the 2026-09-26 22:14
  outage, had left session 044 dead). The user unplugged the DGX ~19:20
  while a daemon-injected digest prompt (19:19:59) had just started a
  turn in session 051 — the turn died with the model and stayed
  `turn_active`, classifying **ALIVE on every DOWN tick** (19:29–19:39,
  fresh-ledger mask — exactly the 044 case the normal nudge path never
  sees). All daemon-side, zero human input: `dgx_down` 19:23:44 → auto
  `power_cycle` 19:34:15 (≈10 min sustained DOWN) → `dgx_up` 19:45:01 →
  Phase 15 pass sent `continue` to the focus session
  (`dgx_up_recovery:claude-continuity-5da1c3ab:051`, ledger
  `watchdog_nudge` n=1 + `user_prompt` at the nudge ts) → the session
  resumed on its own → `watchdog_verified` n=1, tick outcome
  **VERIFIED** 19:50:01. Cut → recovered ≈ 25 min. The stale-route
  network recoveries on the 19:18–19:39 failed-probe ticks (all
  exit=0, 15 s) are expected bounded behavior.
