"""Installer / uninstaller (PRD §17, P7-D1…P7-D12).

``install``: prerequisite checks; copy the app to
``~/.claude-continuity/app/`` + symlink ``~/.local/bin/cc-continuity``;
merge (never replace) ``~/.claude/settings.json`` (hooks, statusLine
chain, env, permissions — home-relative form per S12) with a timestamped
backup; managed block in ``~/.claude/CLAUDE.md`` (Appendix A); the six
user skills; managed block in ``~/.zshrc`` (Appendix D, symlink-aware);
write ``config.json`` (defaults + window + chained_statusline + manifest).
Idempotent: entries are identifiable by the substring ``cc-continuity``.

``uninstall``: manifest-driven inverse — removes exactly our entries and
blocks, restores the chained status line, keeps
``~/.claude-continuity/projects`` unless ``--purge``. Byte-for-byte
restoration of unmodified user files is a Gate 7 criterion.

Only this module may touch ``~/.claude/settings.json``,
``~/.claude/CLAUDE.md`` and ``~/.zshrc`` (PRD §0.6).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, atomic, config, managed, skills
from .settings_merge import (ACW_VALUE, detect_format, dump_settings,
                             merge_settings, unmerge_settings)

MIN_CLAUDE = (2, 1, 209)  # Phase 0 verified floor (VERIFICATION.md)

# ---------------------------------------------------------------- constants

CLAUDE_MD_BEGIN = "<!-- cc-continuity:begin -->"
CLAUDE_MD_END = "<!-- cc-continuity:end -->"
CLAUDE_MD_CONTENT = """\
## Session continuity (automatic, applies to every project)
This machine runs cc-continuity. Long work is continued across sessions automatically.
- If you receive a CONTINUITY message asking for a handoff: finish only the step you are in
  (complete the edit, run its test if quick), start nothing new, then write the handoff to the
  exact path given, using the exact template headings. Then end your turn.
- If a tool call is denied with a CONTINUITY reason: write the handoff immediately.
- If your conversation starts with "CONTINUITY RESUME" or a CONTINUITY bootstrap block:
  read the handoff fully, respect the VALIDATION result (diagnose mismatches before changing
  anything), never redo "Completed This Session", start from "Next Atomic Action".
  First line of your reply: "Continuing from cp-<NNN> (<new session name>)."
