"""settings_merge.py: PRD §17.3 merge/unmerge, byte-for-byte restoration."""

import json

from cc_continuity import settings_merge as sm

NTFY = "curl -s https://ntfy.sh/example-topic"
LAUNCHER = "/Users/test/.local/bin/cc-continuity"
HUD = {"type": "command", "command": "~/.claude/bin/claude-hud-launch.sh"}


def seed() -> dict:
    return {
        "env": {
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
        },
        "statusLine": dict(HUD),
        "hooks": {
            "Notification": [
                {"matcher": "permission_prompt",
                 "hooks": [{"type": "command", "command": NTFY}]},
            ],
            "Stop": [
                {"matcher": "", "hooks": [{"type": "command", "command": NTFY}]},
            ],
        },
        "theme": "dark",
    }


def ours_per_event(settings: dict) -> dict[str, int]:
    out = {}
    for event, entries in settings.get("hooks", {}).items():
        out[event] = sum(1 for e in entries if sm.is_ours(e))
    return out


def test_merge_realistic_seed():
    s, m = sm.merge_settings(seed(), launcher=LAUNCHER, window=262144)
    counts = ours_per_event(s)
    # Notification is a FOREIGN event (not in HOOK_EVENTS): preserved as-is,
    # no ours entry — compare only our events
    assert {e: counts[e] for e in sm.HOOK_EVENTS} == \
        {e: 1 for e in sm.HOOK_EVENTS}
    # user entries untouched, ours appended AFTER them
    assert s["hooks"]["Notification"][0]["hooks"][0]["command"] == NTFY
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"] == NTFY
    assert s["statusLine"] == {"type": "command", "command": f"{LAUNCHER} statusline"}
    assert s["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in s["env"]  # user-owned (F-P9-4)
    assert s["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "95"  # untouched
    assert s["permissions"]["allow"] == list(sm.ALLOW_RULES)
    assert s["permissions"]["additionalDirectories"] == ["~/.claude-continuity"]
    assert s["theme"] == "dark"  # foreign keys survive
    assert m["previous_status_line"] == HUD
    assert m["status_line_changed"] is True
    assert m["env_prev"] == {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144"}


def test_merge_fresh_empty_settings():
    s, m = sm.merge_settings({}, launcher=LAUNCHER, window=40000)
    assert set(s) == {"hooks", "statusLine", "env", "permissions"}
    assert len(s["hooks"]) == 10
    assert s["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "40000"
    assert m["previous_status_line"] is None
    assert m["status_line_changed"] is False


def test_merge_twice_no_duplicates():
    once, _ = sm.merge_settings(seed(), launcher=LAUNCHER, window=262144)
    twice, _ = sm.merge_settings(once, launcher=LAUNCHER, window=262144)
    counts = ours_per_event(twice)
    assert {e: counts[e] for e in sm.HOOK_EVENTS} == \
        {e: 1 for e in sm.HOOK_EVENTS}


def test_reinstall_updates_launcher_path():
    a, _ = sm.merge_settings(seed(), launcher=LAUNCHER, window=262144)
    b, _ = sm.merge_settings(a, launcher=LAUNCHER + "-moved", window=262144)
    for event in sm.HOOK_EVENTS:
        ours = [e for e in b["hooks"][event] if sm.is_ours(e)]
        assert len(ours) == 1
        assert ours[0]["hooks"][0]["command"] == \
            f"{LAUNCHER}-moved hook {event}"
    assert b["statusLine"]["command"] == f"{LAUNCHER}-moved statusline"
    counts = ours_per_event(b)
    assert {e: counts[e] for e in sm.HOOK_EVENTS} == \
        {e: 1 for e in sm.HOOK_EVENTS}


def test_unmerge_reverses_merge():
    orig = seed()
    merged, m = sm.merge_settings(orig, launcher=LAUNCHER, window=262144)
    assert sm.unmerge_settings(merged, m) == orig


def test_unmerge_fresh_roundtrip_is_empty():
    merged, m = sm.merge_settings({}, launcher=LAUNCHER, window=262144)
    assert sm.unmerge_settings(merged, m) == {}


def test_unmerge_env_prev_none_removes_key():
    merged, m = sm.merge_settings({}, launcher=LAUNCHER, window=262144)
    out = sm.unmerge_settings(merged, m)
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in out.get("env", {})


def test_merge_preserves_user_acw():
    """F-P9-4 (2026-09-21 user decision): the installer never writes or
    removes CLAUDE_CODE_AUTO_COMPACT_WINDOW — it is user-owned. PCT
    override + ACW together would enable native auto-compact, which the
    design deliberately avoids (rotation + /clear is the only reset)."""
    orig = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "30000"}}
    merged, m = sm.merge_settings(orig, launcher=LAUNCHER, window=262144)
    assert merged["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "30000"
    assert m["env_prev"] == {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": None}
    out = sm.unmerge_settings(merged, m)
    assert out["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "30000"


def test_unmerge_env_prev_value_restored():
    orig = {"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "123456"}}
    merged, m = sm.merge_settings(orig, launcher=LAUNCHER, window=262144)
    out = sm.unmerge_settings(merged, m)
    assert out["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "123456"


def test_unmerge_removes_statusline_when_absent_before():
    merged, m = sm.merge_settings({}, launcher=LAUNCHER, window=262144)
    assert "statusLine" not in sm.unmerge_settings(merged, m)


def test_is_ours_detection():
    assert sm.is_ours({"hooks": [{"type": "command",
                                  "command": "/x/cc-continuity hook Stop"}]})
    assert not sm.is_ours({"hooks": [{"type": "command", "command": NTFY}]})
    assert not sm.is_ours({"hooks": []})


def test_merge_does_not_mutate_input():
    orig = seed()
    snap = json.loads(json.dumps(orig))
    sm.merge_settings(orig, launcher=LAUNCHER, window=262144)
    assert orig == snap


RAW_USER_STYLE = (json.dumps(seed(), indent=2, ensure_ascii=False) + "\n").encode()


def test_detect_format_user_real_style():
    assert sm.detect_format(RAW_USER_STYLE) == {
        "indent": 2, "ensure_ascii": False, "trailing_newline": True}


def test_detect_format_variants():
    # non-ASCII content so ensure_ascii is observable (for pure-ASCII input
    # both settings dump identically and the flag is not detectable)
    # non-personal Unicode case (UTF-8 handling)
    obj = {"a": "ñ", "b": [1, 2]}
    for indent in (2, 4):
        for ea in (False, True):
            raw = json.dumps(obj, indent=indent, ensure_ascii=ea).encode()
            fmt = sm.detect_format(raw)
            assert fmt == {"indent": indent, "ensure_ascii": ea,
                           "trailing_newline": False}
            raw_nl = raw + b"\n"
            assert sm.detect_format(raw_nl) == {
                "indent": indent, "ensure_ascii": ea, "trailing_newline": True}


def test_detect_format_invalid_json():
    assert sm.detect_format(b"not json") is None


def test_dump_settings_none_uses_proven_default():
    assert sm.dump_settings({"x": 1}, None) == json.dumps(
        {"x": 1}, indent=2, ensure_ascii=False) + "\n"


def test_byte_roundtrip_full_cycle():
    """Gate 7's core guarantee: install then uninstall == original bytes."""
    raw = RAW_USER_STYLE
    obj = json.loads(raw)
    fmt = sm.detect_format(raw)
    merged, m = sm.merge_settings(obj, launcher=LAUNCHER, window=262144)
    unmerged = sm.unmerge_settings(merged, m)
    assert sm.dump_settings(unmerged, fmt).encode() == raw
