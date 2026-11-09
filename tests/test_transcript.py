"""transcript.py: issue #8 - JSONL parsing, markdown rendering, HTML export."""

import json
from datetime import datetime
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


def test_label_timestamp_separator_in_markup():
    # Issue #12 (emit level): the timestamp used to render flush against
    # the role label ("User11:23:08"); a literal separator between the
    # label and the span is pinned, spacing is not left to CSS alone.
    import re
    d = _doc()
    assert re.search(r'<h2 class="user">User +<span class="ts">\d{2}:\d{2}:\d{2}</span></h2>', d)


def test_timestamp_shares_label_baseline():
    # Issue #12 (CSS level): .ts floated inside a max-content h2 sat above
    # the label instead of sharing its baseline; the pill becomes an
    # inline-flex baseline container with an explicit gap and no floats.
    d = _doc()
    assert "display:inline-flex" in d and "align-items:baseline" in d and "gap:8px" in d
    assert "float" not in d
    assert ".ts:empty{display:none}" in d  # gap adds slack only when a timestamp exists


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


TS = "2026-11-05T19:26:03.123Z"


def _fixture(tmp_path, n=2):
    recs = [_rec("user", "hello", TS), _rec("assistant", "hi", "2026-11-05T23:59:00.000Z")]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(recs) + "\n", encoding="utf-8")
    return p


def _meta():
    return {"name": "sess-1", "nnn": "1", "project": "proj", "branch": "main",
            "head": "abc1234", "model": "m", "ratio": 0.5}


def test_export_pair(tmp_path):
    stem = transcript.export_session(_fixture(tmp_path), tmp_path / "out", _meta())
    local = datetime.fromisoformat(TS).astimezone().strftime("%Y-%m-%d-%H%M")
    assert stem == f"sess-1_{local}"
    assert (tmp_path / "out" / f"{stem}.md").read_text(encoding="utf-8").startswith("# sess-1_")
    assert "hello" in (tmp_path / "out" / f"{stem}.html").read_text(encoding="utf-8")


def test_export_cap_and_footer(tmp_path):
    p = tmp_path / "big.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i in range(300):
            f.write(_rec("user", "x" * 40000 + str(i), "2026-11-05T19:26:03.123Z") + "\n")
    stem = transcript.export_session(p, tmp_path / "out", _meta())
    html = (tmp_path / "out" / f"{stem}.html").read_text(encoding="utf-8")
    assert "[truncated]" in html
    md = (tmp_path / "out" / f"{stem}.md").read_text(encoding="utf-8")
    assert "xxxx0" in md and "xxxx299" not in md  # hard cut at cap, whole lines only


def test_export_failures_never_raise(tmp_path, cc_home, monkeypatch):
    # cc_home: debug_log must write to the temp store, never the real
    # ~/.claude-continuity/logs/errors.log (Ruling: brief fixture omitted it).
    assert transcript.export_session(tmp_path / "missing.jsonl", tmp_path / "o1", _meta()) is None
    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(transcript.atomic, "write_bytes_fsync", boom)
    assert transcript.export_session(_fixture(tmp_path), tmp_path / "o2", _meta()) is None
    assert not (tmp_path / "o2").exists() or not list((tmp_path / "o2").iterdir())


# --- issue #16: session switcher (per-project sessions.js manifest) ---

def _proj_tree(tmp_path, nnn_stems):
    """Fake project dir holding sessions/<nnn>/checkpoint/<stem>.html files."""
    proj = tmp_path / "projects" / "proj-abc12345"
    for nnn, stem in nnn_stems:
        d = proj / "sessions" / nnn / "checkpoint"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.html").write_text("<html></html>", encoding="utf-8")
    return proj


def test_collect_sessions_newest_first(tmp_path):
    proj = _proj_tree(tmp_path, [("166", "claude-continuity-166_2026-11-08-1000"),
                                 ("167", "claude-continuity-167_2026-11-08-1815")])
    e = transcript.collect_sessions(proj)
    assert [x["href"] for x in e] == ["sessions/167/checkpoint/claude-continuity-167_2026-11-08-1815.html",
                                      "sessions/166/checkpoint/claude-continuity-166_2026-11-08-1000.html"]
    assert e[0]["label"] == "claude-continuity-167 - 08 Nov 18:15"
    assert all(x["href"].isascii() and x["label"].isascii() for x in e)


def test_collect_sessions_ignores_noise_and_falls_back_to_mtime(tmp_path):
    proj = _proj_tree(tmp_path, [("001", "sess_a")])  # stem without a date part
    (proj / "sessions" / "001" / "checkpoint" / "handoff.md").write_text("x", encoding="utf-8")
    (proj / "sessions" / "002" / "checkpoint").mkdir(parents=True)  # no export yet
    e = transcript.collect_sessions(proj)
    assert len(e) == 1
    assert e[0]["href"] == "sessions/001/checkpoint/sess_a.html"
    assert e[0]["label"] == "sess_a"  # bad stem: label falls back to the stem


