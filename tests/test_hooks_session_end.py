"""hooks/session_end.py: reason ledger entry + ROTATING/clear anomaly log.

PRD §9: ledger entry with reason; if reason == ``clear`` while state is
``ROTATING``, log it (successor confirmation happens at SessionStart, Phase 6).
"""

import json

from cc_continuity import ledger, paths, state

from hookutil import fixture_payload, make_repo, payload, register_session, run_hook


def _to_rotating(key, nnn):
    for target in ("ACTIVE", "WARNED", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                   "CHECKPOINT_WRITING", "CHECKPOINT_VERIFIED", "ROTATING"):
        state.transition(key, nnn, target)


def test_fixture_session_end_entry(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("SessionEnd", fixture_payload("SessionEnd", repo))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "session_end"
    assert entry["reason"] == "other"  # fixture's recorded reason


def test_clear_while_rotating_is_logged_to_events_log(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    _to_rotating(key, nnn)
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="clear"))
    assert ledger.read(paths.session_dir(key, nnn))[-1]["reason"] == "clear"

    events = cc_home / "logs" / "events.log"
    assert events.is_file()
    assert "ROTATING" in events.read_text()


def test_clear_while_not_rotating_is_not_logged(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert state.load_state(key, nnn)["state"] == "ACTIVE"  # Phase 5 D3
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="clear"))
    assert not (cc_home / "logs" / "events.log").exists()


def test_unregistered_session_is_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = payload("SessionEnd", repo, session_id="no-such-session", reason="clear")
    assert run_hook("SessionEnd", p) == 0
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()


def test_session_end_pushes_row10(cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    repo = make_repo(tmp_path)
    (cc_home / "config.json").write_text(json.dumps({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")
    register_session(repo)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, body)) or True)
    p = fixture_payload("SessionEnd", repo)
    p["reason"] = "clear"
    assert run_hook("SessionEnd", p) == 0
    assert sent == [("■ cc-continuity: session ended",
                     "proj=spike-repo 001 reason=clear")]


# ------------------- Phase 12: progress pane cleanup on SessionEnd
#
# Mode 1 owns a 1-line tmux pane per CC WINDOW. SessionEnd kills the pane
# for THIS window (window id resolved from $TMUX_PANE) and drops its
# record entry. No $TMUX_PANE (or no entry) -> no-op. Fully wrapped: the
# hook must never fail because of it.

import json as _json
import os as _os

import pytest

from cc_continuity import progress_pane

_FAKE_TMUX_WIN = """#!/bin/sh
printf '%s\\n' "---" "$@" >> "${FAKE_TMUX_LOG:?}"
for a in "$@"; do
  if [ "$a" = "#{window_id}" ]; then
    printf '@7\\n'
    exit 0
  fi
done
exit 0
"""


@pytest.fixture
def fake_tmux_win(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(_FAKE_TMUX_WIN)
    _os.chmod(bin_path, 0o755)
    log = tmp_path / "tmux-args.log"
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    return log


def _seed_pane_record(cc_home, entry):
    p = paths.runtime_dir() / "progress-pane.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps({"@7": entry}), encoding="utf-8")


def _tmux_log(path) -> str:
    """The fake-tmux arg log; empty string when the fake was never called."""
    return path.read_text() if path.exists() else ""


def test_session_end_kills_progress_pane_and_drops_entry(
        cc_home, tmp_path, monkeypatch, fake_tmux_win):
    repo = make_repo(tmp_path)
    register_session(repo)
    _seed_pane_record(cc_home, {"pane_id": "%31", "ts": 1.0})
    data = paths.runtime_dir() / "progress-data-@7.json"
    data.write_text(_json.dumps({"text": "◆ 61% Active Usage", "ts": 1.0}),
                    encoding="utf-8")
    monkeypatch.setenv("TMUX_PANE", "%5")
    run_hook("SessionEnd", payload("SessionEnd", repo))
    log = _tmux_log(fake_tmux_win)
    assert "kill-pane" in log
    assert "%31" in log
    # record entry dropped; the window's data file is KEPT — the pane is
    # dead anyway, and a dropped file leaves a recreated pane on the
    # "⟳ …" placeholder until the next render (none comes while the
    # model is down). A new session's pane sees at most ~2 s of stale
    # text before its first render overwrites.
    rec = _json.loads(
        (paths.runtime_dir() / "progress-pane.json").read_text())
    assert "@7" not in rec
    assert data.exists()


def test_session_end_no_tmux_pane_is_noop(cc_home, tmp_path,
                                          monkeypatch, fake_tmux_win):
    repo = make_repo(tmp_path)
    register_session(repo)
    _seed_pane_record(cc_home, {"pane_id": "%31", "ts": 1.0})
    monkeypatch.delenv("TMUX_PANE", raising=False)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    log = _tmux_log(fake_tmux_win)
    assert "kill-pane" not in log
    rec = _json.loads(
        (paths.runtime_dir() / "progress-pane.json").read_text())
    assert rec == {"@7": {"pane_id": "%31", "ts": 1.0}}


def test_session_end_window_not_in_record_is_noop(
        cc_home, tmp_path, monkeypatch, fake_tmux_win):
    repo = make_repo(tmp_path)
    register_session(repo)
    # record only knows window @9; this pane is in window @7
    _seed_pane_record(cc_home, {"pane_id": "%31", "ts": 1.0})
    (paths.runtime_dir() / "progress-pane.json").write_text(
        _json.dumps({"@9": {"pane_id": "%99", "ts": 1.0}}),
        encoding="utf-8")
    monkeypatch.setenv("TMUX_PANE", "%5")
    run_hook("SessionEnd", payload("SessionEnd", repo))
    log = _tmux_log(fake_tmux_win)
    assert "kill-pane" not in log


def test_session_end_drops_only_own_server_entry(
        cc_home, tmp_path, monkeypatch, fake_tmux_win):
    # two tmux servers both own a window @7: SessionEnd must kill and
    # drop only THIS server's entry — never the other server's pane
    repo = make_repo(tmp_path)
    register_session(repo)
    (paths.runtime_dir() / "progress-pane.json").write_text(
        _json.dumps({"s1111@7": {"pane_id": "%31", "ts": 1.0},
                     "s2222@7": {"pane_id": "%32", "ts": 1.0}}),
        encoding="utf-8")
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("TMUX", "/tmp/srv,1111,0")
    run_hook("SessionEnd", payload("SessionEnd", repo))
    log = _tmux_log(fake_tmux_win)
    assert "kill-pane" in log
    assert "%31" in log
    assert "%32" not in log  # the other server's pane must survive
    rec = _json.loads(
        (paths.runtime_dir() / "progress-pane.json").read_text())
    assert "s1111@7" not in rec
    assert rec["s2222@7"] == {"pane_id": "%32", "ts": 1.0}
