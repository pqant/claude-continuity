"""transcript.py: issue #8 - JSONL parsing, markdown rendering, HTML export."""

import json

from cc_continuity import transcript


def _rec(t, content, ts="2026-11-05T19:26:03.123Z"):
    return json.dumps({"type": t, "timestamp": ts, "message": {"content": content}})


def test_order_roles_and_noise():
    lines = [_rec("user", "hello"), _rec("assistant", [{"type": "text", "text": "hi"}]),
             json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1"}]}}),
             json.dumps({"type": "system", "message": {"content": "x"}}),
             "not json at all", _rec("user", "ok<system-reminder>SECRET</system-reminder>"),
             _rec("user", "<local-command-caveat>skip"), _rec("user", "   "),
             _rec("user", "Caveat: the messages below")]
    blocks = transcript.parse_conversation(lines)
    assert [(r, t) for r, t, _ in blocks] == [("user", "hello"), ("assistant", "hi"), ("user", "ok")]
    assert blocks[0][2] == "2026-11-05T19:26:03.123Z"


def test_assistant_whitespace_collapsed():
    b = transcript.parse_conversation([_rec("assistant", "a\n\n\n\nb")])
    assert b[0][1] == "a\n\nb"
