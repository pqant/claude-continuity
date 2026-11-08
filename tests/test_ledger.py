"""ledger.py: append-only JSONL, safe under concurrent hook invocations."""

import threading

from cc_continuity import ledger


def test_append_and_read(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    ledger.append(sdir, {"type": "user_prompt", "text": "hi"})
    entries = ledger.read(sdir)
    assert len(entries) == 1
    assert entries[0]["type"] == "user_prompt"
    assert entries[0]["text"] == "hi"
    assert isinstance(entries[0]["ts"], float)


def test_append_creates_missing_dir(tmp_path):
    sdir = tmp_path / "a" / "001"
    ledger.append(sdir, {"type": "x"})
    assert (sdir / "ledger.jsonl").is_file()


def test_read_missing_returns_empty_list(tmp_path):
    assert ledger.read(tmp_path / "nope") == []


def test_ts_can_be_overridden(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    ledger.append(sdir, {"type": "x", "ts": 1234.5})
    assert ledger.read(sdir)[0]["ts"] == 1234.5


def test_concurrent_appends_all_lines_present(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    n_threads, n_each = 8, 25

    def worker(tid):
        for i in range(n_each):
            ledger.append(sdir, {"type": "t", "tid": tid, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = ledger.read(sdir)  # also proves every line is valid JSON
    assert len(entries) == n_threads * n_each
    seen = {(e["tid"], e["i"]) for e in entries}
    assert len(seen) == n_threads * n_each  # no lost or duplicated entries


def test_blocked_reason_forward_scan(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    assert ledger.blocked_reason(sdir) is None
    ledger.append(sdir, {"type": "needs_human", "reason": "drop the db?"})
    assert ledger.blocked_reason(sdir) == "drop the db?"
    ledger.append(sdir, {"type": "user_prompt", "text": "ok",
                         "agent_id": "sub-1"})  # subagent: does NOT consume
    assert ledger.blocked_reason(sdir) == "drop the db?"
    ledger.append(sdir, {"type": "user_prompt", "text": "ok"})
    assert ledger.blocked_reason(sdir) is None


def test_blocked_reason_last_unconsumed_wins(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    ledger.append(sdir, {"type": "needs_human", "reason": "first"})
    ledger.append(sdir, {"type": "user_prompt", "text": "ok"})
    ledger.append(sdir, {"type": "needs_human", "reason": "second"})
    assert ledger.blocked_reason(sdir) == "second"


def test_blocked_reason_survives_corrupt_ledger(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "ledger.jsonl").write_text("{not json\n", encoding="utf-8")
    assert ledger.blocked_reason(sdir) is None


# ---------------------------------------------------------------- reply_ready


def _rdir(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    return sdir


def test_reply_pending_none_when_empty(tmp_path):
    assert ledger.reply_pending(_rdir(tmp_path)) is None


def test_reply_pending_reports_ts_after_reply_ready(tmp_path):
    sdir = _rdir(tmp_path)
    ent = ledger.append(sdir, {"type": "reply_ready", "ts": 55.0})
    assert ledger.reply_pending(sdir) == ent["ts"] == 55.0


def test_reply_consumed_by_reply_read(tmp_path):
    sdir = _rdir(tmp_path)
    ledger.append(sdir, {"type": "reply_ready", "ts": 55.0})
    ledger.mark_reply_read(sdir)
    assert ledger.reply_pending(sdir) is None


def test_reply_consumed_by_main_user_prompt_not_subagent(tmp_path):
    sdir = _rdir(tmp_path)
    ledger.append(sdir, {"type": "reply_ready", "ts": 55.0})
    ledger.append(sdir, {"type": "user_prompt", "agent_id": "a1"})
    assert ledger.reply_pending(sdir) == 55.0   # subagent prompt does not consume
    ledger.append(sdir, {"type": "user_prompt", "text": "hi"})
    assert ledger.reply_pending(sdir) is None


def test_reply_latest_turn_wins(tmp_path):
    sdir = _rdir(tmp_path)
    ledger.append(sdir, {"type": "reply_ready", "ts": 1.0})
    ledger.mark_reply_read(sdir)
    ledger.append(sdir, {"type": "reply_ready", "ts": 2.0})
    assert ledger.reply_pending(sdir) == 2.0


def test_reply_pending_settle_hides_young_reply(tmp_path):
    # user ruling 2026-11-07: no blink before the settle window elapses
    sdir = _rdir(tmp_path)
    ledger.append(sdir, {"type": "reply_ready", "ts": 100.0})
    assert ledger.reply_pending(sdir, settle=15, now=110.0) is None
    assert ledger.reply_pending(sdir, settle=15, now=116.0) == 100.0
    assert ledger.reply_pending(sdir) == 100.0   # settle 0: raw read


def test_reply_pending_resume_entries_consume(tmp_path):
    # a session that wakes itself was never waiting for the user
    for wake in ("tool_use", "tool_failure", "task_created",
                 "task_completed", "stop"):
        sdir = tmp_path / wake
        ledger.append(sdir, {"type": "reply_ready", "ts": 100.0})
        ledger.append(sdir, {"type": wake, "ts": 101.0})
        assert ledger.reply_pending(sdir, settle=0, now=300.0) is None


def test_reply_pending_non_work_entries_do_not_consume(tmp_path):
    sdir = _rdir(tmp_path)
    ledger.append(sdir, {"type": "reply_ready", "ts": 100.0})
    ledger.append(sdir, {"type": "state", "from": "ACTIVE", "ts": 101.0})
    ledger.append(sdir, {"type": "user_prompt", "agent_id": "sub",
                         "ts": 102.0})
    assert ledger.reply_pending(sdir, settle=0, now=300.0) == 100.0
    ledger.append(sdir, {"type": "tool_failure", "tool": "Bash", "ts": 103.0})
    assert ledger.reply_pending(sdir, settle=0, now=300.0) is None
