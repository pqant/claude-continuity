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

from hookutil import (SID, InlineEmitSub, make_repo, payload,
                      register_session, run_hook, set_metric, walk_to)

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
    cfg["retry_deferred"] = False  # keep the pre-#53 run() tests hermetic
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


# --- deferred rotation self-retry (#53) ---------------------------------------
# After ROTATION_DEFERRED the rotator arms one detached tick per round:
# RETRY_DELAY_S later it re-checks and fires the rotation itself ONLY when
# the state is still CHECKPOINT_VERIFIED with the same rotation_id (the
# Stop-driven route stays primary; the guard keeps the tick idempotent).


def _deferred(cc_home, tmp_path) -> dict:
    """Post-defer shape: CV keeping the rotation_id, quiet session, and the
    self-retry switched ON."""
    d = _rotating(cc_home, tmp_path)
    state.transition(d["key"], d["nnn"], "CHECKPOINT_VERIFIED")
    d["cfg"] = dict(d["cfg"], retry_deferred=True)
    return d


def test_selfretry_fires_when_idle(fake_tmux, cc_home, tmp_path, monkeypatch):
    d = _deferred(cc_home, tmp_path)
    monkeypatch.setattr(rotator, "RETRY_DELAY_S", 0.0)
    fired = []
    monkeypatch.setattr(rotator, "spawn",
                        lambda sid, pane, rid: fired.append((sid, pane, rid)) or 4242)
    rc = rotator.retry_run(key=d["key"], nnn=d["nnn"], pane="%9",
                           session_id=d["sid"], rotation_id=d["rid"],
                           attempt=1, cfg=d["cfg"])
    assert rc == 0
    rec = state.load_state(d["key"], d["nnn"])
    assert rec["state"] == "ROTATING"
    assert rec["rotation_id"] != d["rid"]           # a fresh rotation_id
    assert fired == [(d["sid"], "%9", rec["rotation_id"])]
    assert "ROTATION_RETRY_FIRED" in (paths.logs_dir() / "events.log").read_text()


def test_selfretry_noop_when_stop_already_rotated(fake_tmux, cc_home,
                                                  tmp_path, monkeypatch):
    d = _deferred(cc_home, tmp_path)
    state.transition(d["key"], d["nnn"], "ROTATING", rotation_id="rid-stop-2")
    monkeypatch.setattr(rotator, "RETRY_DELAY_S", 0.0)
    fired = []
    monkeypatch.setattr(rotator, "spawn",
                        lambda sid, pane, rid: fired.append(rid) or 4242)
    rc = rotator.retry_run(key=d["key"], nnn=d["nnn"], pane="%9",
                           session_id=d["sid"], rotation_id=d["rid"],
                           attempt=1, cfg=d["cfg"])
    assert rc == 0
    assert fired == []
    rec = state.load_state(d["key"], d["nnn"])
    assert rec["state"] == "ROTATING" and rec["rotation_id"] == "rid-stop-2"
    assert "RETRY_NOP" in (paths.logs_dir() / "events.log").read_text()


def test_selfretry_noop_when_rotation_id_changed(fake_tmux, cc_home,
                                                 tmp_path, monkeypatch):
    # A fresh Stop re-armed the rotation (new rotation_id on the CV record);
    # the old tick must stand down without touching state.
    d = _deferred(cc_home, tmp_path)
    state.update_fields(d["key"], d["nnn"], rotation_id="rid-stop-2")
    monkeypatch.setattr(rotator, "RETRY_DELAY_S", 0.0)
    fired = []
    monkeypatch.setattr(rotator, "spawn",
                        lambda sid, pane, rid: fired.append(rid) or 4242)
    rotator.retry_run(key=d["key"], nnn=d["nnn"], pane="%9",
                      session_id=d["sid"], rotation_id=d["rid"],
                      attempt=1, cfg=d["cfg"])
    assert fired == []
    assert state.load_state(d["key"], d["nnn"])["state"] == "CHECKPOINT_VERIFIED"


