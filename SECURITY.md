# Security

## Reporting a vulnerability

Use this repository's **Security** tab → *Reporting a vulnerability*
(GitHub private vulnerability reporting). Do not open a public issue
for a security concern.

## Trust model

cc-continuity is local infrastructure for your own Claude Code setup.
Read this before installing:

- **Local code execution.** The installer copies the package to
  `~/.claude-continuity/app/` and wires Claude Code hooks that run that
  local Python. It runs with your user's rights, on your machine.
- **Settings merge, never replace.** `~/.claude/settings.json`,
  `~/.claude/CLAUDE.md` and `~/.zshrc` are edited as identifiable
  managed blocks; a timestamped backup of `settings.json` is written
  first, and `cc-continuity uninstall` restores byte-for-byte.
- **Daemon.** The watchdog is an opt-in launchd LaunchAgent running
  local Python; it may curl your own model-server health URL and, if
  you configure it, your own power-plug endpoint.
- **User-supplied commands only.** `watchdog_network_recovery_cmd` and
  the power-plug IP/port are read from YOUR config and executed with
  YOUR rights. They are never derived from network content, tool
  output, or any other untrusted input.
- **Outbound data.** With `notify_enabled`, short lifecycle pushes
  (≤ 200 characters: project slug, session number, context ratio, a
  summary you control, hostname) go only to the ntfy endpoint YOU
  configure. Text recorded in ledgers and handoffs is secret-redacted
  before storage (generic default patterns; extend via
  `redact_patterns_extra`).
- **No telemetry.** There is no update checker, no analytics, and no
  data flow to any endpoint by default.