def test_render_manifest_shape(tmp_path):
    entries = [{"href": "sessions/001/checkpoint/a.html", "label": "a - 01 Jan 02:03"}]
    s = transcript.render_manifest(entries)
    assert s.startswith("window.CC_SESSIONS=") and s.isascii()
    assert json.loads(s[s.index("["):s.rindex("]") + 1]) == entries


def test_update_sessions_manifest_writes_project_file(tmp_path):
    proj = _proj_tree(tmp_path, [("166", "s-166_2026-11-08-1000"),
                                 ("167", "s-167_2026-11-08-1815")])
    transcript.update_sessions_manifest(proj / "sessions" / "166" / "checkpoint")
    js = (proj / "sessions.js").read_text(encoding="utf-8")
    assert js.startswith("window.CC_SESSIONS=")
    assert "sessions/167/checkpoint/s-167_2026-11-08-1815.html" in js
    assert "sessions/166/checkpoint/s-166_2026-11-08-1000.html" in js


def test_update_sessions_manifest_wrong_shape_is_noop(tmp_path, cc_home):
    # cc_home: the skip path logs through debug_log, never the real store.
    out = tmp_path / "out"
    out.mkdir()
    transcript.update_sessions_manifest(out)  # not <proj>/sessions/<nnn>/checkpoint
    assert not list(tmp_path.rglob("sessions.js"))


def test_render_html_switcher_when_wired():
    meta = dict(META, manifest_src="../../sessions.js",
                self_href="sessions/166/checkpoint/x.html")
    d = transcript.render_html([("user", "hi", None)], meta)
    assert '<select id="s"' in d and 'data-cur="sessions/166/checkpoint/x.html"' in d
    assert 'src="../../sessions.js"' in d
    assert d.isascii()
    # User polish (2026-11-08): switcher shares the search row, both groups
    # carry visible capitalized labels; the plural is resolved client-side.
    # User polish round 2 (2026-11-08): two rows. Row 1 = Search (capped at
    # half the header) with Sessions facing it at the right edge; row 2 =
    # matching-blocks filter, theme, stats.
    assert d.count('<div class="controls">') == 2
    assert '<label for="q">Search</label>' in d
    assert '<label for="s" id="slbl">' in d
    assert "'Sessions'" in d and "'Session'" in d  # plural-aware JS
    assert "input[type=search]{flex:1" in d and "max-width:50%" in d
    assert d.index('id="s"') < d.index('id="f"') < d.index('id="t"') < d.index('id="stats"')


def test_render_html_no_switcher_when_unwired():
    d = _doc()  # META has no manifest keys (legacy call shape)
    assert 'id="s"' not in d and "sessions.js" not in d
    assert '<label for="q">Search</label>' in d  # Search label is unconditional


def test_export_session_writes_manifest(tmp_path):
    proj = tmp_path / "proj"
    dest = proj / "sessions" / "001" / "checkpoint"
    stem = transcript.export_session(_fixture(tmp_path), dest, _meta())
    assert stem
    js = (proj / "sessions.js").read_text(encoding="utf-8")
    assert f"sessions/001/checkpoint/{stem}.html" in js


def test_export_relative_depth_matches_checkpoint_dirs(tmp_path):
    # Browser-verified integration rule: the page lives at
    # sessions/<NNN>/checkpoint/x.html (three levels under the project),
    # so the manifest must resolve with ../../../ from the checkpoint dir.
    proj = tmp_path / "proj"
    dest = proj / "sessions" / "007" / "checkpoint"
    stem = transcript.export_session(_fixture(tmp_path), dest, _meta())
    page = (dest / f"{stem}.html").read_text(encoding="utf-8")
    assert 'src="../../../sessions.js"' in page
    assert 'data-up="../../../"' in page
    assert 'data-cur="sessions/007/checkpoint/' in page
    # and the resolved target must actually exist
    assert (dest / "../../../sessions.js").resolve() == (proj / "sessions.js").resolve()


def test_export_session_survives_manifest_failure(tmp_path, cc_home, monkeypatch):
    def boom(dest_dir):
        raise RuntimeError("simulated manifest loss")
    monkeypatch.setattr(transcript, "update_sessions_manifest", boom)
    dest = tmp_path / "proj" / "sessions" / "001" / "checkpoint"
    assert transcript.export_session(_fixture(tmp_path), dest, _meta())
    assert list(dest.glob("*.html"))
