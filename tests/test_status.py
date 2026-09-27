"""cli `status [--json]` (PRD §16): current project/session summary.

Current session resolution: ``CC_CONTINUITY_SESSION`` (written by the
SessionStart hook via CLAUDE_ENV_FILE, S11) wins; otherwise the most recent
``runtime/*.json`` whose ``project_dir`` matches the resolved project root.
"""

import json
import re
import time

import pytest

from cc_continuity import cli, project, state, statusline

SID_A = "f4dcbb38-9b1a-4fa0-b76a-bf35625e09e1"
SID_B = "00000000-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Deterministic env: pin window to the built-in default, no tmux/session."""
    monkeypatch.delenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("CC_CONTINUITY_SESSION", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


def _metric(session_id, project_dir, ts=None, used=12345, window=262144):
    return {
        "session_id": session_id,
        "ts": ts if ts is not None else time.time(),
        "used_tokens": used,
        "window_tokens": window,
        "ratio": round(used / window, 4),
        "project_dir": str(project_dir),
    }


def _setup(tmp_path, name="proj-001", nnn=1, session_id=SID_A,
           states=("ACTIVE",), metric=True, metric_ts=None):
    """Register the project, create a session record, write its metric."""
    root = project.resolve_root(tmp_path)
    key = project.project_key_for_root(root)
    project.ensure_project(tmp_path)
    state.create_session_state(key, name, nnn, session_id)
    for s in states:
        state.transition(key, nnn, s)
    if metric:
        statusline.write_metric(_metric(session_id, root, ts=metric_ts))
    return key, root


def test_status_json_full_session(cc_home, tmp_path):
    key, root = _setup(tmp_path)
    data = cli.collect_status(cwd=tmp_path)
    assert data["project_key"] == key
    assert data["project_root"] == str(root)
    assert data["registered"] is True
    assert data["session"]["name"] == "proj-001"
    assert data["session"]["nnn"] == 1
    assert data["session"]["session_id"] == SID_A
    assert data["session"]["state"] == "ACTIVE"
    assert data["used_tokens"] == 12345
    assert data["window_tokens"] == 262144
    assert abs(data["ratio"] - 12345 / 262144) < 1e-4
    assert data["metric_stale"] is False
    assert data["next_name"] == "proj-002"
    assert data["latest_checkpoint"] is None
    assert data["tmux"] is False
    assert data["thresholds"] == {"warn_ratio": 0.55, "rotate_ratio": 0.62,
                                  "hard_ratio": 0.72}


def test_status_json_unregistered_project_with_metric(cc_home, tmp_path):
    # Statusline writes metrics even before Phase 3 registers the project.
    root = project.resolve_root(tmp_path)
    statusline.write_metric(_metric(SID_A, root))
    data = cli.collect_status(cwd=tmp_path)
    assert data["registered"] is False
    assert data["session"] is None
    assert data["ratio"] is not None  # metric is still reported
    assert data["next_name"] is None


def test_status_stale_metric_flagged(cc_home, tmp_path):
    _setup(tmp_path, metric_ts=time.time() - 700)  # > 10 minutes
    data = cli.collect_status(cwd=tmp_path)
    assert data["metric_stale"] is True
    assert data["ratio"] is not None  # stale values are shown, flagged


def test_status_no_metric_is_stale_unknown(cc_home, tmp_path):
    _setup(tmp_path, metric=False)
    data = cli.collect_status(cwd=tmp_path)
    assert data["ratio"] is None
    assert data["metric_stale"] is True


def test_status_env_var_selects_session_over_newest_runtime(cc_home, tmp_path,
                                                            monkeypatch):
    key, root = _setup(tmp_path, nnn=1, session_id=SID_A,
                       metric_ts=time.time() - 100)
    state.create_session_state(key, "proj-002", 2, SID_B)
    state.transition(key, 2, "ACTIVE")
    statusline.write_metric(_metric(SID_B, root))  # newer than A's
    # Without the env var (autouse fixture cleared it), newest runtime wins.
    assert cli.collect_status(cwd=tmp_path)["session"]["session_id"] == SID_B
    # …but the explicit SessionStart env var selects A.
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID_A)
    data = cli.collect_status(cwd=tmp_path)
    assert data["session"]["session_id"] == SID_A
    assert data["session"]["name"] == "proj-001"


def test_status_tmux_flag(cc_home, tmp_path, monkeypatch):
    _setup(tmp_path)
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
    assert cli.collect_status(cwd=tmp_path)["tmux"] is True


def test_cli_status_human_readable(cc_home, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "proj-001" in out
    assert "ACTIVE" in out
    assert "4.7%" in out  # 12345/262144, one decimal
    assert "proj-002" in out
    assert "55%" in out and "62%" in out and "72%" in out


def test_cli_status_json_flag(cc_home, tmp_path, monkeypatch, capsys):
    _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session"]["name"] == "proj-001"
    assert data["project_key"]  # non-empty key


def test_cli_status_unregistered_prints_hint(cc_home, tmp_path, monkeypatch,
                                              capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "no cc-continuity data" in out.lower()


def test_cli_statusline_reads_stdin_and_prints_line(cc_home, tmp_path,
                                                     monkeypatch, capsys):
    payload = {
        "session_id": SID_A,
        "workspace": {"project_dir": str(tmp_path)},
        "context_window": {
            "total_input_tokens": 158234,
            "context_window_size": 262144,
            "current_usage": {
                "input_tokens": 158234, "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "used_percentage": 60,
        },
        "version": "2.1.209",
    }
    monkeypatch.setattr("sys.stdin", _text_stdin(json.dumps(payload)))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["statusline"]) == 0
    out = capsys.readouterr().out
    # Phase 11: the CLI line is state-colored by default — assert the
    # contract on the SGR-stripped text (format-agnostic).
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert "◆ 61% Active Usage" in plain  # ceil(158234/262144*100) = 61


def test_cli_statusline_garbage_stdin_exits_zero(cc_home, tmp_path,
                                                  monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _text_stdin("not json at all"))
    assert cli.main(["statusline"]) == 0
    assert "◆ …" in capsys.readouterr().out


def _text_stdin(text):
    import io
    return io.StringIO(text)