- Never edit files under ~/.claude-continuity except the handoff.draft.md you are told to write.
"""

ZSHRC_BEGIN = "# >>> cc-continuity >>>"
ZSHRC_END = "# <<< cc-continuity <<<"
ZSHRC_CONTENT = """\
claude() {
  # pass-through cases: disabled, already in tmux, non-interactive, print mode, subcommands, info flags
  if [[ "${CC_CONTINUITY:-1}" == 0 || -n "$CC_CONTINUITY_NO_TMUX" || ! -t 0 || ! -t 1 ]]; then
    command claude "$@"; return
  fi
  cc-continuity launch -- "$@"   # decides: pass-through (-p/--print/--version/--help/known subcommands
                                 # recorded in Phase 0 from `claude --help`) or new tmux session
                                 # "cc-<slug>-<rand4>" in $PWD running `command claude -n <slug>-NNN "$@"`
                                 # (no -n added if user passed -n/--name/-r/--resume/-c/--continue)
}
"""

LAUNCHER_STUB = '''#!/usr/bin/env python3
"""cc-continuity launcher (written by `cc-continuity install`)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_continuity.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'''


class InstallError(RuntimeError):
    """Prerequisite or pre-flight failure; no user file was modified."""


class UninstallError(RuntimeError):
    """Uninstall cannot proceed safely (unreadable/corrupt state)."""


# ------------------------------------------------------------- primitives


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def check_prerequisites(home: Path,
                        runner=subprocess.run) -> list[str]:
    """Return human-readable missing items; [] means all prerequisites OK."""
    missing = []
    if sys.version_info < (3, 11):
        missing.append(f"python >= 3.11 (found {sys.version.split()[0]})")
    if shutil.which("tmux") is None:
        missing.append("tmux on PATH")
    if shutil.which("zsh") is None:
        missing.append("zsh on PATH")
    claude = shutil.which("claude")
    if claude is None:
        missing.append("claude CLI on PATH")
    else:
        try:
            out = runner([claude, "--version"], capture_output=True,
                         text=True, timeout=30)
            v = parse_version(out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            v = None
        if v is None:
            missing.append("claude CLI (cannot parse `claude --version`)")
        elif v < MIN_CLAUDE:
            missing.append(f"claude >= 2.1.209 (found {v[0]}.{v[1]}.{v[2]})")
    return missing


def copy_app(app_root: Path) -> Path:
    """Copy the package into ``app_root`` + write the launcher stub.

    Returns the launcher path (``app_root/bin/cc-continuity``).
    """
    app_root = Path(app_root)
    pkg_src = Path(__file__).resolve().parent
    pkg_dst = app_root / "cc_continuity"
    if pkg_dst.exists():
        shutil.rmtree(pkg_dst)
    shutil.copytree(pkg_src, pkg_dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bin_dir = app_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "cc-continuity"
    launcher.write_text(LAUNCHER_STUB, encoding="utf-8")
    os.chmod(launcher, 0o755)
    return launcher


def make_symlink(link: Path, target: Path) -> None:
    """Create/refresh the symlink; never clobbers a non-symlink file."""
    link, target = Path(link), Path(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if os.path.realpath(link) != os.path.realpath(target):
            link.unlink()
            link.symlink_to(target)
        return
    if link.exists():
        raise InstallError(
            f"refusing to replace existing non-symlink file: {link}")
    link.symlink_to(target)


# ----------------------------------------------------------------- install


def _carry_over_manifest(manifest: dict, prev: dict, win: int) -> None:
    """Reinstall (F-G7-2): carry the user-original values the previous
    install recorded for the fields THIS merge saw as unchanged.

    ``merge_settings`` only records what the current merge changed, so a
    second install over an already-merged file would record "nothing
    changed" (status_line_changed=False, env_prev = our own values,
    empty allow/dirs lists) and uninstall could no longer restore the
    user's originals — breaking the Gate 7 byte-for-byte criterion.
    A field is carried over only when the new merge saw our own value
    there (i.e. the user did not edit it between installs); a fresh user
    edit always wins over the old manifest.
    """
    if not manifest.get("status_line_changed") and \
            prev.get("status_line_changed"):
        manifest["status_line_changed"] = True
        manifest["previous_status_line"] = prev.get("previous_status_line")
    ours = {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(win),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": ACW_VALUE}
    prev_env = prev.get("env_prev") or {}
    env_prev = manifest.get("env_prev") or {}
    for key, our_value in ours.items():
        if env_prev.get(key) == our_value and key in prev_env:
            env_prev[key] = prev_env[key]
    for field in ("allow_added", "dirs_added"):
        old = list(prev.get(field) or [])
        manifest[field] = old + [x for x in (manifest.get(field) or [])
                                 if x not in old]


def install(home: Path, *, window: int | None = None,
            runner=subprocess.run) -> dict:
    """Run the full install (PRD §17 steps 1–7 minus doctor/usage text).

    ``home`` is the user's HOME (the CLI passes ``Path.home()``).
    Returns the install manifest (also stored in ``config.json["install"]``).
    Raises ``InstallError`` before any user file is modified when a
    prerequisite is missing or a file is unreadable.
    """
    home = Path(home)
    missing = check_prerequisites(home, runner=runner)
    if missing:
        raise InstallError("missing prerequisites: " + "; ".join(missing))

    store = home / ".claude-continuity"
    store.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(store, 0o700)  # PRD 19: private store (doctor: store-perms)
    except OSError:
        pass
    app_root = store / "app"
    launcher = copy_app(app_root)
    bin_link = home / ".local" / "bin" / "cc-continuity"
    make_symlink(bin_link, launcher)
    manifest: dict = {
        "version": __version__,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "launcher": str(launcher),
    }

    # 1) settings.json (PRD §17.3): backup, then merge-never-replace
    claude_dir = home / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.is_file():
        raw = settings_path.read_bytes()
        try:
            existing = json.loads(raw)
        except ValueError as e:
            raise InstallError(f"invalid JSON in {settings_path}: {e}") from e
        if not isinstance(existing, dict):
            raise InstallError(
                f"settings.json must be a JSON object: {settings_path}")
        bak = settings_path.with_name(f"settings.json.bak-{_ts()}")
        n = 1
        while bak.exists():  # two installs in the same second
            n += 1
            bak = settings_path.with_name(f"settings.json.bak-{_ts()}-{n}")
        shutil.copy2(settings_path, bak)
        manifest["settings_backup"] = str(bak)
        manifest["settings_format"] = detect_format(raw)
        manifest["settings_created"] = False
    else:
        existing, manifest["settings_created"] = {}, True
    win = int(window) if window is not None else 262144
    # settings commands use the ~/.local/bin path (P7-D3); the manifest
    # records the app launcher itself
    merged, sm = merge_settings(existing, launcher=str(bin_link), window=win)
    atomic.atomic_write_text(
        settings_path, dump_settings(merged, manifest.get("settings_format")))
    manifest.update(sm)

    # 2) ~/.claude/CLAUDE.md managed block (PRD §17.4, Appendix A)
    claude_md = claude_dir / "CLAUDE.md"
    created_md = not claude_md.exists()
    prev_text = claude_md.read_text(encoding="utf-8") if claude_md.is_file() \
        else ""
    atomic.atomic_write_text(
        claude_md,
        managed.upsert_block(prev_text, CLAUDE_MD_BEGIN, CLAUDE_MD_END,
                             CLAUDE_MD_CONTENT.rstrip("\n")))
    manifest["claude_md_created"] = created_md

    # 3) user skills (PRD §17.5)
    skills.install_skills(claude_dir / "skills")

    # 4) ~/.zshrc managed block (PRD §17.6, Appendix D) — symlink-aware
    zshrc = home / ".zshrc"
    is_link = zshrc.is_symlink()
    target = zshrc.resolve() if is_link else zshrc
    created_z = not zshrc.exists() and not is_link
    ztext = target.read_text(encoding="utf-8") if target.is_file() else ""
    atomic.atomic_write_text(
        target,
        managed.upsert_block(ztext, ZSHRC_BEGIN, ZSHRC_END,
                             ZSHRC_CONTENT.rstrip("\n")))
    manifest["zshrc"] = {"symlink": is_link, "resolved": str(target),
                         "created": created_z}

    # 5) config.json (PRD §17.2/§15 + P7-D8/D12)
    cfg_path = store / "config.json"
    cfg = dict(config.DEFAULTS)
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (ValueError, OSError):
            pass  # corrupt user config: start from defaults; doctor flags it
    if window is not None:
        cfg["window_tokens"] = int(window)
    prev_manifest = cfg.get("install")
    if isinstance(prev_manifest, dict) and prev_manifest:
        _carry_over_manifest(manifest, prev_manifest, win)
    chained = (manifest.get("previous_status_line") or {}).get("command")
    if chained:
        cfg["chained_statusline"] = chained
    cfg["install"] = manifest
    atomic.atomic_write_json(cfg_path, cfg)
    return manifest


# -------------------------------------------------------------- uninstall


def uninstall(home: Path, *, purge: bool = False) -> None:
    """Inverse of ``install`` (PRD §17). Manifest-driven when the manifest
    exists; otherwise falls back to removing only identifiable entries
    (substring ``cc-continuity`` / marker blocks).

    Order: settings.json → CLAUDE.md → zshrc → skills → symlink → app →
    config.json → (optional) purge. ``projects/`` and ``logs/`` survive
    unless ``purge``. Raises ``UninstallError`` before modifying anything
    when state is unreadable.
    """
    home = Path(home)
    store = home / ".claude-continuity"
    cfg_path = store / "config.json"
    cfg: dict = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise UninstallError(f"unreadable {cfg_path}: {e}") from e
        if not isinstance(loaded, dict):
            raise UninstallError(f"config.json must be a JSON object: {cfg_path}")
        cfg = loaded
    manifest = cfg.get("install") or {}

    # 1) settings.json
    settings_path = home / ".claude" / "settings.json"
    if settings_path.is_file():
        raw = settings_path.read_bytes()
        try:
            existing = json.loads(raw)
        except ValueError as e:
            raise UninstallError(
                f"invalid JSON in {settings_path}: {e}") from e
        if not isinstance(existing, dict):
            raise UninstallError(
                f"settings.json must be a JSON object: {settings_path}")
        new = unmerge_settings(existing, manifest)
        if manifest.get("settings_created") and not new:
            # the installer created this file and the unmerge emptied it:
            # remove it instead of leaving a bare "{}" behind
            settings_path.unlink()
        else:
            atomic.atomic_write_text(
                settings_path,
                dump_settings(new, manifest.get("settings_format")
                              or detect_format(raw)))

    # 2) ~/.claude/CLAUDE.md
    claude_md = home / ".claude" / "CLAUDE.md"
    if claude_md.is_file():
        new_text = managed.remove_block(
            claude_md.read_text(encoding="utf-8"),
            CLAUDE_MD_BEGIN, CLAUDE_MD_END)
        if manifest.get("claude_md_created") and not new_text.strip():
            claude_md.unlink()
        else:
            atomic.atomic_write_text(claude_md, new_text)

    # 3) ~/.zshrc (resolved target; a file the installer CREATED is removed
    #    when the block leaves it empty)
    zinfo = manifest.get("zshrc") or {}
    zshrc = home / ".zshrc"
    if zinfo.get("resolved"):
        target = Path(zinfo["resolved"])
    else:
        target = zshrc.resolve() if zshrc.is_symlink() else zshrc
    if not target.is_file():  # stale manifest path -> current resolution
        target = zshrc.resolve() if zshrc.is_symlink() else zshrc
    if target.is_file():
        new_text = managed.remove_block(
            target.read_text(encoding="utf-8"), ZSHRC_BEGIN, ZSHRC_END)
        if zinfo.get("created") and not new_text.strip():
            target.unlink()
            if zshrc.is_symlink():
                zshrc.unlink()
        else:
            atomic.atomic_write_text(target, new_text)

    # 4) skills
    skills.uninstall_skills(home / ".claude" / "skills")

    # 5) ~/.local/bin symlink — only when it points into our app/
    #    (realpath on BOTH sides: a symlinked component in the home path,
    #     e.g. macOS /tmp -> /private/tmp, would otherwise mismatch — F-G7-1)
    link = home / ".local" / "bin" / "cc-continuity"
    if link.is_symlink() and os.path.realpath(link).startswith(
            os.path.realpath(str(store / "app")) + os.sep):
        link.unlink()

    # 6) app copy
    app = store / "app"
    if app.is_dir():
        shutil.rmtree(app)

    # 7) config.json: drop installer keys (keep user keys)
    cfg.pop("install", None)
    cfg.pop("chained_statusline", None)
    if cfg_path.is_file():
        if cfg:
            atomic.atomic_write_json(cfg_path, cfg)
        else:
            cfg_path.unlink()

    # 8) optional purge
    if purge and store.exists():
        shutil.rmtree(store)
