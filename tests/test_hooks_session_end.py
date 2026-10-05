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


def test_session_end_reason_resume_marks_session_off(cc_home, tmp_path):
    # regression 2026-09-27 047: `claude --continue` first registers a new
    # startup sid, then abandons it when the process resumes the earlier
    # conversation — the abandoned startup must be switched off, else it
    # lingers as a live zombie (ACTIVE, fresh ledger, same pane) that
    # watchdog/digest scans keep treating as a real candidate
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert state.load_state(key, nnn)["off"] is False
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="resume"))
    rec = state.load_state(key, nnn)
    assert rec["off"] is True
    assert rec["state"] == "ACTIVE"  # off flag only — no state transition


def test_session_end_other_reasons_do_not_mark_off(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("SessionEnd", payload("SessionEnd", repo, reason="clear"))
    assert state.load_state(key, nnn)["off"] is False


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
    # "◆ …" placeholder until the next render (none comes while the
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


# ------------------- Phase 30: SessionEnd push storm guard
#
# A third-party app (a menu-bar monitor) re-spawns `claude` in the same
# project every ~60 s; CC native auto-resume re-attaches to the last
# conversation and the probe dies seconds later — one SessionEnd push
# per minute, indefinitely (live: probe-004 89 pushes / ~90 min,
# probe-005 74). The ledger entry is ALWAYS appended (audit data); only
# the push is limited: >=3 ends in a 10 m window open a suppression
# window (one event-log line); ends inside the 30 m cooldown are silent;
# an elapsed cooldown re-arms — the next end pushes again and reopens
# the window, so a continuing storm stays visible at 1 push / cooldown.
import time

from cc_continuity import notify as _notify
from cc_continuity.hooks import session_end as se_mod

_STATE_FILE = "push-suppress.json"


def _enable_notify(cc_home):
    (cc_home / "config.json").write_text(_json.dumps({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic"}),
        encoding="utf-8")


def _capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(_notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, body)) or True)
    return sent


def test_session_end_first_two_ends_still_push(cc_home, tmp_path,
                                               monkeypatch):
    # guardrail: below the churn threshold every end pushes as before
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    register_session(repo)
    sent = _capture_sends(monkeypatch)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 2
    assert sent[0] == ("■ cc-continuity: session ended",
                       "proj=spike-repo 001 reason=other")


def test_session_end_third_end_in_window_suppresses_push(cc_home, tmp_path,
                                                         monkeypatch):
    # the live probe storm, ends every ~61 s: the first two push; the
    # third (>=3 in 10 m) opens the suppression window — no push, one
    # event-log line, but the ledger entry is still appended
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    key, nnn = register_session(repo)
    sent = _capture_sends(monkeypatch)
    for _ in range(3):
        run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 2  # the 3rd end's push was suppressed
    ends = [e for e in ledger.read(paths.session_dir(key, nnn))
            if e["type"] == "session_end"]
    assert len(ends) == 3  # audit data is never suppressed
    assert (paths.project_dir(key) / _STATE_FILE).is_file()
    events = cc_home / "logs" / "events.log"
    assert "session_end push suppressed" in events.read_text()


def test_session_end_suppression_cooldown_rearms_push(cc_home, tmp_path,
                                                      monkeypatch):
    # after the cooldown elapses the next end pushes again (a continuing
    # storm stays visible at 1 push / cooldown, not 1 / minute) and
    # reopens the window for the following ends
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    register_session(repo)
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(se_mod, "SESSION_END_PUSH_COOLDOWN_S", 0.2)
    for _ in range(3):
        run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 2  # 3rd suppressed, window open
    time.sleep(0.3)  # cooldown elapsed
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 3  # re-armed: the push goes out
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 3  # window reopened: next end silent again


def test_session_end_ends_outside_window_do_not_count(cc_home, tmp_path,
                                                      monkeypatch):
    # two ends from 12 minutes ago are outside the 10 m churn window:
    # this end is the 1st that counts, so it pushes (no suppression)
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    key, nnn = register_session(repo)
    d = paths.session_dir(key, nnn)
    old = time.time() - 720
    ledger.append(d, {"type": "session_end", "reason": "other", "ts": old})
    ledger.append(d, {"type": "session_end", "reason": "other", "ts": old})
    sent = _capture_sends(monkeypatch)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 1
    assert not (cc_home / "logs" / "events.log").exists()


def test_session_end_corrupt_state_file_self_heals(cc_home, tmp_path,
                                                   monkeypatch):
    # a corrupt push-suppress.json must not break the guard: the 3rd end
    # is still suppressed and the state file is rewritten valid
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    key, nnn = register_session(repo)
    sent = _capture_sends(monkeypatch)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    run_hook("SessionEnd", payload("SessionEnd", repo))
    st = paths.project_dir(key) / _STATE_FILE
    st.write_text("not-json{{", encoding="utf-8")
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 2  # still suppressed
    rec = _json.loads(st.read_text(encoding="utf-8"))
    assert isinstance(rec["ts"], (int, float))
    assert "session_end push suppressed" in (
        cc_home / "logs" / "events.log").read_text()


# ------------------- Phase 30.1: declared-noise projects + window fidelity
#
# The churn guard is behavioral (any app) but cannot NAME a source: the
# SessionEnd payload carries cwd, never the spawning app. What
# cc-continuity CAN name is the PROJECT (cwd -> project key) — and a
# third-party app's probe dir IS its own project (ClaudeBar:
# probe-12f448d3). Config's ``session_end_push_suppress_projects``
# declares such projects: their SessionEnd pushes are suppressed from
# the FIRST end (the churn guard would still let 2 through) and NEVER
# re-arm (a declared noise source stays silent, not 1 push / cooldown);
# each suppression window logs one line.
#
# Also pinned: a suppression window is opened ONCE. Ends inside the
# cooldown must stay silent WITHOUT re-logging or rewriting the state —
# a per-end rewrite would re-arm the window forever and the re-arm push
# (storm stays visible at 1 / cooldown) would never fire during a
# continuous storm.

def _declare_project(cc_home, key):
    (cc_home / "config.json").write_text(_json.dumps({
        "notify_enabled": True,
        "notify_endpoint": "https://ntfy.sh/example-topic",
        "session_end_push_suppress_projects": [key]}),
        encoding="utf-8")


def test_session_end_window_is_not_reset_by_inside_ends(cc_home, tmp_path,
                                                        monkeypatch):
    # ends keep coming through the cooldown: the 4th (inside the window)
    # stays silent — no re-log, no state rewrite; only an ELAPSED window
    # re-arms the push
    repo = make_repo(tmp_path)
    _enable_notify(cc_home)
    key, nnn = register_session(repo)
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(se_mod, "SESSION_END_PUSH_COOLDOWN_S", 2.0)
    for _ in range(4):
        run_hook("SessionEnd", payload("SessionEnd", repo))
        time.sleep(0.15)
    assert len(sent) == 2  # 3rd and 4th suppressed
    events = cc_home / "logs" / "events.log"
    assert events.read_text().count("session_end push suppressed") == 1
    st = paths.project_dir(key) / _STATE_FILE
    opened = _json.loads(st.read_text(encoding="utf-8"))["ts"]
    st.write_text(_json.dumps({"ts": opened - 999, "ends": 3}),
                  encoding="utf-8")  # fake an elapsed window
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert len(sent) == 3  # re-armed: the push goes out


def test_session_end_declared_project_suppresses_from_first_end(
        cc_home, tmp_path, monkeypatch):
    # a project key in session_end_push_suppress_projects: even the
    # FIRST end pushes nothing (churn would let 2 through); the ledger
    # entry is still appended; one log line opens the window
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    _declare_project(cc_home, key)
    sent = _capture_sends(monkeypatch)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert sent == []
    ends = [e for e in ledger.read(paths.session_dir(key, nnn))
            if e["type"] == "session_end"]
    assert len(ends) == 2  # audit data untouched
    events = (cc_home / "logs" / "events.log").read_text()
    assert events.count("session_end push suppressed") == 1
    assert "declared project" in events


def test_session_end_declared_project_rearm_stays_silent(cc_home, tmp_path,
                                                         monkeypatch):
    # unlike the churn re-arm, a declared project NEVER pushes again:
    # after the cooldown the next end re-logs the window but no push
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    _declare_project(cc_home, key)
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(se_mod, "SESSION_END_PUSH_COOLDOWN_S", 0.2)
    run_hook("SessionEnd", payload("SessionEnd", repo))
    time.sleep(0.3)  # window elapsed
    run_hook("SessionEnd", payload("SessionEnd", repo))
    assert sent == []
    events = (cc_home / "logs" / "events.log").read_text()
    assert events.count("session_end push suppressed") == 2
