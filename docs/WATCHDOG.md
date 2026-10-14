# LLM Watchdog (Phase 10)

A watchdog daemon that keeps a watch over the local model server and the
live Claude Code sessions running on it. It nudges, it never restarts a
session, and — since Phase 13 — it powers the LLM box back on after a
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
4. detects **freeze** (a pane stuck unchanged) and reports it; since
   Phase 22 it also **escalates** a freeze that caught a turn in flight
   and outlived the threshold (see *Frozen-turn escalation*);
5. pushes events to the phone (ntfy).

Recovery is a **prompt, never a restart**: the daemon never kills a
session, never rotates, never reboots a *session* host. The original
P10-D1 rule ("no box-side action of any kind") is **superseded by Phase
13**: when the model server itself is unreachable the daemon may run a
user-configured *local* network-recovery command on the Mac (see below),
and — when the outage outlasts `power_cycle_after_s` and a Kasa power
plug is configured — it power-cycles the LLM box through the plug
(see **Auto power-cycle**).

## Install

```bash
# 1. opt in + point it at the model server (health URL is user-supplied;
#    the docs always show the placeholder, P10-D13):
cc-continuity configure --watchdog-enabled true \
    --watchdog-health http://<LLM-HOST>:8000/health

# 2. install the LaunchAgent (refuses while disabled):
cc-continuity watchdog install

# operations:
cc-continuity watchdog start      # kickstart the running LaunchAgent
cc-continuity watchdog stop       # bootout
cc-continuity watchdog status     # plist + launchd state + baked binary
cc-continuity watchdog run        # foreground loop (development)
cc-continuity watchdog uninstall  # remove the LaunchAgent

# digest relay (Phase 13 — the watchdog log shown in Claude Code):
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
| `watchdog_nudge_text` | **`"Continue."`** | the nudge prompt sent to the pane |
| `watchdog_nudge_only_most_recent` | **`false`** | when on, at most one session per tick may be nudged — the focus session (see *Nudge focus*, Phase 16); off = legacy nudge-all |
| `watchdog_network_recovery_cmd` | **`""`** | local (Mac-side) route-recovery command run ONLY on a failed health probe (e.g. a script that flushes + re-adds the default route); `""` (default) = disabled |
| `watchdog_frozen_nudge_minutes` | **`20`** | a FROZEN pane WITH a turn in flight escalates (ESC + nudge + generation probe) once the freeze outlives this (see *Frozen-turn escalation*, Phase 22) |
| `watchdog_gen_probe_timeout_s` | **`180`** | the 5-token completion step of the generation probe; a hung generation never answers — the timeout IS the detection |
| `power_plug_ip` / `power_plug_port` | **`""` / `9999`** | the smart plug (e.g. a Kasa HS100) on the model server's power path (the plug's own LAN port); empty IP (default) disables auto power-cycle |
| `power_cycle_off_s` | **`8`** | relay-off duration during a cycle |
| `power_cycle_after_s` | **`600`** | sustained DOWN before the auto power-cycle fires (10 min covers box boot + model load) |
| `power_cycle_cooldown_s` | **`1800`** | minimum spacing between successful cycles (30 min — no cycle loops) |
| `watchdog_digest_push_cap_s` | **`300`** | at most one digest injection per this window (file logging is uncapped) |

Env override: `CC_CONTINUITY_EXIT_HANG_LIMIT` (Phase 21) — the grace
in SECONDS before a hung `/exit` is killed; default **30**; `0`,
negative or non-numeric values are rejected (fall back to the default —
a 0 grace would kill a mid-`/exit`).

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
| 5 | pane unchanged for 2+ consecutive ticks (input closed) | **FROZEN** | one `freeze_detected` push per pane; since Phase 22, escalate when a turn was in flight and the freeze outlived `watchdog_frozen_nudge_minutes` (see *Frozen-turn escalation*) |
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
burn its `max_nudges` budget. The sessions share ONE model server: a tick that
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

### Nudge enrichment + force-cancel (Phase 19)

A bare base line gave the session no context about WHY its turn
stopped (too thin to resume on). Since Phase 19 every nudge
is an ENRICHED prompt (`watchdog.build_nudge_text`): the base line (the
configured `watchdog_nudge_text`) + a context block prefixed
`[cc-continuity watchdog]`, in two forms:

- **OUTAGE form** (the dgx_up recovery pass): names the outage window
  — `DOWN <HH:MM:SS> → UP <HH:MM:SS> (N min down)` — with a
  `→ power_cycle <HH:MM:SS>` segment ONLY when the last auto
  power-cycle ts falls INSIDE that window, and tells the session its
  in-flight turn died with the model and to check `git status` / the
  last output before continuing.
- **PLAIN form** (the normal TURN_DEAD/STALLED path): `The turn stalled
  (N min idle)` for a dead turn — or the stalled variant when the
  turn ended in error.

Before the text lands, the first physical nudge of an episode sends a
**standalone `send-keys Escape`** (force-cancel) and settles
`ESCAPE_SETTLE_S` (0.75 s): it kills the stuck in-flight turn and any
stale duplicate line left in the input box. RESEND and SEND_ENTER must
NOT escape — they submit the nudge already sitting in the box.

**Multi-line pending**: an unsubmitted
enriched nudge is MULTI-LINE — the `❯` glyph is on the FIRST input
line only, so `classify()` sees a non-idle pane (WAIT/FROZEN) and the
old single-line pending check never matched; the nudge would rot in
the input box. `pane_pending` now also matches the SENT text's TAIL
line (the sent text is re-read from the ledger's last `watchdog_nudge`
— the freshly built text's context block differs per tick), and on
WAIT/FROZEN with pending input the daemon submits it with a **bare
Enter** (no new attempt; SEND_ENTER never counts against
`max_nudges`). A FRESH episode (daemon restart) with pending input
also starts at SEND_ENTER, never at a re-paste.

`ESCAPE_SETTLE_S` (and the popup geometry/duration in the digest
relay) are v1 **module constants**, not config keys — they become
config when a second value is needed.

### Exit-hang killer (Phase 21)

CC 2.1.209's `/exit` can HANG: the marker entry is written to the
transcript but the process never dies (observed: 20+ h zombie). A
dead CC in a tmux pane blocks the project (no new session can take
the pane). The daemon kills it — a 5 s **fast pass**
(`check_exit_hangs`) runs on EVERY loop iteration; the full tick
(probe + session scan) still fires only every `watchdog_interval_s`
(one loop, two cadences, first iteration ticks immediately).

Detection (all per pass, one `ps` scan total):

1. live tmux sessions → live records; per target the **highest nnn
   wins** (an in-place rotation shares the pane);
2. the **turn guard BEFORE the ps scan**: any `turn_active` record
   makes the whole pass skip the scan — a mid-turn `/exit` waits for
   the Stop hook to clear `turn_active`;
3. per pane: the **topmost `claude`** in the pane process tree
   (BFS, exact basename), alive (`kill(pid, 0)`), whose transcript's
   `/exit` marker is older than the grace AND has **no user prompt
   after the marker** (a prompt = the user gave up on `/exit` —
   never touched);
   **Start-time guard (Phase 21.1, 2026-09-30):** the marker must be
   **this process's own** `/exit`. `read_transcript_entries` picks the
   dir's NEWEST transcript by mtime — that file can belong to an OLDER
   conversation (a `--continue`/`--resume` replay re-serves an old
   tail, or a fresh process is read before its own transcript exists).
   2026-09-30 incident: every fresh connector `claude` was killed ~5 s
   after launch against a 2-day-old marker. The guard: the process
   start ts (`ps -o etime= -p <pid>`, second-granular) is fetched ONLY
   for kill candidates; a marker older than start −
   `EXIT_HANG_START_EPS_S` (5 s) → **no kill**. The real 20.8 h hang is
   unaffected (its marker was written by the process itself, hours
   after its start). An unknown start (ps race/failure) degrades to
   **no kill** — a destructive action never runs on an unverified
   state; the next pass retries.
   **Session attribution (Phase 21.2, 2026-09-30):** the start-time
   guard compares marker age to process age but not OWNERSHIP — a
   FRESH marker from ANOTHER session's transcript (a `--continue`/
   `--resume` replay writing its `/exit` to its own old file, which
   becomes the dir's newest) can still be attributed to this session's
   CC (2026-09-30 16:10 false-positive kill). So `read_transcript_entries`
   reads the session's OWN `<sid>.jsonl` (keyed by the record's
   `session_id`); no own file → `None` → no marker → no kill. Records
   without `session_id` keep the legacy dir-newest read;
4. kill: SIGTERM, 5 s, then SIGKILL; then the **GATE cleanup** —
   `tmux kill-session -t <target>`: the killed claude was the
   session's main pane, and the orphaned **progress-pane would keep
   the session alive as a zombie** with no CC and no restart
   (launch is one-shot). The cleanup is best-effort: a failure
   degrades and never eats the push/log.

Outcome: one `exit_hang` ntfy push + **one** log line
(`exit_hang_killed:<key>`, pid/mode/age). A `"dead"` kill (lost the
race — someone else reaped it) joins the killed-set but gets **no
push, no log, no session cleanup**. A no-op pass writes **zero** log
lines (the 5 s cadence must not bloat the log). The killed-set
(daemon lifetime) guards re-kill/re-push of a zombie whose
`kill(pid, 0)` still succeeds.

### Frozen-turn escalation (Phase 22)

Phase 21's exit-hang killer handles a hung `/exit`. Phase 22 handles the
other way a turn dies silently: **vLLM hangs mid-generation**. The
liveness probe (`/health`) keeps answering **200 UP** while a completion
never returns (2026-09-29 morning: a 5-token ping never completed while
`/health` was healthy) — so the per-tick liveness probe could never see
it, and a session sat **FROZEN with its turn still "in flight"**
indefinitely, because a FROZEN pane is normally report-only.

Detection is a separate **generation probe** (`probe_generation`), run
**ONLY on suspicion** — never on a healthy tick, because a real 5-token
completion costs the shared model:

1. `GET {base}/v1/models` (10 s) — the model **NAME** is asked from the
   server, never read from config, so a box restart / model swap adapts
   itself. `{base}` = `watchdog_health_url` without its trailing path.
2. `POST {base}/v1/chat/completions` — a 5-token "ping" completion,
   timeout `watchdog_gen_probe_timeout_s` (180 s); ok = rc 0 AND HTTP 200.
   A hung generation never answers: **the timeout IS the detection**.

The result feeds a **separate gen HealthState** (2-fail debounce — the
same machine as liveness, so one failed probe is never a gen-DOWN).
Liveness and generation health are distinct: a hung vLLM is gen-DOWN
while liveness is still UP.

The FROZEN branch now has two paths. The announce is unchanged: **one
`freeze_detected` per PANE per run** — deduped on the resolved target,
so stale records that resolve to the same pane announce once, and
seeded from the ledger on a daemon restart (never re-announced).

- **turn in flight** (`turn_active`): once the freeze outlives
  `watchdog_frozen_nudge_minutes` (20 min), the daemon **escalates** —
  a force-cancel `Escape` + the enriched nudge in its **FROZEN form**
  (form priority OUTAGE > FROZEN > STALLED/PLAIN; the FROZEN line says
  the turn froze for *N* minutes and was ESC-cancelled, and to check
  `git status` / the last output before resuming) + a `frozen_nudge`
  push (title "frozen turn — escalated", `Nudge k/max`). The episode
  machine then continues on later ticks (RESEND/SEND_ENTER/GIVE_UP). The
  generation probe runs on the escalation tick and every tick after,
  while the pane stays FROZEN-with-turn. Sustained **gen-DOWN** past
  `power_cycle_after_s` (600 s) fires the **shared** auto power-cycle —
  the same relay and the same cooldown file as liveness (one relay, one
  30-min window); a **gen-DOWN→UP** fires `dgx_up` + the existing outage
  recovery (`_recover_stuck_turns`), which liveness can never trigger on
  its own.
- **no turn in flight** (the picker screen): **one** force-cancel
  `Escape` + a `frozen_nudge` push — no nudge text and **no model call**
  (nothing is generating, so there is nothing to nudge and nothing to
  probe).

Both paths are bounded per pane: the turn escalation runs at most **once
per tick** (a tick-local set) and the picker Esc at most **once per run**
(`frozen_esc`), so 11 stale records resolving to one pane produce one
announce, one escalation, and one gen probe — not eleven.

### Zombie-session watcher (Phase 23, fix a)

Phase 21's exit-hang killer and its GATE cleanup handle a **hung** CC and
the session left behind. A *clean* `/exit` can leave a different zombie:
the CC process exits normally, the record flips `off=True`, but the tmux
**session survives on its orphaned progress-pane** — no CC, no restart
(2026-09-30 13:47 connector incident: the session sat alive with nothing
running in it). The daemon now sweeps these at the end of each full tick
(`check_zombie_sessions`, 300 s cadence — the 5 s fast pass is
exit-hang-only). A still-alive tmux session is killed only when **all**
five fail-safe conditions hold:

1. the record is `off=True` and has a `tmux_session` field;
2. that session is in the current `list_sessions()`;
3. **no live (`off=False`) record resolves to it** (`resolve_pane`,
   prefix fallback included) — a shared / in-place-rotation pane is
   never touched;
4. **no `claude` process in any of the session's panes** (one `ps`
   snapshot, `find_claude_pid` per pane; the progress-pane is python,
   not claude);
5. it has been `off` for more than `ZOMBIE_GRACE_S` (120 s, per the
   record's `updated` ts) — a freshly-ended session may be mid-relaunch.

Conditions 1, 2, 3, 5 are cheap and run first, so a no-op pass costs
zero `ps`. The kill is `tmux kill-session` + one `zombie_cleaned` push
("no CC, killed") + a `zombie_cleaned:<key>:<nnn>` event; a successful
kill is deduped for the daemon's lifetime (`zombie_killed`), a failed
kill (`rc != 0`) is **not** — the next tick retries. Unparseable /
missing `updated` degrades to no kill.

### FROZEN progress-pane skip (Phase 23, fix b)

A progress-only session (the CC died; the session survives on its
orphaned progress-pane) renders a **static** "◆ NN% Active Usage" line.
An unchanged static capture classifies **FROZEN** after two ticks — and
Phase 22's FROZEN branch would act on it: a spurious `freeze_detected`
announce, and (with `turn_active`) an ESC + nudge + gen-probe
escalation sent to a pane that has no CC to freeze (the 2026-09-30
14:03 / 16:04 noise). The FROZEN branch now opens with a guard
(`_frozen_progress_pane`): skip **all** FROZEN handling when the
captured pane is a progress display. Two independent signals, either
suffices:

- the capture's last non-empty line **matches the progress usage line**
  (`_PROGRESS_TAIL_RE`, `^\s*◆?\s*\d+% Active Usage`) — checks the
  captured pane, so it catches a live CC sitting in ANOTHER pane while
  the active pane is the progress display;
- **OR** the `ps` snapshot shows **no `claude` in any of the session's
  panes** (no CC to be frozen) — the robust fallback, since the
  progress pane animates and its tail is not always the usage line.

Both signals False → the existing FROZEN behavior stands (no
regression). The helper never raises: a `ps`/`list_panes` failure
degrades to `False` (no skip), so a broken I/O path reverts to acting
on the freeze, never to silently ignoring a real one.

### Network recovery (P10)

A failed health probe may be a **stale route** (after a model server or Mac
restart), not a real outage. Only on a failed probe the daemon runs
`watchdog_network_recovery_cmd` (the macOS root wrapper that re-establishes
the route — it briefly tears down the Mac's Ethernet, ~15 s), re-probes for
a **second chance**, and only then counts the tick as a failure. The
command's full output is logged timestamped to
`runtime/watchdog-recovery.log` so it is inspectable inside Claude Code.
A healthy probe never triggers recovery; an empty command disables it.

### Auto power-cycle (Phase 13)

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
  first when truncated + footer:
  `(cc-continuity watchdog digest — log only, not a prompt)`);
- **always** appends it to `runtime/watchdog-digest.log`;
- then, outside the `watchdog_digest_push_cap_s` window (5 min), SHOWS
  it as a **~8 s tmux `display-popup`** (110×16, title
  `cc-continuity watchdog digest`) on the attached client of the
  **ONE most-recently-active live session** (max `last_ledger_ts` among
  ACTIVE/WARNED; pane via the daemon's `resolve_pane`, client via
  `list-clients -t <pane>` with a bare `list-clients` fallback).
  Phase 19: the digest is a VISIBLE LOG, never a prompt — the popup
  shows the text and closes itself; it never touches the input box
  (the old load-buffer + paste-buffer + Enter made the digest a queued
  PROMPT, indistinguishable from user input, that a rotation could
  eat). No attached client, unresolvable pane, or failed display → the
  digest is still file-logged, and a failed display records no push
  cap (next run retries). A popup cannot be shown to a tmux CONTROL
  client (`-CC`) — a tmux limitation (its own regress test), so on a
  control-client setup the digest is file-logged only.
- **mid-turn protection (Phase 14, kept)**: if that target session is
  `turn_active` (mid-turn), the display is SKIPPED (`skipped_busy`) and
  `last_ts` is held back (no advance) — the next 2-min tick re-reads
  the notable window and retries. A popup over a mid-turn session
  steals its keystrokes, and the user wants the digest ACTIONED, not
  arriving mid-turn.

Display filtering: `capture_failed:*` / `unresolvable:*` events (the
chronic dead-session noise, repeated every tick) are dropped from the
digest display so they cannot drown the outage signal.

## Verification

A nudge counts as **recovered** only when the session's **ledger records a
`user_prompt` with a ts after the nudge ts** — the landing prompt is the
evidence, never a model's claim. A nudge that sat unsubmitted in the input
line is submitted with a bare Enter on the next unchanged tick.

## Logs

- `~/.claude-continuity/logs/watchdog.log` — one JSONL line per tick
  (health, per-session outcome, events, recovery summary). The line's
  `health` field is the state at tick START (pre-transition): the tick
  that fires `dgx_down` still logs `health=UP`, the one that fires
  `dgx_up` still logs `health=DOWN` — the transition is the event, and
  the new state is confirmed on the next tick's line.
- `~/.claude-continuity/logs/errors.log` — tick exceptions; the loop
  **always continues** (P10-D12: a watchdog failure must never break the
  continuity loop).
- `~/.claude-continuity/runtime/watchdog-recovery.log` — full output of
  every network-recovery run (timestamp + cmd + exit + elapsed + output).
- `~/.claude-continuity/runtime/watchdog-digest.log` — every digest
  (always appended, displayed or not).
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
- During a real model server outage the network recovery runs once per
  failed-probe tick (default: every 5 min) and briefly drops the Mac's
  network each time. Bounded, and that is the point (a stale route is
  the most common outage cause) — set the command to `""` to disable.
