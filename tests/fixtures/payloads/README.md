# Hook payload fixtures (Phase 0 → Phase 3)

Recorded hook stdin payloads, one file per event. Source: Phase 0 spikes on
Claude Code **2.1.209** (see `docs/VERIFICATION.md`, S1/S6B):

| File | Source run |
|---|---|
| `SessionStart.json` … `TaskCompleted.json`, `Stop.json`, `SessionEnd.json` | `.sandbox/out/s1-tools/` |
| `PostToolUseFailure.json` | `.sandbox/out/s1b/` |
| `PreCompact.json` | `.sandbox/out/s6b/` |

File format (spike harness wrapper): `{"event", "mode", "ts", "pid", "env",
"payload"}` — the actual hook stdin content is the **`payload`** key.

If the Claude Code version changes, re-record per the recipe in
`HANDOFF.md` §7 and replace these files.
