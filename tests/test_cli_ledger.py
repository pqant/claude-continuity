"""cli `ledger [n]` (issue #46): compact tail of the session ledger.

One line per entry, `HH:MM:SS type key=value…`, newest N (default 20).
Session resolution mirrors `status` (#32); the command is read-only.
"""

from cc_continuity import cli, ledger, paths

from hookutil import SID, make_repo, register_session


def _seed(repo, *entries):
    key, nnn = register_session(repo)
    for e in entries:
        ledger.append(paths.session_dir(key, nnn), e)
    return key, nnn


def test_ledger_prints_recent_entries(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    _seed(repo, {"type": "gate", "action": "guide", "from": "ACTIVE",
                 "to": "ROTATION_PENDING", "ratio": 0.76, "variant": "c2"})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger"]) == 0
    out = capsys.readouterr().out
    assert "gate" in out
    assert "action=guide" in out
    assert "variant=c2" in out


def test_ledger_multiline_value_collapsed(cc_home, tmp_path, monkeypatch,
                                          capsys):
    repo = make_repo(tmp_path)
    _seed(repo, {"type": "user_prompt", "text": "two\nlines   here"})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger"]) == 0
    prompt_lines = [l for l in capsys.readouterr().out.splitlines()
                    if "user_prompt" in l]
    assert len(prompt_lines) == 1  # entry stays on ONE line
    assert "text=two lines here" in prompt_lines[0]


def test_ledger_long_value_truncated(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    _seed(repo, {"type": "note", "text": "x" * 300})
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger"]) == 0
    line = [l for l in capsys.readouterr().out.splitlines()
            if "note" in l][0]
    assert line.endswith("…")
    assert len(line) < 120