def test_selfretry_not_idle_reschedules_until_cap(fake_tmux, cc_home,
                                                  tmp_path, monkeypatch):
    d = _deferred(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(d["key"], d["nnn"]),
                  {"type": "user_prompt", "text": "user is active"})
    monkeypatch.setattr(rotator, "RETRY_DELAY_S", 0.0)
    armed = []
    monkeypatch.setattr(rotator, "spawn_retry",
                        lambda *a, **kw: armed.append(a) or 9)
    fired = []
    monkeypatch.setattr(rotator, "spawn", lambda *a: fired.append(a) or 1)
    rc = rotator.retry_run(key=d["key"], nnn=d["nnn"], pane="%9",
                           session_id=d["sid"], rotation_id=d["rid"],
                           attempt=1, cfg=d["cfg"])
    assert rc == 0 and fired == []
    assert armed and armed[0][5] == 2               # attempt+1 respawn
    assert state.load_state(d["key"], d["nnn"])["state"] == "CHECKPOINT_VERIFIED"


def test_selfretry_gives_up_at_cap_touching_nothing(fake_tmux, cc_home,
                                                    tmp_path, monkeypatch):
    d = _deferred(cc_home, tmp_path)
    time.sleep(0.01)
    ledger.append(paths.session_dir(d["key"], d["nnn"]),
                  {"type": "user_prompt", "text": "user is active"})
    monkeypatch.setattr(rotator, "RETRY_DELAY_S", 0.0)
    armed = []
    monkeypatch.setattr(rotator, "spawn_retry",
                        lambda *a, **kw: armed.append(a) or 9)
    fired = []
    monkeypatch.setattr(rotator, "spawn", lambda *a: fired.append(a) or 1)
    rc = rotator.retry_run(key=d["key"], nnn=d["nnn"], pane="%9",
                           session_id=d["sid"], rotation_id=d["rid"],
                           attempt=rotator.RETRY_MAX_ATTEMPTS, cfg=d["cfg"])
    assert rc == 0 and fired == [] and armed == []
    assert state.load_state(d["key"], d["nnn"])["state"] == "CHECKPOINT_VERIFIED"
    assert "RETRY_GIVE_UP" in (paths.logs_dir() / "events.log").read_text()


def test_run_arms_selfretry_after_defer(fake_tmux, cc_home, tmp_path,
                                        monkeypatch):
    # Wiring: the activity-after-Stop defer arms the bounded self-retry.
    d = _rotating(cc_home, tmp_path)
    d["cfg"] = dict(d["cfg"], retry_deferred=True)
    time.sleep(0.01)
    ledger.append(paths.session_dir(d["key"], d["nnn"]),
                  {"type": "user_prompt", "text": "user keeps typing"})
    armed = []
    monkeypatch.setattr(rotator, "spawn_retry",
                        lambda k, n, p, s, r, a: armed.append((k, n, p, s, r, a)) or 9)
    rc = rotator.run(key=d["key"], nnn=d["nnn"], session_id=d["sid"],
                     pane="%9", rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0
    assert armed == [(d["key"], d["nnn"], "%9", d["sid"], d["rid"], 1)]
    assert "ROTATION_RETRY_ARMED" in (paths.logs_dir() / "events.log").read_text()


def test_run_defer_stays_quiet_when_selfretry_disabled(fake_tmux, cc_home,
                                                        tmp_path, monkeypatch):
    # The cfg gate is honored: with retry_deferred off, no tick is armed
    # and the next-Stop route is exactly the pre-#53 behavior.
    d = _rotating(cc_home, tmp_path)  # _rotating ships the gate OFF
    time.sleep(0.01)
    ledger.append(paths.session_dir(d["key"], d["nnn"]),
                  {"type": "user_prompt", "text": "user keeps typing"})

    def boom(*a, **kw):
        raise AssertionError("spawn_retry must not be called")
    monkeypatch.setattr(rotator, "spawn_retry", boom)
    assert rotator.run(key=d["key"], nnn=d["nnn"], session_id=d["sid"],
                       pane="%9", rotation_id=d["rid"], cfg=d["cfg"]) == 0


def test_run_retry_does_not_redrive_when_bootstrap_rotated(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # Attempt 1 sends the continuation (the successor's SessionStart claims
    # -> ROTATED) but _verify_resume misses it, so escalation re-enters.
    # The first tick must detect ROTATED and stop, NOT re-drive the pane.
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
                        lambda *a, **kw: False)  # force the escalation
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.3)
    monkeypatch.setattr(rotator, "RETRY_POLL_S", 0.05)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)

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


