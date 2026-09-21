"""settings.json merge/unmerge for install/uninstall (PRD §17.3, P7-D4).

Pure functions on the parsed dict. Byte-for-byte restoration (Gate 7)
comes from the installer recording the file's JSON formatting via
``detect_format`` at install time and re-dumping with it on uninstall.
"""

from __future__ import annotations

import copy
import json

MARKER = "cc-continuity"

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "PostToolUseFailure", "TaskCreated", "TaskCompleted", "Stop",
               "PreCompact", "SessionEnd")

ALLOW_RULES = ("Read(~/.claude-continuity/**)",
               "Write(~/.claude-continuity/**)",
               "Edit(~/.claude-continuity/**)",
               "Bash(cc-continuity *)")
ADDITIONAL_DIRS = ("~/.claude-continuity",)
ENV_KEYS = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS",)
# NOTE (F-P9-4, 2026-09-21 user decision): CLAUDE_CODE_AUTO_COMPACT_WINDOW
# is user-owned and never written or removed by the installer — PCT
# override + ACW together would enable native auto-compact, which the
# design deliberately avoids (rotation + /clear is the only reset).


def our_hook_entry(event: str, launcher: str) -> dict:
    return {"hooks": [{"type": "command",
                       "command": f"{launcher} hook {event}"}]}


def is_ours(entry: dict) -> bool:
    for h in entry.get("hooks", []):
        if isinstance(h, dict) and MARKER in str(h.get("command", "")):
            return True
    return False


def merge_settings(existing: dict, *, launcher: str, window: int) -> tuple[dict, dict]:
    """Merge our entries into a parsed settings dict.

    Returns ``(new_settings, manifest)``; ``existing`` is never mutated.
    Re-running merge over an already-merged dict is safe (our entries are
    replaced, user entries preserved in order).
    """
    s = copy.deepcopy(existing)
    manifest: dict = {
        "env_prev": {k: None for k in ENV_KEYS},
        "allow_added": [],
        "dirs_added": [],
        "previous_status_line": None,
        "status_line_changed": False,
    }
    hooks = s.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        entries = [e for e in hooks.get(event, []) if not is_ours(e)]
        entries.append(our_hook_entry(event, launcher))
        hooks[event] = entries
    sl = s.get("statusLine")
    if isinstance(sl, dict) and sl.get("command") and \
            MARKER not in str(sl.get("command", "")):
        manifest["previous_status_line"] = copy.deepcopy(sl)
        manifest["status_line_changed"] = True
    s["statusLine"] = {"type": "command", "command": f"{launcher} statusline"}
    env = s.setdefault("env", {})
    for key in ENV_KEYS:
        manifest["env_prev"][key] = env.get(key)
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
    perms = s.setdefault("permissions", {})
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow
    for rule in ALLOW_RULES:
        if rule not in allow:
            allow.append(rule)
            manifest["allow_added"].append(rule)
    dirs = perms.get("additionalDirectories")
    if not isinstance(dirs, list):
        dirs = []
        perms["additionalDirectories"] = dirs
    for d in ADDITIONAL_DIRS:
        if d not in dirs:
            dirs.append(d)
            manifest["dirs_added"].append(d)
    return s, manifest


def unmerge_settings(existing: dict, manifest: dict) -> dict:
    """Inverse of ``merge_settings`` (manifest-driven, P7-D12)."""
    s = copy.deepcopy(existing)
    hooks = s.get("hooks", {})
    for event in HOOK_EVENTS:
        entries = hooks.get(event)
        if isinstance(entries, list):
            rest = [e for e in entries if not is_ours(e)]
            if rest:
                hooks[event] = rest
            else:
                hooks.pop(event, None)
    if not hooks:
        s.pop("hooks", None)
    # statusLine: merge always set it to ours, so unmerge restores the
    # previous line only when the manifest recorded a foreign one;
    # otherwise (fresh file, or already ours on reinstall) ours is removed
    if manifest.get("status_line_changed") and \
            manifest.get("previous_status_line") is not None:
        s["statusLine"] = copy.deepcopy(manifest["previous_status_line"])
    else:
        s.pop("statusLine", None)
    env = s.get("env")
    if isinstance(env, dict):
        for key, prev in (manifest.get("env_prev") or {}).items():
            if prev is None:
                env.pop(key, None)
            else:
                env[key] = prev
        if not env:
            s.pop("env", None)
    perms = s.get("permissions")
    if isinstance(perms, dict):
        for key, field in (("allow", "allow_added"),
                           ("additionalDirectories", "dirs_added")):
            lst = perms.get(key)
            added = manifest.get(field) or []
            if isinstance(lst, list) and added:
                rest = [x for x in lst if x not in added]
                if rest:
                    perms[key] = rest
                else:
                    perms.pop(key, None)
        if not perms:
            s.pop("permissions", None)
    return s


def detect_format(raw: bytes) -> dict | None:
    """json.dumps kwargs that reproduce ``raw`` byte-for-byte, else None."""
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    for indent in (2, 4):
        for ensure_ascii in (False, True):
            base = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
            for trail in (True, False):
                cand = base + "\n" if trail else base
                if cand.encode("utf-8") == raw:
                    return {"indent": indent, "ensure_ascii": ensure_ascii,
                            "trailing_newline": trail}
    return None


def dump_settings(obj: dict, fmt: dict | None) -> str:
    if fmt is None:
        fmt = {"indent": 2, "ensure_ascii": False, "trailing_newline": True}
    s = json.dumps(obj, indent=fmt["indent"], ensure_ascii=fmt["ensure_ascii"])
    return s + "\n" if fmt["trailing_newline"] else s
