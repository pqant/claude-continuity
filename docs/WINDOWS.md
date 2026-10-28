# Windows (via WSL)

cc-continuity runs inside a Windows Subsystem for Linux distro. The Linux
side is unchanged — same rotation, same watchdog, same ntfy pushes; only
the daemon hosting differs: where macOS uses launchd, WSL uses a
detached **tmux session** (default) or **systemd user units** (WSL 2
with systemd enabled). The Windows side adds one optional logon task so
the daemons come back when the WSL VM restarts (see **Autostart**).

## Requirements

- Windows 10/11 with WSL. **WSL 2 is recommended**; WSL 1 works too,
  minus the systemd option (see **WSL 1**).
- Ubuntu 24.04 recommended. Ubuntu 22.04 works, but ships Python 3.10 —
  install `python3.11` (deadsnakes) and run the installer with the 3.11+
  interpreter (see below).
- Python ≥ 3.11 (standard library only at runtime).
- tmux ≥ 3.2 on PATH — `sudo apt install tmux` is enough (Ubuntu 22.04
  ships 3.2a, 24.04 ships 3.4). The daemon session relies on
  `new-session -e` (environment injection), a tmux 3.2 feature.
- bash or zsh as the login shell. WSL's default login shell is bash and
  bash is always present — **no zsh needed**. (Any other shell, e.g.
  fish, is refused at install with the exact fix.)
- Claude Code ≥ 2.1.209, installed inside the distro (the official Linux
  installer).

Check your WSL version from PowerShell:

```powershell
wsl -l -v    # VERSION column: 2 recommended, 1 works minus systemd
```

## Install

Inside the distro (WSL terminal):

```bash
# 1. tmux (tmux 3.2a on 22.04 is fine — it ships it):
sudo apt update && sudo apt install -y tmux

# 2. Claude Code: run the official Linux installer inside the distro,
#    then check it:
claude --version        # >= 2.1.209

# 3. cc-continuity — from Git:
git clone https://github.com/pqant/claude-continuity
cd claude-continuity
python3 -m cc_continuity.cli install --window 262144
#    … or from PyPI:
pip install cc-continuity
cc-continuity install

#    Ubuntu 22.04 only: 3.10 is too old — the installer says so.
#    Install python3.11+ (deadsnakes) or use Ubuntu 24.04, then run
#    the installer with the 3.11+ interpreter:
#    python3.11 -m cc_continuity.cli install --window 262144

# 4. reload the shell (the claude() wrapper lands in ~/.bashrc —
#    the login shell's rc — as an identifiable managed block):
exec $SHELL

# 5. health checks (everything should be green):
cc-continuity doctor
```

`cc-continuity doctor` on WSL reports the WSL-specific checks:
`shell` (bash is fine), `rc-block` (the managed block in `~/.bashrc`),
`wsl` (informational: which host is active), `daemon` (running state of
every installed daemon), and `autostart` (the Windows logon task — see
below).

## The watchdog

Daemons are opt-in, exactly like macOS. Configure, then install:

```bash
# opt in + point it at the model server (health URL is user-supplied;
# the docs always show the placeholder):
cc-continuity configure --watchdog-enabled true \
    --watchdog-health http://<LLM-HOST>:8000/health

# install: a tmux session (WSL 1 / WSL 2 without systemd) or a systemd
# user unit (WSL 2 with systemd enabled) — the CLI picks:
cc-continuity watchdog install

# operations (identical to macOS):
cc-continuity watchdog status
cc-continuity watchdog stop
cc-continuity watchdog start
cc-continuity watchdog uninstall

# digest relay (the watchdog log shown in Claude Code):
cc-continuity watchdog digest install
cc-continuity watchdog digest status
```

On the tmux host the daemon lives in a detached session named
`cc-d-<daemon>`; when the supervisor exits the session ends, so
`tmux has-session` is a complete liveness test. The installer also
appends one **self-heal line per daemon** to your shell rc:

```bash
tmux has-session -t =cc-d-watchdog 2>/dev/null || cc-continuity daemon ensure cc-d-watchdog
```

The leading `=` forces an EXACT tmux target — a bare `cc-d-watchdog`
prefix-matches the `cc-d-watchdog-digest` session, so a dead watchdog
would look alive and the self-heal would never run.

So the next shell you open repairs a dead daemon by itself — and if you
want to repair it now:

```bash
cc-continuity daemon ensure watchdog      # one daemon
cc-continuity daemon ensure               # all installed daemons
cc-continuity daemon status               # / status <name>
cc-continuity daemon stop watchdog        # stop one
```

## Autostart (Windows side)

WSL daemons die when the WSL VM stops (`wsl --shutdown`, sleep, reboot).
The rc self-heal covers *shells*, but a stopped VM has no shell. The
completing piece is one Windows scheduled task that ensures the daemons
at logon (created in PowerShell, distro name parameterized — here
`Ubuntu`):

```bat
schtasks /Create /TN cc-continuity-ensure /SC ONLOGON ^
  /TR "wsl.exe -d Ubuntu cc-continuity daemon ensure"
```

`cc-continuity daemon ensure` (no name) walks every installed daemon.
`cc-continuity doctor` probes the task through WSL interop
(`schtasks.exe /query`) and warns with the exact one-liner when it is
missing; with interop disabled it reports `not verifiable`.

## Optional: WSL 2 + systemd

Prefer user units over tmux sessions? Enable systemd in the distro:

```bash
# in /etc/wsl.conf:
[boot]
systemd=true
```

then restart the VM from PowerShell:

```powershell
wsl --shutdown
```

With systemd running, `cc-continuity watchdog install` writes
`~/.config/systemd/user/cc-continuity-watchdog.service` (no sudo, never
system scope) and enables it. Add linger so the units start at VM boot
even when no login shell is open yet:

```bash
loginctl enable-linger $USER
```

## Troubleshooting

| Symptom (doctor shows) | Fix |
|---|---|
| `daemon` — not running | The rc self-heal repairs it on the next shell, or now: `cc-continuity daemon ensure <name>` |
| `daemon` — session exists but is not ours (name collision) | `cc-continuity daemon stop <name>` then `cc-continuity daemon ensure <name>` |
| `rc-block` — missing | Re-run `cc-continuity install` (the block is upserted, nothing else is touched) |
| `python` — 3.10 | Ubuntu 24.04, or `python3.11` from deadsnakes, then run the installer with the 3.11+ interpreter |
| `shell` — nothing on PATH | `sudo apt install zsh` (or bash, which is always present) |
| `autostart` — task missing | Run the **Autostart** one-liner in PowerShell |
| Wrong WSL version | `wsl -l -v` in PowerShell; WSL 2 recommended, WSL 1 works minus the systemd option |

## What differs from macOS

- **No Kasa power-cycle.** `cc-continuity power-plug` is refused
  off-macOS: the LLM box in the WSL setup is the Mac (or a server) that
  the distro reaches over the network — power cycling it from inside
  WSL is not part of this design.
- **No launchd.** The same lifecycle commands install a tmux session or
  a systemd user unit instead of a LaunchAgent; `watchdog start/stop/
  status/uninstall` behave identically.
- **The rc file is the login shell's** (`~/.bashrc` by default,
  `~/.zshrc` for zsh), tracked in the install manifest so uninstall
  cleans exactly what the installer wrote.

## WSL 1

WSL 1 is the e2e-validated default path: no systemd is available there,
so the tmux host is the only host and everything above applies as-is
(except the **Optional: WSL 2 + systemd** section).
