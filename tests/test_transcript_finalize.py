"""Finalize wiring for the transcript export (issue #8 G1/G2/B4).

Reuses the established checkpoint harness: ``_registered``/``_cfg_for``/
``DRAFT_TEXT``/``_snap`` from ``test_checkpoint`` (same-directory pytest
imports, same as ``hookutil``) and the repo-root ``cc_home`` fixture.
"""

from cc_continuity import checkpoint, config, paths, state, transcript
from hookutil import SID
from test_checkpoint import DRAFT_TEXT, _cfg_for, _registered

TS = "2026-11-05T19:26:03.123Z"


def _conv(tmp_path):
    import json
    recs = [json.dumps({"type": "user", "timestamp": TS, "message": {"content": "hello"}}),
            json.dumps({"type": "assistant", "timestamp": TS, "message": {"content": "hi"}})]
    p = tmp_path / "conv.jsonl"
    p.write_text("\n".join(recs) + "\n", encoding="utf-8")
    return p


def test_finalize_writes_transcript_line(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    jsonl = _conv(tmp_path)
    cp_id, degraded = checkpoint.finalize(
        key, nnn, cfg=_cfg_for(repo), root=repo,
        metric={"session_id": SID, "transcript_path": str(jsonl), "model": "m", "ratio": 0.5})
    assert degraded is False
    final = paths.session_dir(key, nnn) / "checkpoint"
    stems = {p.stem for p in final.glob("*.html")}
    assert len(stems) == 1
    stem = stems.pop()
    assert (final / f"{stem}.md").is_file() and (final / f"{stem}.html").is_file()
    handoff = (final / "handoff.md").read_text(encoding="utf-8")
    assert f"Transcript: {(final / (stem + '.html')).as_uri()}" in handoff
    assert len((final / "SHA256SUMS").read_text(encoding="utf-8").splitlines()) == 6
    assert checkpoint.headings_in_order(handoff)


def test_finalize_without_transcript_source(cc_home, tmp_path):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    cp_id, degraded = checkpoint.finalize(key, nnn, cfg=_cfg_for(repo), root=repo, metric=None)
    assert degraded is False
    final = paths.session_dir(key, nnn) / "checkpoint"
    assert "Transcript:" not in (final / "handoff.md").read_text(encoding="utf-8")
    assert {p.name for p in final.iterdir()} == {"handoff.md", "state.json",
        "snapshot.json", "ledger.jsonl", "SHA256SUMS"}


def test_finalize_export_failure_is_silent(cc_home, tmp_path, monkeypatch):
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)

    def boom(*a, **k):
        raise RuntimeError("simulated disk loss")
    monkeypatch.setattr(transcript, "export_session", boom)
    cp_id, degraded = checkpoint.finalize(
        key, nnn, cfg=_cfg_for(repo), root=repo,
        metric={"session_id": SID, "transcript_path": str(_conv(tmp_path))})
    assert degraded is False
    final = paths.session_dir(key, nnn) / "checkpoint"
    assert "Transcript:" not in (final / "handoff.md").read_text(encoding="utf-8")
    assert "transcript: export aborted" in (paths.logs_dir() / "errors.log").read_text(encoding="utf-8")


def test_insert_transcript_line_position():
    from test_checkpoint import _snap
    text = checkpoint.render_handoff(_snap(), DRAFT_TEXT, False)
    out = checkpoint._insert_transcript_line(text, "file:///x/y.html")
    lines = out.splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("Transcript: "))
    assert lines[i - 1].startswith("- ") and lines[i + 1] == ""
    assert checkpoint.headings_in_order(out)


def test_finalize_writes_sessions_manifest(cc_home, tmp_path):
    # Issue #16: the export also refreshes the per-project sessions.js so
    # every viewer page can offer the session switcher over file://.
    repo, key, nnn = _registered(tmp_path, draft=DRAFT_TEXT)
    checkpoint.finalize(
        key, nnn, cfg=_cfg_for(repo), root=repo,
        metric={"session_id": SID, "transcript_path": str(_conv(tmp_path)),
                "model": "m", "ratio": 0.5})
    proj = paths.session_dir(key, nnn).parent.parent
    js = (proj / "sessions.js").read_text(encoding="utf-8")
    assert js.startswith("window.CC_SESSIONS=")
    assert f"sessions/{nnn:03d}/checkpoint/" in js
    # the checkpoint dir itself must stay manifest-free (SHA256SUMS shape)
    final = paths.session_dir(key, nnn) / "checkpoint"
    assert "sessions.js" not in {p.name for p in final.iterdir()}
