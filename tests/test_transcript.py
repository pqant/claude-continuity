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
    assert [(r, t) for r, t, _, _ in blocks] == [("user", "hello"), ("assistant", "hi"), ("user", "ok")]
    assert blocks[0][2] == "2026-11-05T19:26:03.123Z"


def test_assistant_whitespace_collapsed():
    b = transcript.parse_conversation([_rec("assistant", "a\n\n\n\nb")])
    assert b[0][1] == "a\n\nb"


# --- issue #54: protocol plumbing must not crowd out the conversation ----


def test_plumbing_records_dropped():
    lines = [_rec("user", "gercek mesaj"),
             _rec("user", "<command-name>/clear</command-name> "
                         "<command-message>clear</command-message> "
                         "<command-args></command-args>"),
             _rec("user", "<task-notification>\n<task-id>bgc</task-id>\n"
                         "</task-notification>"),
             _rec("user", "Stop hook feedback:\nCONTINUITY: context at 67%. "
                          "Rotation pending."),
             _rec("user", "Base directory for this skill: /Users/x/.claude/skills/y"),
             _rec("assistant", "No response requested."),
             _rec("user", "devam")]
    blocks = transcript.parse_conversation(lines)
    assert [(r, t) for r, t, _, _ in blocks] == [("user", "gercek mesaj"),
                                                  ("user", "devam")]


def test_resume_anchors_collapsed():
    resume = ("CONTINUITY RESUME cp-223: read /Users/x/handoff.md completely, "
              "apply the VALIDATION result, then continue... " * 5)
    blocks = transcript.parse_conversation(
        [_rec("user", resume),
         _rec("user", "Continue from where you left off.")])
    assert [t for _, t, _, _ in blocks] == [
        "↻ CONTINUITY RESUME cp-223 · handoff.md", "↻ CC resume"]


def test_image_records_become_chips():
    # consecutive chips fold into one; human text in a record survives
    # without the metadata lines.
    blocks = transcript.parse_conversation([
        _rec("user", [{"type": "text", "text": "[Image: source: /x/1.png]"},
                      {"type": "text",
                       "text": "[Image: original 958x2062, displayed at 929x2000]"}]),
        _rec("user", "[Image: source: /x/2.png]"),
        _rec("user", "bunu gordun mu ???\n[Image: source: /x/3.png]")])
    assert [t for _, t, _, _ in blocks] == ["🖼 image", "bunu gordun mu ???"]


# --- issue #55: pasted images open in a lightbox modal --------------------


def _img(media="image/png", data="QUJD", stype="base64"):
    return {"type": "image",
            "source": {"type": stype, "media_type": media, "data": data}}


def test_image_blocks_attach_data_uris():
    blocks = transcript.parse_conversation([
        _rec("user", [{"type": "text", "text": "[Image: source: /x/1.png]"},
                      _img()])])
    assert [t for _, t, _, _ in blocks] == ["🖼 image"]
    assert list(blocks[0][3]) == ["data:image/png;base64,QUJD"]


def test_pure_image_record_without_text_line():
    blocks = transcript.parse_conversation([_rec("user", [_img(data="RAA=")])])
    assert len(blocks) == 1
    assert blocks[0][1] == "🖼 image"
    assert list(blocks[0][3]) == ["data:image/png;base64,RAA="]


def test_mixed_text_and_image_appends_chip():
    blocks = transcript.parse_conversation([
        _rec("user", [{"type": "text",
                       "text": "bunu gordun mu ???\n[Image: source: /x/3.png]"},
                      _img(media="image/jpeg", data="SUVHRQ==")])])
    assert len(blocks) == 1
    assert blocks[0][1] == "bunu gordun mu ???"
    assert list(blocks[0][3]) == ["data:image/jpeg;base64,SUVHRQ=="]


def test_folded_chips_merge_galleries():
    blocks = transcript.parse_conversation([
        _rec("user", [{"type": "text", "text": "[Image: source: /a]"},
                      _img(data="QQ==")]),
        _rec("user", [{"type": "text", "text": "[Image: original 10x10]"},
                      _img(data="Qg==")])])
    assert [t for _, t, _, _ in blocks] == ["🖼 image"]
    assert list(blocks[0][3]) == ["data:image/png;base64,QQ==",
                                  "data:image/png;base64,Qg=="]


