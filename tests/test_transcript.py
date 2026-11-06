"""transcript.py: issue #8 - JSONL parsing, markdown rendering, HTML export."""

import json
from pathlib import Path

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


def test_table_fenced_and_escaping():
    md = "|a|b|\n|--|--|\n|1|b|\n\n<script>x</script>\n\n```\nprint(1)\n```\n\nplain **b** `c`"
    out = transcript.md_to_html(md)
    assert "<table>" in out and "<th>a</th>" in out and "<td>1</td>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert "<pre>print(1)</pre>" in out
    assert "<p>plain <b>b</b> <code>c</code></p>" in out


def test_paragraph_grouping_and_lists():
    out = transcript.md_to_html("l1\nl2\n\n- one\n- two\n### head")
    assert out.count("<p>") == 1 and "l1 l2" in out
    assert "<ul><li>one</li><li>two</li></ul>" in out and "<h4>head</h4>" in out


META = {"name": "claude-continuity-166", "nnn": "166", "project": "claude-continuity",
        "branch": "main", "head": "1abe3c0", "model": "local-qwen", "ratio": 0.642,
        "stem": "claude-continuity-166_2026-11-05-1926", "span": "2026-11-05 19:26 - 23:59"}


def _doc():
    b = [("user", "hello", "2026-11-05T19:26:03.123Z"), ("assistant", "hi **there**", None)]
    return transcript.render_html(b, META)


def test_design_strings_and_ascii():
    d = _doc()
    assert d.isascii()
    for label in ("Session Name", "Model", "Project", "Ratio at rotation", "Branch", "Session Time"):
        assert label in d
    assert 'id="q"' in d and "matching blocks only" in d and 'id="t"' in d and 'id="stats"' in d
    assert '<h2 class="user">User' in d and '<h2 class="asst">Assistant' in d
    assert '<h2 class="user">#' not in d and '<h2 class="asst">#' not in d
    assert "<b>there</b>" in d and "auto" in d and "localStorage" in d
    import re
    assert re.search(r'<span class="ts">\d{2}:\d{2}:\d{2}</span>', d)  # tz-safe format pin


def test_both_mode_filter_reset_order():
    d = _doc()
    assert d.index("b.classList.remove('hidden');") < d.index("if(filter)")


def _mkjsonl(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", encoding="utf-8")
    return p


def test_metric_hit_wins(tmp_path):
    good = _mkjsonl(tmp_path / "live.jsonl")
    got = transcript.resolve_transcript_path({"transcript_path": str(good)},
                                             tmp_path / "proj", "sid", home=tmp_path)
    assert got == good


def test_fallback_derive_and_miss(tmp_path):
    # Ruling: root is a neutral path of [a-zA-Z0-9/] only, so the fixture's
    # naive replace("/", "-") expectation never depends on tmp_path's
    # underscore-bearing randomized name (interface rule is re.sub).
    root = Path("/tmp/proj")
    derived = _mkjsonl(tmp_path / ".claude" / "projects" / str(root).replace("/", "-") / "sid.jsonl")
    assert transcript.resolve_transcript_path({"transcript_path": str(tmp_path / "gone.jsonl")},
                                              root, "sid", home=tmp_path) == derived
    assert transcript.resolve_transcript_path(None, root, "nope", home=tmp_path) is None
