"""Health checks (PRD §16/§17). Read-only except deletion of
``checkpoint.tmp-*`` older than 1 h (PRD §18: ".tmp cleaned by doctor").

Exit code (CLI): 0 iff no check is not-ok; warnings do not fail.
A half-installed or uninstalled machine must never raise — every check
guards its own read errors.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, managed, notify, skills
from .install import (CLAUDE_MD_BEGIN, CLAUDE_MD_END, MIN_CLAUDE,
                      ZSHRC_BEGIN, ZSHRC_END, parse_version)
from .lock import pid_alive
from .settings_merge import HOOK_EVENTS, is_ours

TMP_MAX_AGE = 3600.0
LOCK_STALE_AFTER = 120.0


@dataclasses.dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False

    @property
    def symbol(self) -> str:
        if not self.ok:
            return "✗"
        return "!" if self.warning else "✓"


def tmp_age_cutoff() -> float:
    return time.time() - TMP_MAX_AGE


def _safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_doctor(home: Path, runner=subprocess.run,
               which=shutil.which) -> list[Check]:
    home = Path(home)
    store = home / ".claude-continuity"
    out: list[Check] = []

    def add(name, ok, detail, warning=False):
        out.append(Check(name, ok, detail, warning))

    # 1-3: prerequisites
    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    add("tmux", which("tmux") is not None, which("tmux") or "not on PATH")
    add("zsh", which("zsh") is not None, which("zsh") or "not on PATH")
    claude = which("claude")
    if claude is None:
        add("claude", False, "claude CLI not on PATH")
    else:
        try:
            r = runner([claude, "--version"], capture_output=True,
                       text=True, timeout=30)
            v = parse_version(r.stdout or "")
        except (OSError, subprocess.SubprocessError):
            v = None
        if v is None:
            add("claude", False, "cannot parse `claude --version`")
        else:
            detail = f"{v[0]}.{v[1]}.{v[2]}"
            add("claude", v >= MIN_CLAUDE, detail,
                warning=v != (2, 1, 209))
            if v != (2, 1, 209):
                out[-1].detail += " — re-run spikes S9/S10/S12 (HANDOFF §7)"

    # 4-5: app + launcher + symlink
    launcher = store / "app" / "bin" / "cc-continuity"
    pkg_ok = (store / "app" / "cc_continuity" / "__init__.py").is_file()
    add("app", pkg_ok and launcher.is_file()
        and os.access(launcher, os.X_OK), str(launcher))
    link = home / ".local" / "bin" / "cc-continuity"
    link_ok = (link.is_symlink() and launcher.is_file()
               and os.path.realpath(link).startswith(
                   os.path.realpath(str(store / "app")) + os.sep))
    add("symlink", link_ok, str(link))

    # 6: config.json
    cfg_path = store / "config.json"
    cfg = _safe_json(cfg_path)
    cfg_ok = isinstance(cfg, dict) and \
        isinstance(cfg.get("window_tokens"), int)
    add("config", cfg_ok, str(cfg_path))

    # 6b: curl (P9-P11 — notify needs it; install refuses without it)
    curl = which("curl")
    add("curl", curl is not None, curl or "not on PATH")

    # 6c: notify (P9-D3): configured → one test message, ✓/✗ by the curl
    #     exit; unset → "not configured (disabled)" (never a failure)
    ep = (cfg or {}).get("notify_endpoint")
    if not ((cfg or {}).get("notify_enabled") and ep):
        add("notify", True, "not configured (disabled)")
    else:
        ok = notify.send(str(ep), "🔔 cc-continuity test",
                         f"{notify.host()} cc-continuity {__version__}",
                         "default", event="notify-test", wait=True,
                         runner=runner)
        add("notify", ok,
            ep if ok else f"{ep} — test send failed (see errors.log)")

    # 7-8: settings hooks + statusLine
    settings = _safe_json(home / ".claude" / "settings.json")
    if not isinstance(settings, dict):
        add("settings-hooks", False, "settings.json missing/invalid")
        add("statusline", False, "settings.json missing/invalid")
    else:
        hooks = settings.get("hooks", {})
        missing = [e for e in HOOK_EVENTS
                   if not any(is_ours(x) for x in hooks.get(e, []))]
        add("settings-hooks", not missing,
            "all 10 events" if not missing else f"missing: {', '.join(missing)}")
        sl = settings.get("statusLine") or {}
        cmd = str(sl.get("command", ""))
        add("statusline", "cc-continuity" in cmd, cmd or "absent")
    # 9: chained statusline target exists (warning only — the user's own
    # script may live outside this HOME in a half-installed machine)
    chained = (cfg or {}).get("chained_statusline")
    if chained:
        target = Path(chained).expanduser()
        add("chained", True, str(target), warning=not target.exists())

    # 10: CLAUDE.md block
    claude_md = home / ".claude" / "CLAUDE.md"
    md_text = claude_md.read_text(encoding="utf-8") if claude_md.is_file() \
        else ""
    add("claude-md",
        managed.block_present(md_text, CLAUDE_MD_BEGIN, CLAUDE_MD_END),
        str(claude_md))

    # 11: zshrc block (symlink-aware)
    zshrc = home / ".zshrc"
    target = zshrc.resolve() if zshrc.is_symlink() else zshrc
    ztext = target.read_text(encoding="utf-8") if target.is_file() else ""
    add("zshrc", managed.block_present(ztext, ZSHRC_BEGIN, ZSHRC_END),
        str(target))

    # 12: skills
    sdir = home / ".claude" / "skills"
    missing_s = [n for n in skills.skill_names()
                 if not (sdir / n / "SKILL.md").is_file()]
    add("skills", not missing_s,
        "6/6" if not missing_s else f"missing: {', '.join(missing_s)}")

    # 13: window vs observed context_window_size (warning only)
    env = (settings or {}).get("env") or {}
    cfg_tokens = env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    observed = None
    for f in (store / "runtime").glob("*.json"):
        m = _safe_json(f)
        if isinstance(m, dict) and isinstance(m.get("context_window_size"), int):
            observed = max(observed or 0, m["context_window_size"])
    if cfg_tokens and observed:
        try:
            mismatch = int(cfg_tokens) != observed
        except ValueError:
            mismatch = True
        add("window", not mismatch,
            f"config {cfg_tokens} vs observed {observed}",
            warning=mismatch)

    # 14: stale locks (warning)
    stale = []
    for lf in (store / "projects").glob("*/lock"):
        info = _safe_json(lf)
        if not isinstance(info, dict):
            continue
        ts, pid = info.get("ts"), info.get("pid")
        if (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and time.time() - ts > LOCK_STALE_AFTER
                and isinstance(pid, int) and not pid_alive(pid)):
            stale.append(lf.parent.name)
    add("stale-locks", True,
        "none" if not stale else f"stale: {', '.join(stale)}",
        warning=bool(stale))

    # 15: tmp residue — delete > 1 h, warn about the rest
    deleted, recent = [], []
    for t in store.rglob("checkpoint.tmp-*"):
        if not t.is_dir():
            continue
        try:
            old = time.time() - t.stat().st_mtime > TMP_MAX_AGE
        except OSError:
            continue
        if old:
            shutil.rmtree(t, ignore_errors=True)
            deleted.append(t.name)
        else:
            recent.append(t.name)
    add("tmp-residue", True,
        "none" if not (deleted or recent)
        else f"deleted {deleted} recent {recent}",
        warning=bool(deleted or recent))

    # 16: errors.log tail (warning)
    el = store / "logs" / "errors.log"
    if el.is_file():
        lines = el.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines:
            add("errors-log", True, "…".join(lines[-3:]), warning=True)
        else:
            add("errors-log", True, "empty")
    else:
        add("errors-log", True, "no errors.log")

    # 17: store permissions (warning)
    if store.is_dir():
        mode = os.stat(store).st_mode & 0o777
        add("store-perms", True, oct(mode), warning=mode != 0o700)

    return out


def render(checks: list[Check]) -> str:
    return "\n".join(f"{c.symbol} {c.name} — {c.detail}" for c in checks)
