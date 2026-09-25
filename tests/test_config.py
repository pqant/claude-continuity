"""config.py: PRD §15 defaults + global/project merge + CC_CONTINUITY=0."""

import json
import subprocess

import pytest

from cc_continuity import config


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, env={**__import__("os").environ,
                                             "GIT_CONFIG_GLOBAL": "/dev/null",
                                             "GIT_CONFIG_SYSTEM": "/dev/null"})


def test_defaults_match_prd_section_15():
    d = config.DEFAULTS
    assert d["enabled"] is True
    assert d["window_tokens"] == 262144
    assert d["warn_ratio"] == 0.55
    assert d["rotate_ratio"] == 0.62
    assert d["hard_ratio"] == 0.72
    assert d["full_auto"] is True
    assert d["settle_ms"] == 1500
    assert d["git_snapshot"] is True
    assert d["snapshot_retention"] == 20
    assert d["handoff_max_words"] == 5000
    assert d["progress_type"] == 0
    assert d["inline_handoff_max_chars"] == 12000
    assert d["notify_command"] is None
    assert d["log_level"] == "info"
    assert len(d["redact_patterns"]) == 3
    assert "sk-[A-Za-z0-9]{20,}" in d["redact_patterns"]


def test_no_files_gives_pure_defaults(cc_home, tmp_path, monkeypatch):
    # With no file setting window_tokens the env var would otherwise apply.
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    assert config.load_config(cwd=tmp_path) == config.DEFAULTS


def test_global_config_merges_over_defaults(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({"warn_ratio": 0.5}))
    cfg = config.load_config(cwd=tmp_path)
    assert cfg["warn_ratio"] == 0.5
    assert cfg["hard_ratio"] == 0.72  # untouched default survives


def test_project_override_wins_over_global(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({"warn_ratio": 0.5}))
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "continuity.json").write_text(json.dumps({"warn_ratio": 0.45}))
    assert config.load_config(cwd=proj)["warn_ratio"] == 0.45


def test_project_file_at_git_root_found_from_subdir(cc_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "continuity.json").write_text(json.dumps({"rotate_ratio": 0.6}))
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert config.load_config(cwd=sub)["rotate_ratio"] == 0.6
    assert config.load_config(cwd=repo)["rotate_ratio"] == 0.6


def test_non_git_parent_config_not_picked_up(cc_home, tmp_path):
    parent = tmp_path / "a"
    child = tmp_path / "a" / "b"
    (parent / ".claude").mkdir(parents=True)
    (parent / ".claude" / "continuity.json").write_text(json.dumps({"warn_ratio": 0.1}))
    child.mkdir(parents=True)
    # Not in git → only the cwd itself is searched.
    assert config.load_config(cwd=child)["warn_ratio"] == 0.55
    (child / ".claude").mkdir()
    (child / ".claude" / "continuity.json").write_text(json.dumps({"warn_ratio": 0.2}))
    assert config.load_config(cwd=child)["warn_ratio"] == 0.2


def test_cc_continuity_0_disables(monkeypatch, cc_home, tmp_path):
    cfg = config.load_config(cwd=tmp_path)
    monkeypatch.setenv("CC_CONTINUITY", "0")
    assert config.is_enabled(cfg) is False
    monkeypatch.setenv("CC_CONTINUITY", "1")
    assert config.is_enabled(cfg) is True


def test_enabled_false_in_config_wins_over_env(monkeypatch, cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({"enabled": False}))
    monkeypatch.delenv("CC_CONTINUITY", raising=False)
    assert config.is_enabled(config.load_config(cwd=tmp_path)) is False


def test_invalid_global_json_raises_config_error(cc_home, tmp_path):
    (cc_home / "config.json").write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.load_config(cwd=tmp_path)


def test_invalid_project_json_raises_config_error(cc_home, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "continuity.json").write_text("nope")
    with pytest.raises(config.ConfigError):
        config.load_config(cwd=proj)


def test_defaults_have_phase9_keys():
    assert config.DEFAULTS["notify_endpoint"] == ""
    assert config.DEFAULTS["notify_enabled"] is False
    assert config.DEFAULTS["unattended"] is False


