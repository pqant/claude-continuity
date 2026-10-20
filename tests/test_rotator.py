"""rotator.py: PRD §11 full-auto rotator (fake tmux per PRD §21).

The fake tmux records argv (one arg per line, "---" between calls) and can
run side-effect hook commands — Tasks 7–10 use it to simulate the pane.
"""

import base64
import json
import os
import subprocess
import time

import pytest

from cc_continuity import bootstrap, ledger, lock, paths, rotator, state

from hookutil import (SID, make_repo, payload, register_session, run_hook,
                      set_metric, walk_to)

FAKE_TMUX = """#!/bin/sh
# Log argv base64-encoded, one line per call (0x1f-joined inside the blob):
# the C.8 paste-buffer payload is a single argument WITH newlines, so the
# log must be newline-safe or the parser splits that argument.
{
  printf '%s\\n' "---"
  printf '%s\\037' "$@" | base64 | tr -d '\\n'
  printf '\\n'
} >> "${FAKE_TMUX_LOG:?}"
case "$*" in
  *' /clear'*) if [ -n "${FAKE_CLEAR_HOOK:-}" ]; then eval "$FAKE_CLEAR_HOOK"; fi ;;
  *paste-buffer*) if [ -n "${FAKE_PASTE_HOOK:-}" ]; then eval "$FAKE_PASTE_HOOK"; fi ;;
  *' C-u'*)
    # simulate C-u clearing the input box (verified live on 2.1.209)
    if [ "${FAKE_CU_CLEARS:-1}" = "1" ] && [ -n "${FAKE_PANE_FILE:-}" ]; then
      : > "$FAKE_PANE_FILE"
    fi ;;
  *capture-pane*)
    if [ -n "${FAKE_PANE_FILE:-}" ] && [ -f "${FAKE_PANE_FILE}" ]; then
      cat "$FAKE_PANE_FILE"
    fi ;;
esac
exit 0
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-tmux"
    bin_path.write_text(FAKE_TMUX)
    os.chmod(bin_path, 0o755)
    log = tmp_path / "tmux-args.log"
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", str(bin_path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    return log


def _calls(log) -> list[list[str]]:
    """Parse the fake-tmux log into a list of argv lists (no calls: no log).

    Format: a ``---`` line, then one base64 line holding that call's argv
    joined with ``\\x1f`` (unit separator) — newline-safe, so multi-line
    arguments (the C.8 paste-buffer payload) stay ONE argv element."""
    if not log.is_file():
        return []
    out, cur = [], None
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            if cur is not None:
                out.append(cur)
            cur = []
        elif cur is not None and line.strip():
            parts = base64.b64decode(line).decode("utf-8").split("\x1f")
            if parts[-1] == "":
                parts = parts[:-1]  # trailing separator after the last arg
            cur.extend(parts)
    if cur is not None:
        out.append(cur)
    return out


def _quiet_session(cc_home, tmp_path) -> tuple:
    """Registered session, turn closed: turn_active False + last_stop_ts set."""
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    return repo, key, nnn


# --- tmux helper -------------------------------------------------------------


def test_tmux_helper_uses_env_bin_and_records(fake_tmux):
    assert rotator._tmux("send-keys", "-t", "%9", "-l", "/clear") == 0
    assert _calls(fake_tmux) == [["send-keys", "-t", "%9", "-l", "/clear"]]


def test_tmux_helper_missing_bin_returns_1(cc_home, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_TMUX_BIN", "/nonexistent/tmux")
    assert rotator._tmux("list-sessions") == 1


# --- idle check (PRD §11.3) ---------------------------------------------------


def test_idle_true_when_quiet(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    assert rotator._idle(key, nnn) is True


def test_idle_false_when_turn_active(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    state.update_fields(key, nnn, turn_active=True)
    assert rotator._idle(key, nnn) is False


def test_idle_false_on_main_entry_after_stop(cc_home, tmp_path):
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "user_prompt", "text": "user typed something"})
    assert rotator._idle(key, nnn) is False


def test_idle_ignores_subagent_entries_after_stop(cc_home, tmp_path):
    # P6-D6: a background subagent finishing after the Stop is NOT
    # main-session activity.
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "task_completed", "agent_id": "agent-3",
                   "subject": "background job"})
    assert rotator._idle(key, nnn) is True


def test_idle_ignores_state_bookkeeping_after_stop(cc_home, tmp_path):
    # The Stop hook's own CV->ROTATING transition writes a "state" ledger
    # entry AFTER last_stop_ts — it must not look like activity.
    _, key, nnn = _quiet_session(cc_home, tmp_path)
    state.transition(key, nnn, "ROTATION_PENDING")
    assert rotator._idle(key, nnn) is True


# --- .started marker wait (PRD §11.6) -----------------------------------------


def test_wait_new_started_detects_new_marker(cc_home, monkeypatch):
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    before = rotator._started_names()
    bootstrap.write_started("sid-marker")
    assert rotator._wait_new_started(before) is True


def test_wait_new_started_times_out(cc_home, monkeypatch):
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.1)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    assert rotator._wait_new_started(rotator._started_names()) is False


# --- spawn (P6-D8) -------------------------------------------------------------


def test_spawn_detached_with_cli_command(cc_home, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))
            self.pid = 4242

    monkeypatch.setattr(rotator.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(rotator.shutil, "which",
                        lambda name: "/usr/bin/cc-continuity")
    pid = rotator.spawn("sid-1", "%7", "rid-1")
    assert pid == 4242
    cmd, kw = calls[0]
    assert cmd == ["/usr/bin/cc-continuity", "rotate", "--session", "sid-1",
                   "--pane", "%7", "--rotation-id", "rid-1"]
    assert kw["start_new_session"] is True
    assert kw["stdin"] is subprocess.DEVNULL
    assert "rotator.log" in str(kw["stdout"].name)


def test_command_falls_back_to_python_module(cc_home, monkeypatch):
    monkeypatch.setattr(rotator.shutil, "which", lambda name: None)
    assert rotator._command() == [rotator.sys.executable,
                                  "-m", "cc_continuity"]


def test_spawn_raises_when_popen_fails(cc_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(rotator.subprocess, "Popen", boom)
    with pytest.raises(rotator.RotatorSpawnError):
        rotator.spawn("sid", "%1", "r")


# --- run(): full happy path (PRD §11 steps 1–9, success) ----------------------


def _rotating(cc_home, tmp_path) -> dict:
    """Session 001 in ROTATING with a real checkpoint (the post-Stop shape:
    the Stop hook sets turn_active/last_stop_ts before the ROTATING move)."""
    from cc_continuity import checkpoint, config, project

    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(
        _DRAFT)  # same nine-heading body as tests/test_hooks_stop.py
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    rid = "rid-test"
    state.update_fields(key, nnn, turn_active=False, last_stop_ts=time.time())
    state.transition(key, nnn, "ROTATING", rotation_id=rid)
    cfg = dict(config.DEFAULTS)
    cfg["settle_ms"] = 0
    return {"repo": repo, "key": key, "nnn": nnn, "rid": rid, "cfg": cfg,
            "sid": SID}


_DRAFT = "\n".join(f"## {h}\n{b}\n" for h, b in [
    ("Objective", "Implement the feature end to end in the sandbox repo."),
    ("Current Task and Exact Stopping Point",
     "Just finished the edit; the test run has not happened yet."),
    ("Completed This Session", "- Added the module and its imports."),
    ("Next Atomic Action",
     "Run `pytest -q` and fix any failure that appears."),
    ("Remaining Plan", "1. Tests. 2. Docs."),
    ("Decisions and Rationale", "Kept stdlib only, per the constraint."),
    ("Rejected Approaches — Do Not Repeat", "No third-party dependencies."),
    ("Open Problems, Risks, Questions for the User",
     "None known at the moment."),
    ("Runtime Environment Notes", "No running servers or ports."),
])

_NEW_SID = "99999999-8888-7777-6666-555555555555"


def _shrink_waits(monkeypatch):
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 5.0)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 5.0)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)


def _world_thread(world_fn):
    import threading
    t = threading.Thread(target=world_fn, daemon=True)
    t.start()
    return t


def test_run_happy_path_full_chain(fake_tmux, cc_home, tmp_path, monkeypatch):
    # The pane "model" reacts to the keystrokes by running the REAL hooks
    # in-process: /clear -> SessionStart(clear) claim, paste -> UserPromptSubmit.
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = d["repo"], d["key"], d["nnn"], d["rid"], d["cfg"]
    _shrink_waits(monkeypatch)

    def world():
        time.sleep(0.2)  # let /clear land first
        run_hook("SessionStart", payload("SessionStart", repo,
                                         session_id=_NEW_SID, source="clear"))
        time.sleep(0.2)
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", repo, session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    t.join(timeout=10)
    assert rc == 0
    assert state.load_state(key, 1)["state"] == "ROTATED"
    rec2 = state.load_state(key, 2)
    assert rec2["state"] == "ACTIVE"  # claim BOOTSTRAPPING + RESUME -> ACTIVE
    assert rec2["session_id"] == _NEW_SID
    events = [l for l in (paths.logs_dir() / "events.log")
              .read_text().splitlines() if "ROTATION_COMPLETED" in l]
    assert events
    calls = _calls(fake_tmux)
    joined = [" ".join(c) for c in calls]
    order = [joined.index("send-keys -t %9 C-u"),
             joined.index("send-keys -t %9 -l /clear"),
             joined.index("send-keys -t %9 Enter"),
             joined.index("send-keys -t %9 -l /rename spike-repo-002"),
             joined.index("paste-buffer -p -d -b ccc -t %9")]
    assert order == sorted(order)
    buf = next(c for c in calls if c[:2] == ["set-buffer", "-b"])
    assert buf[3].startswith(f"CONTINUITY RESUME cp-{nnn:03d}: read ")
    assert (paths.runtime_dir() / f"{_NEW_SID}.started").is_file()


def test_run_f14_no_sessionstart_still_succeeds(fake_tmux, cc_home, tmp_path,
                                                 monkeypatch):
    # Only the RESUME paste lands (SessionStart(clear) never fired): the
    # UserPromptSubmit F14 fallback performs the claim (P6-D5).
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = d["repo"], d["key"], d["nnn"], d["rid"], d["cfg"]
    _shrink_waits(monkeypatch)

    def world():
        time.sleep(2.0)  # no SessionStart; let the .started wait expire
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", repo, session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 1.0)
    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    t.join(timeout=10)
    assert rc == 0
    log_text = (paths.logs_dir() / "events.log").read_text()
    assert "SESSIONSTART_MISSING" in log_text
    assert state.load_state(key, 1)["state"] == "ROTATED"
    assert state.load_state(key, 2)["state"] == "ACTIVE"


def test_run_defers_when_activity_after_stop(fake_tmux, cc_home, tmp_path):
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    time.sleep(0.01)
    ledger.append(paths.session_dir(key, nnn),
                  {"type": "user_prompt", "text": "user keeps typing"})
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0  # defer is not an error
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    assert _calls(fake_tmux) == []  # no keystrokes at all
    assert "ROTATION_DEFERRED" in (paths.logs_dir() / "events.log").read_text()


def test_run_exits_when_rotation_lock_held(cc_home, tmp_path):
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    holder = lock.ProjectLock(paths.project_dir(key) / "rotation.lock",
                              timeout=0)
    assert holder.acquire(rotation_id="other-rotator")
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    holder.release()
    assert rc == 2
    assert state.load_state(key, nnn)["state"] == "ROTATING"  # untouched


def test_run_takes_over_stale_rotation_lock(fake_tmux, cc_home, tmp_path,
                                             monkeypatch):
    d = _rotating(cc_home, tmp_path)
    key, nnn, rid, cfg = d["key"], d["nnn"], d["rid"], d["cfg"]
    dead = subprocess.Popen(["true"]).pid
    subprocess.run(["true"])  # ensure the pid is dead
    lpath = paths.project_dir(key) / "rotation.lock"
    lpath.parent.mkdir(parents=True, exist_ok=True)
    lpath.write_text(json.dumps({"pid": dead, "ts": time.time() - 300,
                                 "rotation_id": "dead-rotator"}))
    _shrink_waits(monkeypatch)

    def world():
        time.sleep(0.2)
        run_hook("SessionStart", payload("SessionStart", d["repo"],
                                         session_id=_NEW_SID, source="clear"))
        time.sleep(0.2)
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", d["repo"], session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    t.join(timeout=10)
    assert rc == 0  # stale lock taken over, rotation completed
    assert state.load_state(key, nnn)["state"] == "ROTATED"


def test_run_exits_when_state_not_rotating(cc_home, tmp_path):
    from cc_continuity import checkpoint, config, project
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    (paths.session_dir(key, nnn) / "handoff.draft.md").write_text(_DRAFT)
    checkpoint.finalize(key, nnn, cfg=config.load_config(cwd=repo),
                        root=project.resolve_root(repo))
    # still CHECKPOINT_VERIFIED — the Stop hook never moved it
    cfg = dict(config.DEFAULTS); cfg["settle_ms"] = 0
    rc = rotator.run(key=key, nnn=nnn, session_id=SID, pane="%9",
                     rotation_id="rid-x", cfg=cfg)
    assert rc == 2
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"


# --- input-box protection (live bug 039: /clear appended to typed text) ------
#
# Pane renderings captured from the real Claude Code 2.1.209 input box
# (tmux capture-pane -p): the active line is "❯<NBSP>text" at column 0,
# the first line of a multi-line box is "  ❯ text", each box line sits
# above a long "─" frame line, and the status line follows the frame.


_FRAME = "─" * 72
_STATUS = "  [m | API] │ proj git:(main) │ sess-001 │ ⏱️  1h"
BOX_EMPTY = f"⏺ done\n❯\n{_FRAME}\n{_STATUS}\n"
BOX_SINGLE = f"⏺ done\n❯\xa0hello world\n{_FRAME}\n{_STATUS}\n"
BOX_MULTI = (f"⏺ done\n  ❯ line one\n{_FRAME} sess-001 ──\n"
             f"❯\xa0line two\n{_FRAME}\n{_STATUS}\n")


def test_parse_input_box_empty():
    assert rotator._parse_input_box(BOX_EMPTY) == ""


def test_parse_input_box_single_line():
    assert rotator._parse_input_box(BOX_SINGLE) == "hello world"


def test_parse_input_box_multi_line():
    assert rotator._parse_input_box(BOX_MULTI) == "line one\nline two"


def test_parse_input_box_stops_at_transcript_content():
    # a transcript user line above the assistant reply must NOT leak in
    pane = (f"  ❯ old user message\n⏺ assistant reply\n"
            f"❯\xa0fresh input\n{_FRAME}\n{_STATUS}\n")
    assert rotator._parse_input_box(pane) == "fresh input"


def test_parse_input_box_without_frame_is_empty():
    assert rotator._parse_input_box("just\nrandom\ntranscript\ntext\n") == ""


def test_pending_prompt_roundtrip(cc_home):
    assert (rotator.pending_prompt_path("key-x", 7)
            .name) == "pending-prompt-key-x-007.txt"
    rotator.save_pending_prompt("key-x", 7, "abc")
    assert rotator.read_pending_prompt("key-x", 7) == "abc"
    rotator.delete_pending_prompt("key-x", 7)
    assert rotator.read_pending_prompt("key-x", 7) == ""


def test_c8_pending_note_appended_and_capped():
    base = "CONTINUITY RESUME cp-001: read /x completely"
    out = rotator.c8_with_pending(base, "line a\nline b")
    assert out.startswith(base)
    assert "PENDING USER PROMPT" in out
    assert "line a ⏎ line b" in out
    assert "NOT a new instruction" in out
    capped = rotator.c8_with_pending(base, "x" * 900)
    assert len(capped) < 1200
    assert "truncated" in capped
    assert rotator.c8_with_pending(base, "") == base


def test_clear_pane_saves_in_progress_prompt_then_clears(fake_tmux, cc_home):
    texts = iter([BOX_SINGLE, BOX_EMPTY, BOX_EMPTY])
    rc = rotator._clear_pane("%9", "key-x", 1,
                             capture=lambda p: next(texts))
    assert rc is True
    assert rotator.pending_prompt_path("key-x", 1).read_text(
        encoding="utf-8") == "hello world"
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    i_cu = joined.index("send-keys -t %9 C-u")
    assert i_cu < joined.index("send-keys -t %9 -l /clear") \
        < joined.index("send-keys -t %9 Enter")


def test_clear_pane_empty_box_writes_no_pending_file(fake_tmux, cc_home):
    # 3 captures: initial (pre C-u) + 2 consecutive settle polls
    texts = iter([BOX_EMPTY, BOX_EMPTY, BOX_EMPTY])
    assert rotator._clear_pane("%9", "key-x", 1,
                               capture=lambda p: next(texts)) is True
    assert not rotator.pending_prompt_path("key-x", 1).exists()


def test_clear_pane_defers_when_box_never_settles(fake_tmux, cc_home,
                                                  monkeypatch):
    monkeypatch.setattr(rotator, "CLEAR_SETTLE_S", 0.1)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    rc = rotator._clear_pane("%9", "key-x", 1,
                             capture=lambda p: BOX_SINGLE)
    assert rc is False
    # saved before the C-u, but /clear must never be sent
    assert rotator.pending_prompt_path("key-x", 1).read_text(
        encoding="utf-8") == "hello world"
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    assert "send-keys -t %9 C-u" in joined
    assert not any("/clear" in j for j in joined)


def test_run_defers_when_input_box_busy(fake_tmux, cc_home, tmp_path,
                                        monkeypatch):
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(BOX_SINGLE)
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    monkeypatch.setenv("FAKE_CU_CLEARS", "0")  # user keeps typing
    monkeypatch.setattr(rotator, "CLEAR_SETTLE_S", 0.1)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0  # defer is not an error
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    assert not any("/clear" in j for j in joined)  # never cleared the pane
    assert "ROTATION_DEFERRED" in (paths.logs_dir() / "events.log").read_text()


def test_run_defer_tolerates_bootstrap_rotated(fake_tmux, cc_home, tmp_path,
                                               monkeypatch):
    # Regression 2026-09-27 049/050: bootstrap (the successor's
    # SessionStart/clear claim) sets the old session to ROTATED while the
    # rotator is deciding a defer. ROTATED is terminal, so the defer must
    # NOT raise InvalidTransitionError, must NOT roll back, and returns 0.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]

    def racing_clear(pane, k, n, **kw):
        # the concurrent bootstrap closes the old session, then the box is
        # still busy (take the defer path)
        if state.load_state(k, n)["state"] == "ROTATING":
            state.transition(k, n, "ROTATED")
        return False
    monkeypatch.setattr(rotator, "_clear_pane", racing_clear)

    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0  # a defer is not an error — and neither is the race
    # NOT rolled back: ROTATED is terminal and the rotation already completed
    assert state.load_state(key, nnn)["state"] == "ROTATED"


def test_run_retry_does_not_redrive_when_bootstrap_rotated(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # Attempt 1 sends the continuation (the successor's SessionStart claims
    # -> ROTATED) but _verify_resume misses it, so the retry loop re-enters.
    # Attempt 2 must detect ROTATED and stop, NOT re-drive the pane.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    calls = {"n": 0}

    def clear_once_then_rotated(pane, k, n, **kw):
        calls["n"] += 1
        if calls["n"] == 1 and state.load_state(k, n)["state"] == "ROTATING":
            state.transition(k, n, "ROTATED")  # bootstrap won after attempt 1
        return True
    monkeypatch.setattr(rotator, "_clear_pane", clear_once_then_rotated)
    monkeypatch.setattr(rotator, "_verify_resume",
                        lambda *a, **kw: False)  # force the retry

    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    assert calls["n"] == 1  # attempt 2 did NOT re-drive the pane


def test_run_happy_path_with_pending_prompt_in_c8(fake_tmux, cc_home,
                                                  tmp_path, monkeypatch):
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = d["repo"], d["key"], d["nnn"], d["rid"], d["cfg"]
    _shrink_waits(monkeypatch)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(BOX_MULTI)
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    # FAKE_CU_CLEARS defaults to 1: the fake C-u empties the pane file

    def world():
        time.sleep(0.2)
        run_hook("SessionStart", payload("SessionStart", repo,
                                         session_id=_NEW_SID, source="clear"))
        time.sleep(0.2)
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", repo, session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    t.join(timeout=10)
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    calls = _calls(fake_tmux)
    buf = next(c for c in calls if c[:2] == ["set-buffer", "-b"])
    assert buf[3].startswith(f"CONTINUITY RESUME cp-{nnn:03d}: read ")
    assert "PENDING USER PROMPT" in buf[3]
    assert "line one ⏎ line two" in buf[3]
    assert not rotator.pending_prompt_path(key, nnn).exists()  # consumed


def test_run_exits_on_rotation_id_mismatch(cc_home, tmp_path):
    d = _rotating(cc_home, tmp_path)
    rc = rotator.run(key=d["key"], nnn=d["nnn"], session_id=d["sid"],
                     pane="%9", rotation_id="different-id", cfg=d["cfg"])
    assert rc == 2


# --- run(): failure path (PRD §11.9, §16, §18) ---------------------------------


def test_run_failure_retries_once_notifies_and_stays_claimable(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # Dead pane: the fake tmux records keystrokes but no hook ever fires,
    # so neither SessionStart nor the RESUME prompt ever lands.
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = (d["repo"], d["key"], d["nnn"], d["rid"],
                                dict(d["cfg"]))
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    notify_file = tmp_path / "notify.log"
    notify_sh = tmp_path / "notify.sh"
    notify_sh.write_text(f"#!/bin/sh\necho \"$1 $2\" >> {notify_file}\n")
    os.chmod(notify_sh, 0o755)
    cfg["notify_command"] = str(notify_sh)

    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    assert rc == 1
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    entries = ledger.read(paths.session_dir(key, nnn))
    assert entries[-1] == {**entries[-1], "type": "rotation_failed"}
    calls = _calls(fake_tmux)
    clears = [c for c in calls if c == ["send-keys", "-t", "%9",
                                         "-l", "/clear"]]
    assert len(clears) == 2  # one retry of steps 3–9, then give up
    assert notify_file.read_text().strip() == "spike-repo ROTATION_FAILED"
    log_text = (paths.logs_dir() / "events.log").read_text()
    assert "ROTATION_FAILED" in log_text
    assert "SESSIONSTART_MISSING" in log_text


def test_run_failure_without_notify_command_does_not_raise(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    d = _rotating(cc_home, tmp_path)
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    rc = rotator.run(key=d["key"], nnn=d["nnn"], session_id=d["sid"],
                     pane="%9", rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 1


def test_run_happy_path_pushes_row5(fake_tmux, cc_home, tmp_path, monkeypatch):
    from cc_continuity import notify
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid = d["repo"], d["key"], d["nnn"], d["rid"]
    cfg = dict(d["cfg"], notify_enabled=True,
               notify_endpoint="https://ntfy.sh/example-topic")
    _shrink_waits(monkeypatch)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body)) or True)

    def world():
        time.sleep(0.2)  # let /clear land first
        run_hook("SessionStart", payload("SessionStart", repo,
                                         session_id=_NEW_SID, source="clear"))
        time.sleep(0.2)
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", repo, session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    t.join(timeout=10)
    assert rc == 0
    rows5 = [(p, b) for (tt, p, b) in sent
             if tt == "🔄 cc-continuity: rotation complete"]
    assert len(rows5) == 1
    prio, body = rows5[0]
    assert prio == "high"
    assert f"proj=spike-repo {nnn:03d}→{nnn + 1:03d} ratio " in body
    assert "resumed: " in body


def test_run_final_failure_pushes_row8_once(fake_tmux, cc_home, tmp_path,
                                            monkeypatch):
    from cc_continuity import notify
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid = d["repo"], d["key"], d["nnn"], d["rid"]
    cfg = dict(d["cfg"], notify_enabled=True,
               notify_endpoint="https://ntfy.sh/example-topic")
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    sent = []
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body)) or True)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    assert rc == 1
    rows8 = [(p, b) for (tt, p, b) in sent
             if tt == "✗ cc-continuity: rotation failed"]
    assert len(rows8) == 1  # both failed attempts -> still ONE push (dedup)
    prio, body = rows8[0]
    assert prio == "urgent"
    assert "detail=no RESUME in successor ledger after 2 attempts" in body


# --- paneless self-heal: spawn_successor_detached ------------------------------


def test_paneless_spawn_happy_path(fake_tmux, cc_home, tmp_path, monkeypatch):
    # The "successor" is a real claude in a (fake) tmux pane: its
    # SessionStart(startup) claims the ROTATING project, and the C.8
    # prompt lands in its ledger via UserPromptSubmit.
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = d["repo"], d["key"], d["nnn"], d["rid"], d["cfg"]
    monkeypatch.setattr(rotator, "CLAIM_WAIT_S", 5.0)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 5.0)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    monkeypatch.delenv("CC_CONTINUITY_CLAUDE_BIN", raising=False)

    def world():
        time.sleep(0.2)
        run_hook("SessionStart", payload("SessionStart", repo,
                                         session_id=_NEW_SID,
                                         source="startup"))
        time.sleep(0.2)
        run_hook("UserPromptSubmit", payload(
            "UserPromptSubmit", repo, session_id=_NEW_SID,
            prompt=f"CONTINUITY RESUME cp-{nnn:03d}: read /x completely, "
                   "apply the VALIDATION result, then continue from Next "
                   "Atomic Action. Do not repeat completed work."))

    t = _world_thread(world)
    outcome, info = rotator.spawn_successor_detached(key, nnn, d["sid"],
                                                     cfg, rid)
    t.join(timeout=10)
    assert outcome == "completed"
    assert info["name"] == "spike-repo-002"
    assert info["tmux_session"].startswith("cc-spike-repo-")
    assert state.load_state(key, 1)["state"] == "ROTATED"  # the claim did it
    assert state.load_state(key, 2)["session_id"] == _NEW_SID
    assert (paths.runtime_dir() / f"{_NEW_SID}.started").is_file()
    ns = next(c for c in _calls(fake_tmux) if c[0] == "new-session")
    assert ns[:2] == ["new-session", "-d"]
    assert ns[2:5] == ["-s", info["tmux_session"], "-c"]
    assert ns[5] == str(repo)
    cmd = ns[6]
    assert "CC_CONTINUITY_NAME=spike-repo-002" in cmd
    assert "command claude" in cmd
    assert "-n spike-repo-002" in cmd
    assert "CONTINUITY RESUME cp-001" in cmd


def test_paneless_spawn_defers_when_no_claim(fake_tmux, cc_home, tmp_path,
                                              monkeypatch):
    # No SessionStart ever fires (dead pane): the claim wait expires, the
    # state rolls back to CHECKPOINT_VERIFIED and the next Stop retries.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    monkeypatch.setattr(rotator, "CLAIM_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    outcome, info = rotator.spawn_successor_detached(key, nnn, d["sid"],
                                                     d["cfg"], d["rid"])
    assert outcome == "deferred"
    assert info["name"] == "spike-repo-002"
    assert state.load_state(key, nnn)["state"] == "CHECKPOINT_VERIFIED"
    assert next(c for c in _calls(fake_tmux) if c[0] == "new-session") \
        is not None  # the pane WAS created before the deferral


def test_paneless_spawn_exits_when_lock_held(cc_home, tmp_path):
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    holder = lock.ProjectLock(paths.project_dir(key) / "rotation.lock",
                              timeout=0)
    assert holder.acquire(rotation_id="other-rotator")
    outcome, info = rotator.spawn_successor_detached(key, nnn, d["sid"],
                                                     d["cfg"], d["rid"])
    holder.release()
    assert (outcome, info) == ("exited", None)
    assert state.load_state(key, nnn)["state"] == "ROTATING"  # untouched


def test_paneless_spawn_unattended_adds_skip_permissions(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    d = _rotating(cc_home, tmp_path)
    cfg = dict(d["cfg"], unattended=True)
    monkeypatch.setattr(rotator, "CLAIM_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    monkeypatch.delenv("CC_CONTINUITY_CLAUDE_BIN", raising=False)
    outcome, _info = rotator.spawn_successor_detached(d["key"], d["nnn"],
                                                      d["sid"], cfg, d["rid"])
    assert outcome == "deferred"  # no claim; we only inspect the cmd
    ns = next(c for c in _calls(fake_tmux) if c[0] == "new-session")
    assert "--dangerously-skip-permissions" in ns[6]
