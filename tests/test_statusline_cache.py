"""Issue #77: idle status line invocations must not re-run the heavy stages.

CC re-renders the status line about once per second while a session sits
idle. The heavy stages behind the segment (session lookup, ledger reads,
the transcript parse behind the turn clock, the tmux pane capture, the
progress-data rewrite) all produce the same facts between tool calls, so
between boundaries they must run exactly once: the render reuses the
cached facts and only the wall-time stages (shimmer sweep, reply settle,
the ratio straight from the payload) keep flowing. Any tool-boundary file
change (ledger append, state rewrite, transcript growth, pane re-render)
invalidates the cache for the very next render.
"""

import json
import time

from cc_continuity import blink, config, ledger, paths, progress_pane, statusline, transcript
from hookutil import SID, make_repo, register_session

T0 = 1_800_000_000.0


def _clock(monkeypatch):
    box = [T0]
    monkeypatch.setattr(time, "time", lambda: box[0])
    return box


def _counters(monkeypatch):
    """Wrap every heavy stage so each call is recorded."""
    calls = {"blocked": 0, "reply": 0, "prev_dur": 0, "capture": 0,
             "session": 0, "write_progress": 0, "ensure_pane": 0}

    blocked = ledger.blocked_reason

    def blocked_w(session_dir):
        calls["blocked"] += 1
        return blocked(session_dir)

    monkeypatch.setattr(ledger, "blocked_reason", blocked_w)

    reply_pending = getattr(ledger, "reply_pending", None)
    reply_state = getattr(ledger, "reply_state", None)

    def reply_w(fn):
        def w(session_dir, **kw):
            calls["reply"] += 1
            return fn(session_dir, **kw)
        return w

    if reply_pending is not None:
        monkeypatch.setattr(ledger, "reply_pending", reply_w(reply_pending))
    if reply_state is not None:
        monkeypatch.setattr(ledger, "reply_state", reply_w(reply_state))

    prev = transcript.prev_task_duration

    def prev_w(path, **kw):
        calls["prev_dur"] += 1
        return prev(path, **kw)

    monkeypatch.setattr(transcript, "prev_task_duration", prev_w)

    capture = statusline._input_box_lines

    def capture_w(*a, **kw):
        calls["capture"] += 1
        return capture(*a, **kw)

    monkeypatch.setattr(statusline, "_input_box_lines", capture_w)

    find = statusline.find_session_by_id

    def find_w(sid):
        calls["session"] += 1
        return find(sid)

    monkeypatch.setattr(statusline, "find_session_by_id", find_w)

    write_progress = progress_pane.write_progress_data

    def write_w(win, data):
        calls["write_progress"] += 1
        calls.setdefault("progress_data", []).append(data)
        return write_progress(win, data)

    monkeypatch.setattr(progress_pane, "write_progress_data", write_w)

    ensure = progress_pane.ensure_progress_pane

    def ensure_w(cfg):
        calls["ensure_pane"] += 1
        return {"window_id": "@1"}

    monkeypatch.setattr(progress_pane, "ensure_progress_pane", ensure_w)
    has_data = progress_pane.has_progress_data

    def has_w(win):
        return True

    monkeypatch.setattr(progress_pane, "has_progress_data", has_w)
    return calls


def _cfg(**over):
    c = dict(config.DEFAULTS)
    c.update(over)
    return c