def test_oversize_and_bad_type_stay_plain_chip():
    big = "A" * (transcript._MAX_IMG_B64 + 1)
    blocks = transcript.parse_conversation([
        _rec("user", [{"type": "text", "text": "[Image: source: /big]"},
                      _img(data=big)]),
        _rec("user", [{"type": "text", "text": "[Image: source: /tif]"},
                      _img(media="image/tiff")]),
        _rec("user", [{"type": "text", "text": "[Image: source: /u]"},
                      _img(stype="url")])])
    assert [t for _, t, _, _ in blocks] == ["🖼 image"]
    assert list(blocks[0][3]) == []


def test_render_html_chip_clickable_with_images():
    b = [("user", "🖼 image", None, ("data:image/png;base64,QUJD",)),
         ("user", "metin", None, ("data:image/png;base64,QkI=",))]
    d = transcript.render_html(b, META)
    assert d.count('class="chipbtn"') == 2
    assert "data:image/png;base64,QUJD" in d
    assert 'id="lb"' in d and 'id="lbi"' in d
    assert d.index("metin") < d.rindex('class="chipbtn"')  # mixed chip trails text


def test_render_html_plain_chip_not_clickable():
    d = transcript.render_html([("user", "🖼 image", None)], META)
    assert 'class="chipbtn"' not in d and 'id="lb"' not in d


def test_render_html_folded_run_one_button_two_images():
    b = [("user", "🖼 image", None,
          ("data:image/png;base64,QQ==", "data:image/png;base64,Qg=="))]
    d = transcript.render_html(b, META)
    assert d.count('class="chipbtn"') == 1
    assert d.count('src="data:image/png;base64') == 2


def test_render_markdown_ignores_images():
    b = [("user", "metin", None, ("data:image/png;base64,QUJD",))]
    md = transcript.render_markdown(b, META)
    assert "metin" in md and "base64" not in md


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
    # User polish round 3 (2026-11-08): ONE shared header grid so the
    # Sessions label sits directly below Session Time (same column, always);
    # the search box is a fixed short width, no greedy stretching.
    assert d.count('class="hd"') == 1 and '<div class="controls">' not in d
    assert "grid-template-columns:max-content 1fr max-content 1fr" in d
    assert ".qbox{width:360px" in d
    assert d.index('id="slbl"') > d.index("Session Time")
    assert '<div class="row2">' in d and d.index('id="f"') > d.index('id="slbl"')
    assert "'Sessions'" in d and "'Session'" in d  # plural-aware JS
    # User polish round 4 (2026-11-09): the header labels read bold and
    # dark (key labels, Search, Sessions), not faded grey.
    assert ".hd .k{font-weight:600;color:var(--fg)}" in d
    assert "label{font-size:13px;color:var(--fg);font-weight:600" in d


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


# ------------------------------------------------- turn_clock prev reply (#44)