def test_run_deadline_notifies_and_stays_claimable(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # Dead pane: the fake tmux records keystrokes but no hook ever fires,
    # so neither SessionStart nor the RESUME prompt ever lands. The retry-
    # until-RESUME deadline gives up LOUD (notify + push) and stays
    # claimable. /clear is sent exactly ONCE (the old second attempt — which
    # blind-fired the whole sequence again — is gone).
    d = _rotating(cc_home, tmp_path)
    repo, key, nnn, rid, cfg = (d["repo"], d["key"], d["nnn"], d["rid"],
                                dict(d["cfg"]))
    _shrink_deadline(monkeypatch)
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
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    assert joined.count("send-keys -t %9 -l /clear") == 1  # never re-cleared
    assert _rename_calls(fake_tmux) >= 1
    assert notify_file.read_text().strip() == "spike-repo ROTATION_FAILED"
    log_text = (paths.logs_dir() / "events.log").read_text()
    assert "ROTATION_FAILED" in log_text
    assert "SESSIONSTART_MISSING" in log_text


def test_run_failure_without_notify_command_does_not_raise(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    d = _rotating(cc_home, tmp_path)
    _shrink_deadline(monkeypatch)
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
    monkeypatch.setattr(notify, "subprocess", InlineEmitSub(notify))
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
    _shrink_deadline(monkeypatch)
    sent = []
    monkeypatch.setattr(notify, "subprocess", InlineEmitSub(notify))
    monkeypatch.setattr(notify, "send",
                        lambda ep, title, body, prio, **kw: sent.append(
                            (title, prio, body)) or True)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=rid, cfg=cfg)
    assert rc == 1
    rows8 = [(p, b) for (tt, p, b) in sent
             if tt == "✗ cc-continuity: rotation failed"]
    assert len(rows8) == 1  # escalation ticks -> still ONE push (dedup)
    prio, body = rows8[0]
    assert prio == "urgent"
    assert "detail=no RESUME in successor ledger within 3s" in body


# --- rotation resume guarantee (spec 2026-11-03) -------------------------------
#
# Live bug (connector-149, 2026-11-03): CC mid-command/hook-pending still
# ACCUMULATES printable keystrokes but SILENTLY SWALLOWS Enter — the rotator's
# blind-fire sequence corrupted: the attempt-2 blind re-typed /rename + the
# swallowed Enter pasted the RESUME prompt onto the rename command and CC
# "renamed" the session to the garbage string. The fix: retry-until-RESUME
# with ack-driven escalation (bare Enter / re-paste / title-guarded rename),
# never a blind fire.


def _resume_ledger(key, nnn):
    """Simulate the successor's ledger receiving the RESUME prompt (the
    UserPromptSubmit hook would append this in the real world)."""
    ledger.append(paths.session_dir(key, 2),
                  {"type": "user_prompt", "agent_id": None,
                   "text": f"CONTINUITY RESUME cp-{nnn:03d}: read /x "
                           "completely, apply the VALIDATION result, then "
                           "continue from Next Atomic Action."})


def _fake_successor(monkeypatch, key):
    """Successor record exists (claim already ran) without running hooks —
    escalation ticks decide against a readable successor ledger."""
    monkeypatch.setattr(
        rotator, "_find_successor_rec",
        lambda k, old: {"nnn": 2, "name": "spike-repo-002",
                        "session_id": _NEW_SID} if k == key else None)


def _shrink_deadline(monkeypatch, deadline=3.0):
    # deadline is the TOTAL drive budget (attempt 1 + escalation), so it
    # must stay above attempt 1's own shrunk waits.
    monkeypatch.setattr(rotator, "RESUME_DEADLINE_S", deadline)
    monkeypatch.setattr(rotator, "RETRY_POLL_S", 0.05)
    monkeypatch.setattr(rotator, "STARTED_WAIT_S", 0.15)
    monkeypatch.setattr(rotator, "VERIFY_WAIT_S", 0.15)
    monkeypatch.setattr(rotator, "RENAME_ACK_WAIT_S", 0.1)
    monkeypatch.setattr(rotator, "POLL_S", 0.02)


def _enter_calls(log):
    """Count bare-Enter send-keys calls (exactly send-keys -t <pane> Enter).

    Empty argv lists occur when the log is read WHILE the fake tmux is
    mid-write ("---" appended, b64 line not yet) — ignore those partials."""
    return sum(1 for c in _calls(log)
               if len(c) == 4 and c[0] == "send-keys" and c[3] == "Enter")


def _paste_calls(log):
    return sum(1 for c in _calls(log) if c and c[0] == "paste-buffer")


def _rename_calls(log):
    return sum(1 for c in _calls(log)
               if len(c) >= 5 and c[0] == "send-keys" and c[3] == "-l"
               and c[4].startswith("/rename"))


# --- unit: helper predicates ----------------------------------------------------


def test_resume_present_single_ledger_scan(cc_home, monkeypatch):
    monkeypatch.setattr(
        rotator, "_find_successor_rec",
        lambda k, old: {"nnn": 2, "name": "spike-repo-002",
                        "session_id": _NEW_SID})
    ledger.append(paths.session_dir("k", 2),
                  {"type": "user_prompt", "text": "unrelated"})
    assert rotator._resume_present("k", 1, "cp-001", "spike-repo-001") is False
    ledger.append(paths.session_dir("k", 2),
                  {"type": "user_prompt",
                   "text": "CONTINUITY RESUME cp-001: read /x ..."})
    assert rotator._resume_present("k", 1, "cp-001", "spike-repo-001") is True
    assert rotator._resume_present("k", 1, "cp-002", "spike-repo-001") is False


def test_resume_present_no_successor_is_false(cc_home, monkeypatch):
    monkeypatch.setattr(rotator, "_find_successor_rec", lambda k, old: None)
    assert rotator._resume_present("k", 1, "cp-001", "spike-repo-001") is False


def test_title_missing_reads_pane_not_the_input_box():
    # old name in the status line -> rename needed
    cap = lambda p: f"⏺ hi\n{_FRAME}\n  sess spike-repo-001 │\n"
    assert rotator._title_missing("%9", "spike-repo-002", capture=cap) is True
    # successor name visible (status line) -> already renamed
    cap2 = lambda p: f"⏺ hi\n{_FRAME}\n  sess spike-repo-002 │\n"
    assert rotator._title_missing("%9", "spike-repo-002", capture=cap2) is False
    # the name only in the TYPED (unsubmitted) command must NOT count as
    # proof the rename ran (live incident: Enter swallowed)
    cap3 = lambda p: f"❯\xa0/rename spike-repo-002\n{_FRAME}\n  sess x │\n"
    assert rotator._title_missing("%9", "spike-repo-002", capture=cap3) is True
    # unknown pane (empty capture) -> never fire blind
    assert rotator._title_missing("%9", "spike-repo-002",
                                  capture=lambda p: "") is False


def test_wait_pane_contains_ack_poll(fake_tmux, monkeypatch):
    monkeypatch.setattr(rotator, "POLL_S", 0.02)
    # needle appears -> acked
    assert rotator._wait_pane_contains("%9", "spike-repo-002", 0.1,
                                       capture=lambda p: "sess spike-repo-002")
    # non-empty pane without the needle -> times out (NOT acked)
    assert rotator._wait_pane_contains("%9", "spike-repo-002", 0.1,
                                       capture=lambda p: "sess spike-repo-001") is False
    # empty capture = cannot observe -> fail-open (blind-fire parity)
    assert rotator._wait_pane_contains("%9", "spike-repo-002", 0.1,
                                       capture=lambda p: "") is True


# --- escalation (b): bare Enter submits our swallowed prompt --------------------


def test_run_escalation_bare_enter_submits_swallowed_prompt(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # The paste lands but its Enter is swallowed: the C.8 prompt sits in the
    # input box. The tick must press a BARE Enter (submit it) — then the
    # RESUME lands and the rotation completes without re-pasting.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    _fake_successor(monkeypatch, key)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text("")
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    # the fake paste puts the prompt in the box and it STAYS there (Enter lost)
    box = (f"❯\xa0CONTINUITY RESUME cp-{nnn:03d}: read /x completely, apply\n"
           f"{_FRAME}\n{_STATUS}\n")
    monkeypatch.setenv("FAKE_PASTE_HOOK",
                       f"python3 -c \"import pathlib;pathlib.Path({str(pane_file)!r}).write_text({box!r})\"")

    def world():
        # fire the RESUME ledger entry only once a bare Enter (4th: /clear,
        # rename, paste, ESCALATION) proves the escalation submitted the box
        deadline = time.time() + 5
        while time.time() < deadline:
            if _enter_calls(fake_tmux) >= 4:
                _resume_ledger(key, nnn)
                return
            time.sleep(0.02)

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    t.join(timeout=10)
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    assert _enter_calls(fake_tmux) >= 4  # the bare Enter was pressed
    assert _paste_calls(fake_tmux) == 1  # no blind re-paste happened
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    assert any(j == "send-keys -t %9 Enter" for j in
               joined[joined.index("paste-buffer -p -d -b ccc -t %9"):])


# --- escalation (c): re-paste when the box settled empty -------------------------


def test_run_escalation_repastes_when_box_empty(fake_tmux, cc_home, tmp_path,
                                                monkeypatch):
    # The first paste never reached CC (lost entirely): box stays empty, the
    # tick re-pastes C.8; the RESUME lands only after the second paste.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    _fake_successor(monkeypatch, key)
    counter = tmp_path / "paste-count"
    counter.write_text("0")
    monkeypatch.setenv(
        "FAKE_PASTE_HOOK",
        f"python3 -c \"import pathlib;p=pathlib.Path({str(counter)!r});"
        f"p.write_text(str(int(p.read_text())+1))\"")

    def world():
        deadline = time.time() + 5
        while time.time() < deadline:
            if _paste_calls(fake_tmux) >= 2:
                _resume_ledger(key, nnn)
                return
            time.sleep(0.02)

    t = _world_thread(world)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    t.join(timeout=10)
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    assert _paste_calls(fake_tmux) >= 2  # re-paste happened
    joined = [" ".join(c) for c in _calls(fake_tmux)]
    assert joined.count("send-keys -t %9 -l /clear") == 1  # pane cleared once
    assert _rename_calls(fake_tmux) == 1  # title unknown -> never re-typed


# --- escalation (d): title-guarded rename ----------------------------------------


def test_run_escalation_rename_retried_only_while_title_missing(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # capture-pane proves the title was NOT renamed (status line keeps the
    # old name): rename may be re-sent, but NEVER a paste onto an unacked
    # rename -> with the ack never arriving, paste must stay at zero.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(f"⏺ hi\n{_FRAME}\n  sess spike-repo-001 │\n")
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    monkeypatch.setenv("FAKE_CU_CLEARS", "0")
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 1  # deadline: RESUME never landed
    assert _rename_calls(fake_tmux) >= 2  # retried while unrenamed
    assert _paste_calls(fake_tmux) == 0  # never pasted onto an unacked rename
    assert joined_clears(fake_tmux) == 1


def joined_clears(log):
    return [" ".join(c) for c in _calls(log)].count(
        "send-keys -t %9 -l /clear")


def test_run_escalation_never_renames_when_title_present(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # capture-pane shows the successor name already in the status line: the
    # title guard must NOT re-send /rename (the connector-149 garbage-name
    # incident guard). Re-pastes happen; deadline still fails loudly.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text(f"⏺ hi\n{_FRAME}\n  sess spike-repo-002 │\n")
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    monkeypatch.setenv("FAKE_CU_CLEARS", "0")
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 1
    assert _rename_calls(fake_tmux) == 1  # attempt 1 only, never re-typed
    assert _paste_calls(fake_tmux) >= 2   # escalation kept re-pasting
    assert joined_clears(fake_tmux) == 1


# --- escalation guard: never clobber live user input ------------------------------


def test_run_escalation_skips_ticks_while_user_typing(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # After the clear the user starts typing: the box holds foreign text.
    # Every box-touching tick must skip: no Enter beyond the fixed attempt-1
    # three (clear/rename/paste), no re-paste; the deadline still fires loud.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    _fake_successor(monkeypatch, key)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text("")
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    user_box = f"❯\xa0user is drafting this\n{_FRAME}\n{_STATUS}\n"
    monkeypatch.setenv("FAKE_PASTE_HOOK",
                       f"python3 -c \"import pathlib;pathlib.Path({str(pane_file)!r}).write_text({user_box!r})\"")
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 1
    assert _enter_calls(fake_tmux) == 3  # only attempt-1's clear/rename/paste
    assert _paste_calls(fake_tmux) == 1  # never re-pasted onto the draft
    assert "ROTATION_FAILED" in (paths.logs_dir() / "events.log").read_text()


def test_run_escalation_late_resume_completes_without_touching_pane(
        fake_tmux, cc_home, tmp_path, monkeypatch):
    # The user manually submits the RESUME prompt (the connector-149 manual
    # rescue): a late ledger hit must complete the rotation passively — no
    # extra keystrokes at all. The ledger flip is driven from attempt 1's
    # verify (synchronous, right at the escalation boundary) instead of a
    # wall-clock thread: a blind sleep raced the 0.05s tick cadence and
    # flaked the exact keystroke count on loaded CI runners.
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    _fake_successor(monkeypatch, key)
    pane_file = tmp_path / "pane.txt"
    pane_file.write_text("")
    monkeypatch.setenv("FAKE_PANE_FILE", str(pane_file))
    user_box = f"❯\xa0user is drafting this\n{_FRAME}\n{_STATUS}\n"
    monkeypatch.setenv("FAKE_PASTE_HOOK",
                       f"python3 -c \"import pathlib;pathlib.Path({str(pane_file)!r}).write_text({user_box!r})\"")

    calls = {"n": 0}

    def rescue_on_first_verify(*a, **kw):
        # attempt-1 verify fails, the user's manual submit lands, and
        # escalation's FIRST guard tick must complete passively.
        calls["n"] += 1
        if calls["n"] == 1:
            _resume_ledger(key, nnn)
        return False

    monkeypatch.setattr(rotator, "_verify_resume", rescue_on_first_verify)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    assert _enter_calls(fake_tmux) == 3  # passive completion: no new keys
    assert _paste_calls(fake_tmux) == 1


def test_run_escalation_rotated_guard_stops_redrive(fake_tmux, cc_home,
                                                     tmp_path, monkeypatch):
    # A late hook claim moves the state to ROTATED during escalation: the
    # tick must stop re-driving (ROTATED is terminal, successor is live).
    # The flip is driven from attempt 1's verify (synchronous, at the
    # escalation boundary) instead of a wall-clock thread racing the
    # 0.05s tick: on loaded CI runners the late transition let one extra
    # re-paste fire (flaky 4-vs-3 keystrokes).
    d = _rotating(cc_home, tmp_path)
    key, nnn = d["key"], d["nnn"]
    _shrink_deadline(monkeypatch)
    calls = {"n": 0}

    def late_claim_on_first_verify(*a, **kw):
        # attempt-1 verify fails; the hook claim lands; escalation's
        # FIRST guard tick must see ROTATED and stop before any keystroke.
        calls["n"] += 1
        if calls["n"] == 1:
            state.transition(key, nnn, "ROTATED")
        return False

    monkeypatch.setattr(rotator, "_verify_resume", late_claim_on_first_verify)
    rc = rotator.run(key=key, nnn=nnn, session_id=d["sid"], pane="%9",
                     rotation_id=d["rid"], cfg=d["cfg"])
    assert rc == 0
    assert state.load_state(key, nnn)["state"] == "ROTATED"
    assert _enter_calls(fake_tmux) == 3  # escalation added no keystrokes
    assert "already completed (ROTATED)" in (paths.logs_dir()
                                             / "events.log").read_text()


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
