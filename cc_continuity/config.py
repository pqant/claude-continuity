"""Configuration (PRD §15): defaults ← global config.json ← project override.

Merge order (later wins): built-in defaults, then
``<home>/config.json``, then the project file ``.claude/continuity.json``.
The project file is searched from ``cwd`` up to the git root (inclusive);
outside a git repo only ``cwd`` itself is consulted, so an unrelated ancestor
directory can never disable/override a project.

``CC_CONTINUITY=0`` in the environment disables everything, regardless of files.

``window_tokens`` default chain (PRD §7): if no config file sets it, the
``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` environment value is used; else 262144.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import paths


class ConfigError(RuntimeError):
    """A config file exists but cannot be parsed/validated."""


# PRD §15, verbatim.
DEFAULTS: dict = {
    "enabled": True,
    "window_tokens": 262144,
    "warn_ratio": 0.55,
    "rotate_ratio": 0.62,
    "hard_ratio": 0.72,
    "full_auto": True,
    "settle_ms": 1500,
    "boundary_command_regex": (
        r"(pytest|cargo (test|build)|npm (run )?(test|build|lint)"
        r"|go (test|build|vet)|make\b|tsc\b|ruff|mypy)"
    ),
    "git_snapshot": True,
    "snapshot_retention": 20,
    "handoff_max_words": 5000,
    "inline_handoff_max_chars": 12000,
    "notify_command": None,
    "notify_endpoint": "",
    "notify_enabled": False,
    "unattended": False,
    "statusline_ansi": True,
    # Progress display (Phase 12): 0 = statusline shimmer (default, today's
    # look), 1 = separate tmux pane (true 50 ms), 2 = statusline
    # "breathing". Missing/invalid values degrade to 0 via progress_mode().
    "progress_type": 0,
    "watchdog_enabled": False,
    "watchdog_health_url": "",
    "watchdog_interval_s": 300,
    "watchdog_stale_minutes": 45,
    "watchdog_max_nudges": 3,
    "watchdog_nudge_text": "continue",
    # On a failed health probe the daemon runs this (macOS root wrapper,
    # passwordless sudo) to re-establish the DGX route, then re-probes. Empty
    # string disables the step. See setup_dgx_network_recovery.sh.
    "watchdog_network_recovery_cmd":
        "sudo -n /usr/local/libexec/dgx-network-recovery",
    "redact_patterns": [
        r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+",
        r"sk-[A-Za-z0-9]{20,}",
        r"ghp_[A-Za-z0-9]{30,}",
    ],
    "log_level": "info",
}


def _git_root(start: Path) -> Path | None:
    """Git toplevel containing ``start``, or None if not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def _project_config_path(cwd: Path) -> Path | None:
    """The .claude/continuity.json that applies to ``cwd``, if any."""
    try:
        cwd = cwd.resolve()
    except OSError:
        cwd = Path(os.path.abspath(cwd))
    stop: Path | None = _git_root(cwd)
    if stop is None:
        candidates = [cwd]
    else:
        candidates = []
        cur = cwd
        while True:
            candidates.append(cur)
            if cur == stop:
                break
            cur = cur.parent
    for d in candidates:
        p = d / ".claude" / "continuity.json"
        if p.is_file():
            return p
    return None


def _read_json_object(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"invalid config file {path}: {e}") from e
    if not isinstance(obj, dict):
        raise ConfigError(f"config file must be a JSON object: {path}")
    return obj


def load_config(cwd: str | Path | None = None) -> dict:
    """Merged config for ``cwd`` (defaults: the process working directory)."""
    if cwd is None:
        cwd = Path.cwd()
    cwd = Path(cwd)

    cfg = dict(DEFAULTS)
    window_explicit = False

    global_path = paths.config_path()
    if global_path.is_file():
        g = _read_json_object(global_path)
        cfg.update(g)
        window_explicit = "window_tokens" in g

    project_path = _project_config_path(cwd)
    if project_path is not None:
        p = _read_json_object(project_path)
        cfg.update(p)
        window_explicit = window_explicit or "window_tokens" in p

    if not window_explicit:
        # PRD §7: default window is CLAUDE_CODE_MAX_CONTEXT_TOKENS, else 262144.
        env = os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "").strip()
        if env.isdigit():
            cfg["window_tokens"] = int(env)

    return cfg


def is_enabled(cfg: dict) -> bool:
    """Effective enable flag: config ``enabled`` AND ``CC_CONTINUITY != "0"``."""
    if not cfg.get("enabled", True):
        return False
    return os.environ.get("CC_CONTINUITY", "1").strip() != "0"


def progress_mode(cfg: dict) -> int:
    """Validated progress display mode (Phase 12).

    ``progress_type``: 0 = statusline shimmer (default / current look),
    1 = separate tmux pane (true 50 ms), 2 = statusline "breathing".
    A missing key or any invalid value (non-int, out of range) degrades to
    0, so an existing install renders byte-identically to today.
    """
    v = cfg.get("progress_type", 0)
    return v if v in (0, 1, 2) else 0