def _hms(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")


def _turn_records(first_iso="2026-11-10T11:19:03.000Z",
                  last_iso="2026-11-10T11:19:55.000Z"):
    return [
        _rec("user", "ee ne durumdayiz ?", ts="2026-11-10T11:19:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "durum su"}], ts=first_iso),
        _rec("assistant", [{"type": "text", "text": "bitti"}], ts=last_iso),
    ]


def test_prev_reply_clock_basic(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(_turn_records()) + "\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f) == (
        f"prev reply {_hms('2026-11-10T11:19:03.000Z')} -> "
        f"{_hms('2026-11-10T11:19:55.000Z')} (55s)")


def test_prev_reply_clock_under_minute(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(
        _turn_records(last_iso="2026-11-10T11:19:47.000Z")) + "\n",
        encoding="utf-8")
    assert transcript.prev_reply_clock(f).endswith("(47s)")


def test_prev_reply_clock_minute_format(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(
        _turn_records(last_iso="2026-11-10T11:21:34.000Z")) + "\n",
        encoding="utf-8")
    assert transcript.prev_reply_clock(f).endswith("(2m34s)")
    # the 60s boundary reads as one minute, zero-padded seconds
    f.write_text("\n".join(
        _turn_records(last_iso="2026-11-10T11:20:00.000Z")) + "\n",
        encoding="utf-8")
    assert transcript.prev_reply_clock(f).endswith("(1m00s)")


def test_prev_reply_clock_tool_results_do_not_reset(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([
        _rec("user", "go", ts="2026-11-10T11:19:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "basliyorum"}],
             ts="2026-11-10T11:19:02.000Z"),
        _rec("user", [{"type": "tool_result", "content": "ok"}],
             ts="2026-11-10T11:19:30.000Z"),
        _rec("assistant", [{"type": "text", "text": "bitti"}],
             ts="2026-11-10T11:20:00.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f) == (
        f"prev reply {_hms('2026-11-10T11:19:02.000Z')} -> "
        f"{_hms('2026-11-10T11:20:00.000Z')} (1m00s)")


def test_prev_reply_clock_uses_the_last_turn(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(_turn_records(last_iso="2026-11-10T11:19:10.000Z") + [
        _rec("user", "devam", ts="2026-11-10T11:25:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "ikinci"}],
             ts="2026-11-10T11:25:04.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f) == (
        f"prev reply {_hms('2026-11-10T11:25:04.000Z')} -> "
        f"{_hms('2026-11-10T11:25:04.000Z')} (4s)")


def test_prev_reply_clock_unreadable_returns_none(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(_rec("user", "hi") + "\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f) is None  # no assistant reply yet
    f.write_text("garbage\n{broken\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f) is None  # unparsable file
    assert transcript.prev_reply_clock(tmp_path / "missing.jsonl") is None
    assert transcript.prev_reply_clock(None) is None


def test_prev_reply_clock_huge_prefix_still_reads_tail(tmp_path):
    f = tmp_path / "t.jsonl"
    noise = "\n".join(
        '{"type":"user","timestamp":"2026-01-01T00:00:00.000Z",'
        '"message":{"content":"' + "x" * 4096 + '"}}'
        for _ in range(600))  # ~2.5 MB before the real turn
    f.write_text(noise + "\n" + "\n".join(_turn_records()) + "\n",
                 encoding="utf-8")
    line = transcript.prev_reply_clock(f)
    assert line == (f"prev reply {_hms('2026-11-10T11:19:03.000Z')} -> "
                    f"{_hms('2026-11-10T11:19:55.000Z')} (55s)")


# ------------------------------------------ prev_task_duration (HUD #45)


def test_prev_task_duration_basic(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(_turn_records()) + "\n", encoding="utf-8")
    assert transcript.prev_task_duration(f) == "55s"


def test_prev_task_duration_minutes(tmp_path):
    f = tmp_path / "t.jsonl"
    # prompt 11:17:00 -> final reply 11:56:15 = 39m15s wall time
    f.write_text("\n".join([
        _rec("user", "task", ts="2026-11-10T11:17:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "bas"}],
             ts="2026-11-10T11:17:20.000Z"),
        _rec("assistant", [{"type": "text", "text": "bitti"}],
             ts="2026-11-10T11:56:15.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_task_duration(f) == "39m 15s"


def test_prev_task_duration_hours(tmp_path):
    f = tmp_path / "t.jsonl"
    # prompt 11:00:00 -> final reply 12:02:03 = 1h2m3s wall time (issue #51)
    f.write_text("\n".join([
        _rec("user", "task", ts="2026-11-10T11:00:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "bas"}],
             ts="2026-11-10T11:20:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "bitti"}],
             ts="2026-11-10T12:02:03.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_task_duration(f) == "1h 2m 03s"


def test_prev_task_duration_seconds_stay_compact(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([
        _rec("user", "task", ts="2026-11-10T11:00:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "bitti"}],
             ts="2026-11-10T11:00:47.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_task_duration(f) == "47s"


def test_prev_reply_clock_hours_stay_compact(tmp_path):
    # The prompt-side clock (issue #44) keeps the compact shape even
    # once the HUD gained spaced units (issue #51): shared _fmt_secs,
    # sep only on the HUD path.
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([
        _rec("user", "task", ts="2026-11-10T11:00:00.000Z"),
        _rec("assistant", [{"type": "text", "text": "bitti"}],
             ts="2026-11-10T12:02:03.000Z"),
    ]) + "\n", encoding="utf-8")
    assert transcript.prev_reply_clock(f).endswith("(1h2m03s)")


def test_prev_task_duration_unreadable_returns_none(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(_rec("user", "hi") + "\n", encoding="utf-8")
    assert transcript.prev_task_duration(f) is None  # no reply yet
    assert transcript.prev_task_duration(None) is None
