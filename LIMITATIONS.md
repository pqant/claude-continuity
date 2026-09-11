# Limitations

Factual, short, known boundaries of the current implementation.

## Launch name prediction race

`next_counter_for` reads the project counter without locking; the
authoritative increment happens under lock in the SessionStart hook. Two
concurrent `launch` invocations in one project may predict the same NNN;
SessionStart assigns the second launch the next free counter. The
predicted name is used only for tmux session naming and pre-launch
logging — it is never authoritative (see the `launch.py` docstring).

## Legacy `notify_command`

The Phase 6 shell-command notification (`config.json: "notify_command"`)
still works in parallel with the Phase 9 ntfy push system. Both may be
active at once; the rotator's failure path calls both. New installs
should use the push system (`install --ntfy <url>`) and leave
`notify_command` unset.

## Native auto-compact is not enabled

The installer does not set `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. That
variable combined with a pre-existing `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
would switch on Claude Code's native auto-compact, which this design
deliberately avoids: the only context-reset mechanism is the rotation
(62% warning / 72% hard ceiling) — handoff details are written to disk
first, then a fresh session continues from them. (An earlier installer
version did write the variable, which silently enabled native
auto-compact for users with a PCT override; the installer now leaves the
key entirely alone.) If you set the variable
yourself, native auto-compact will apply to your sessions.

## Long unattended runs on a local inference box

If your model runs on local hardware (e.g. a vLLM box), the box itself
can stall under memory pressure — an infrastructure stall is not a
cc-continuity failure. The ledger and status line keep showing the last
known state; rotation resumes on the next Stop. Size your GPU memory
headroom for the worst case of a full-context request.

If the box reboots or the inference server dies while a model call is in
flight, there is no event for the hooks to fire on: Claude Code hooks fire
on completed turns and tool activity, not on a hung or dropped connection.
A dead peer that sends no TCP reset can keep the in-flight call hanging
for well over an hour before the client gives up, and the session then
sits at an API-error screen with no push and no state change until the
next prompt arrives. The on-disk ledger simply shows the last completed
activity. Mitigate with an inference server that auto-restarts (e.g. a
docker restart policy) and treat any session silent longer than your
worst-case request time as "check the box".

## Degraded checkpoints

If the handoff draft is missing or fails validation three times, the
session degrades to `HANDOFF_FAILED` and finalizes a DEGRADED checkpoint
with no handoff.md content, pushing an urgent error. Rotation still
proceeds; the successor resumes from git and logs only. Keep the model
on the handoff template (the block messages list the exact headings) so
degradation stays rare.

## ntfy delivery is best-effort

Pushes have no retry queue. A push missed while the phone or the ntfy
server is offline is not re-sent; the on-disk ledger is the durable
record and the status line reflects the current state on any later
attach.