def _payload(repo, **over):
    p = {
        "session_id": SID,
        "cwd": str(repo),
        "context_window": {
            "context_window_size": 262144,
            "current_usage": {
                "input_tokens": 23170,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    p.update(over)
    return p


def _run(payload, **cfg_over):
    return statusline.run(json.dumps(payload).encode("utf-8"),
                          _cfg(**cfg_over))


def test_idle_repeated_render_runs_heavy_stages_once(cc_home, tmp_path,
                                                     monkeypatch):
    repo = make_repo(tmp_path)
    register_session(repo)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}\n", encoding="utf-8")
    calls = _counters(monkeypatch)
    _clock(monkeypatch)
    payload = _payload(repo, transcript_path=str(tp))

    first = _run(payload, statusline_ansi=True, turn_clock=True)
    second = _run(payload, statusline_ansi=True, turn_clock=True)
    third = _run(payload, statusline_ansi=True, turn_clock=True)

    assert first and second and third
    # frozen clock: the visible line must be identical, not merely similar
    assert second == first and third == first
    # the idle repeats must not touch the heavy stages again
    assert calls["blocked"] == 1
    assert calls["reply"] == 1
    assert calls["prev_dur"] == 1
    assert calls["capture"] == 1
    assert calls["session"] == 1


def test_ledger_boundary_invalidates_next_render(cc_home, tmp_path,
                                                 monkeypatch):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    sdir = paths.session_dir(key, nnn)
    calls = _counters(monkeypatch)
    _clock(monkeypatch)
    payload = _payload(repo)

    first = _run(payload, statusline_ansi=False)
    # a tool-boundary ledger append (no other file changes)
    ledger.append(sdir, {"type": "tool_use", "tool": "Bash"})
    second = _run(payload, statusline_ansi=False)

    assert first == second  # display unchanged
    assert calls["blocked"] == 2  # the boundary forced a fresh read


def test_reply_settle_lands_on_wall_clock_without_a_reread(cc_home,
                                                           tmp_path,
                                                           monkeypatch):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    sdir = paths.session_dir(key, nnn)
    clock = _clock(monkeypatch)
    clock[0] = T0 - 2.0
    ledger.append(sdir, {"type": "reply_ready"})  # ts = T0-2 (fake clock)
    clock[0] = T0
    calls = _counters(monkeypatch)
    payload = _payload(repo)

    first = _run(payload, statusline_ansi=False)  # settling: no marker yet
    assert blink.MARKER_REPLY not in first
    clock[0] = T0 + 6.0  # settle window elapsed, zero file changes
    second = _run(payload, statusline_ansi=False)

    assert blink.MARKER_REPLY in second
    # the marker flipped from the cached reply ts + wall time alone
    assert calls["blocked"] == 1
    assert calls["reply"] == 1


def test_ratio_flows_from_payload_even_on_cache_hit(cc_home, tmp_path,
                                                    monkeypatch):
    repo = make_repo(tmp_path)
    register_session(repo)
    _counters(monkeypatch)
    _clock(monkeypatch)

    first = _run(_payload(repo), statusline_ansi=False)
    assert "9%" in first
    grown = _payload(repo)
    grown["context_window"]["current_usage"]["input_tokens"] = 131072
    second = _run(grown, statusline_ansi=False)  # no file change at all
    assert "50%" in second


def test_mode1_pane_write_only_when_data_changes(cc_home, tmp_path,
                                                 monkeypatch):
    repo = make_repo(tmp_path)
    register_session(repo)
    calls = _counters(monkeypatch)
    _clock(monkeypatch)
    payload = _payload(repo)

    first = _run(payload, progress_type=1, statusline_ansi=False)
    assert first == ""  # the pane owns the display
    assert calls["write_progress"] == 1
    assert "9%" in calls["progress_data"][-1]

    _run(payload, progress_type=1, statusline_ansi=False)  # idle repeat
    assert calls["write_progress"] == 1  # identical data: no rewrite
    assert calls["ensure_pane"] == 1

    grown = _payload(repo)
    grown["context_window"]["current_usage"]["input_tokens"] = 131072
    _run(grown, progress_type=1, statusline_ansi=False)  # ratio moved
    assert calls["write_progress"] == 2
    assert "50%" in calls["progress_data"][-1]


def test_corrupt_cache_falls_back_to_a_full_render(cc_home, tmp_path,
                                                   monkeypatch):
    repo = make_repo(tmp_path)
    register_session(repo)
    calls = _counters(monkeypatch)
    _clock(monkeypatch)
    payload = _payload(repo)

    first = _run(payload, statusline_ansi=False)
    caches = list(paths.runtime_dir().glob("status-render-cache-*"))
    assert caches, "render cache file must live under runtime/"
    for c in caches:
        c.write_text("{not json", encoding="utf-8")
    second = _run(payload, statusline_ansi=False)

    assert second == first
    assert calls["blocked"] == 2  # corrupt cache => full path, no crash
