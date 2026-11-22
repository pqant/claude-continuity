"""overflow.py: context-400 brick scan, METRIC_FLOOR estimate, recovery.

Fixtures write transcript JSONL inline (the connector-258 tail shape);
windows are taken from config.DEFAULTS, never hardcoded (cp-244 test
plan)."""

import json

import pytest

from cc_continuity import config, ledger, overflow, paths, rotator, state
from hookutil import make_repo, project_key_for

W = config.DEFAULTS["window_tokens"]

_400_TEXT = (f"API Error: 400 This model's maximum context length is {W} "
             "tokens. However, you requested 32000 output tokens and your "
             f"prompt contains at least {W - 32000} input tokens, which "
             "exceeds the limit. Please reduce the length of the messages "
             "and retry.")


def _assistant(text, model="x", usage=None):
    msg = {"model": model, "content": [{"type": "text", "text": text}]}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


def _400_record():
    zero = {"input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0}
    return _assistant(_400_TEXT, model="<synthetic>", usage=zero)


def _write_jsonl(tmp_path, records):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------- scan

def test_scan_detects_trailing_400_with_system_tail(cc_home, tmp_path):
    # connector-258 tail shape: trailing system/last-prompt/
    # file-history-snapshot records are NOT assistant records - the
    # newest ASSISTANT record still counts.
    p = _write_jsonl(tmp_path, [
        _assistant("working on it"),
        {"type": "user", "message": {"role": "user", "content": "go"}},
        _400_record(),
        {"type": "system", "subtype": "turn_duration"},
        {"type": "last-prompt", "lastPrompt": "go"},
        {"type": "file-history-snapshot"},
    ])
    info = overflow.scan_tail_400(p)
    assert info is not None
    assert info["window"] == W
    assert info["input_tokens"] == W - 32000
    assert info["text_head"].startswith("API Error: 400")


def test_scan_none_on_plain_assistant_tail(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [_400_record(), _assistant("all good now")])
    assert overflow.scan_tail_400(p) is None


def test_scan_none_on_no_response_requested(cc_home, tmp_path):
    # row 447: the OTHER synthetic kind is not brick evidence
    p = _write_jsonl(tmp_path, [_assistant("No response requested.",
                                          model="<synthetic>")])
    assert overflow.scan_tail_400(p) is None


def test_scan_none_on_middle_400(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [_400_record(), _assistant("recovered"),
                                _assistant("and again")])
    assert overflow.scan_tail_400(p) is None


def test_scan_none_on_missing_or_none_path(cc_home, tmp_path):
    assert overflow.scan_tail_400(tmp_path / "absent.jsonl") is None
    assert overflow.scan_tail_400(None) is None


def test_scan_skips_malformed_tail_line(cc_home, tmp_path):
    p = tmp_path / "torn.jsonl"
    p.write_text(json.dumps(_assistant("ok")) + "\n"
                 + json.dumps(_400_record()) + "\n"
                 + '{"type": "assistant", "mess\n', encoding="utf-8")
    # the torn line is skipped; the 400 is still the newest assistant
    assert overflow.scan_tail_400(p) is not None


# ------------------------------------------------------------- estimate

def test_estimate_skips_zero_usage_synthetic(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [
        _assistant("real turn", usage={"input_tokens": W // 2,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0}),
        _400_record(),
    ])
    assert overflow.estimate_ratio(p, W) == 0.5


def test_estimate_sums_cache_fields(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [
        _assistant("cached turn",
                   usage={"input_tokens": 100,
                          "cache_creation_input_tokens": W // 4,
                          "cache_read_input_tokens": W // 4}),
    ])
    assert overflow.estimate_ratio(p, W) == round((100 + W // 2) / W, 4)


def test_estimate_bad_window_returns_none(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [_assistant("x",
                                           usage={"input_tokens": 10})])
    for bad in (0, -1, None, "262144", True):
        assert overflow.estimate_ratio(p, bad) is None


def test_estimate_no_usage_returns_none(cc_home, tmp_path):
    p = _write_jsonl(tmp_path, [_assistant("plain"), _400_record()])
    assert overflow.estimate_ratio(p, W) is None
    assert overflow.estimate_ratio(None, W) is None


# --------------------------------------------------------------- recover

def _mk(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key = project_key_for(repo)
    state.create_session_state(key, "repo-001", 1, "sid-a")
    return key


def _fake_finalize(k, n, *, cfg, root, metric=None, fault=None):
    # stand-in for the C.7 walk the real finalize drives from HF
    state.transition(k, n, "CHECKPOINT_WRITING")
    state.transition(k, n, "CHECKPOINT_VERIFIED")
    return ("cp-001", True)


def test_recover_400_happy(cc_home, tmp_path, monkeypatch):
    key = _mk(cc_home, tmp_path)  # BOOTSTRAPPING is recoverable
    spawns = []

    def fake_spawn(session_id, pane, rotation_id):
        spawns.append((session_id, pane, rotation_id))
        return 4242

    monkeypatch.setattr(overflow.rotator, "spawn", fake_spawn)
    monkeypatch.setattr(overflow.checkpoint, "finalize", _fake_finalize)
    info = {"window": W, "input_tokens": W - 32000, "text_head": "API Error"}
    res = overflow.recover_400(key, 1, "sid-a", "proj:cc.0",
                               dict(config.DEFAULTS), tmp_path, info)
    assert res == "recovered"
    rec = state.load_state(key, 1)
    assert rec["state"] == "ROTATING"
    assert spawns == [("sid-a", "proj:cc.0", rec["rotation_id"])]
    evs = [e for e in ledger.read(paths.session_dir(key, 1))
           if e.get("type") == "context400_recovered"]
    assert len(evs) == 1
    assert evs[0]["window"] == W and evs[0]["input_tokens"] == W - 32000


def test_recover_400_spawn_failure_keeps_checkpoint_verified(
        cc_home, tmp_path, monkeypatch):
    key = _mk(cc_home, tmp_path)

    def boom(session_id, pane, rotation_id):
        raise rotator.RotatorSpawnError("no tmux")

    monkeypatch.setattr(overflow.rotator, "spawn", boom)
    monkeypatch.setattr(overflow.checkpoint, "finalize", _fake_finalize)
    res = overflow.recover_400(key, 1, "sid-a", "proj:cc.0",
                               dict(config.DEFAULTS), tmp_path, None)
    assert res == "failed:RotatorSpawnError"
    assert state.load_state(key, 1)["state"] == "CHECKPOINT_VERIFIED"


def test_recover_400_state_gate(cc_home, tmp_path, monkeypatch):
    key = _mk(cc_home, tmp_path)
    for step in ("ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED",
                 "HANDOFF_FAILED", "CHECKPOINT_WRITING",
                 "CHECKPOINT_VERIFIED", "ROTATING"):
        state.transition(key, 1, step)
    calls = []
    monkeypatch.setattr(overflow.rotator, "spawn",
                        lambda *a: calls.append(a))
    monkeypatch.setattr(
        overflow.checkpoint, "finalize",
        lambda *a, **k: pytest.fail("finalize must not run"))
    res = overflow.recover_400(key, 1, "sid-a", "proj:cc.0",
                               dict(config.DEFAULTS), tmp_path, None)
    assert res == "skipped:ROTATING"
    assert calls == []