def test_load_config_merges_notify_and_unattended(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic",
        "unattended": True}), encoding="utf-8")
    cfg = config.load_config(cwd=tmp_path)
    assert cfg["notify_enabled"] is True
    assert cfg["notify_endpoint"] == "https://ntfy.sh/example-topic"
    assert cfg["unattended"] is True


def test_defaults_have_phase10_keys():
    assert config.DEFAULTS["watchdog_enabled"] is False
    assert config.DEFAULTS["watchdog_health_url"] == ""
    assert config.DEFAULTS["watchdog_interval_s"] == 300
    assert config.DEFAULTS["watchdog_stale_minutes"] == 45
    assert config.DEFAULTS["watchdog_max_nudges"] == 3
    assert config.DEFAULTS["watchdog_nudge_text"] == "continue"
    # P10 (network recovery): on a failed probe the daemon re-establishes the
    # DGX route (macOS root wrapper, passwordless sudo) before re-probing.
    # Empty string disables the step.
    assert config.DEFAULTS["watchdog_network_recovery_cmd"] == \
        "sudo -n /usr/local/libexec/dgx-network-recovery"


def test_load_config_merges_watchdog_keys(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({
        "watchdog_enabled": True,
        "watchdog_health_url": "http://127.0.0.1:18000/health",
        "watchdog_interval_s": 60,
        "watchdog_stale_minutes": 30}), encoding="utf-8")
    cfg = config.load_config(cwd=tmp_path)
    assert cfg["watchdog_enabled"] is True
    assert cfg["watchdog_health_url"] == "http://127.0.0.1:18000/health"
    assert cfg["watchdog_interval_s"] == 60
    assert cfg["watchdog_stale_minutes"] == 30
    # unset keys keep defaults
    assert cfg["watchdog_max_nudges"] == 3
    assert cfg["watchdog_nudge_text"] == "continue"
    assert cfg["watchdog_network_recovery_cmd"] == \
        "sudo -n /usr/local/libexec/dgx-network-recovery"


# --------------------------------------------------- progress_type (Phase 12)
# Config key: 0 = statusline shimmer (default / current behavior),
# 1 = separate tmux pane (true 50 ms), 2 = statusline "breathing".
# Anything missing or invalid degrades to 0 = today's look.


def test_progress_type_defaults_to_zero():
    assert config.DEFAULTS["progress_type"] == 0


def test_progress_mode_passthrough_valid_values():
    for v in (0, 1, 2):
        assert config.progress_mode({"progress_type": v}) == v


def test_progress_mode_out_of_range_falls_back_to_zero():
    assert config.progress_mode({"progress_type": 5}) == 0
    assert config.progress_mode({"progress_type": -1}) == 0


def test_progress_mode_non_int_falls_back_to_zero():
    assert config.progress_mode({"progress_type": "1"}) == 0
    assert config.progress_mode({"progress_type": None}) == 0


def test_progress_mode_missing_key_falls_back_to_zero():
    assert config.progress_mode({}) == 0


def test_load_config_progress_type_merges(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({"progress_type": 2}),
                                         encoding="utf-8")
    cfg = config.load_config(cwd=tmp_path)
    assert cfg["progress_type"] == 2
    assert config.progress_mode(cfg) == 2


# ------------------------------------------------- power plug (Phase 13)
# The HS100 sits on the DGX power path; the IP is a placeholder default the
# user overrides in config.json once the real plug LAN IP is known.


def test_defaults_have_phase13_power_plug_keys():
    assert config.DEFAULTS["power_plug_ip"] == "192.0.2.99"
    assert config.DEFAULTS["power_plug_port"] == 9999
    assert config.DEFAULTS["power_cycle_off_s"] == 8


def test_load_config_merges_power_plug_keys(cc_home, tmp_path):
    (cc_home / "config.json").write_text(json.dumps({
        "power_plug_ip": "192.0.2.77",
        "power_cycle_off_s": 20}), encoding="utf-8")
    cfg = config.load_config(cwd=tmp_path)
    assert cfg["power_plug_ip"] == "192.0.2.77"
    assert cfg["power_cycle_off_s"] == 20
    assert cfg["power_plug_port"] == 9999  # untouched default survives
