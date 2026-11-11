"""cli `ledger [n]` (issue #46): compact tail of the session ledger.

One line per entry, `HH:MM:SS type key=value…`, newest N (default 20).
Session resolution mirrors `status` (#32); the command is read-only.
"""

from cc_continuity import cli, ledger, paths, state

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


def _seed_ticks(repo, count):
    key, nnn = register_session(repo)
    for i in range(count):
        ledger.append(paths.session_dir(key, nnn),
                      {"type": "tick", "text": f"tick{i}"})
    return key, nnn


def _ticks(out):
    return [l for l in out.splitlines() if "tick" in l]


def test_ledger_default_window_is_20(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    _seed_ticks(repo, 25)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger"]) == 0
    ticks = _ticks(capsys.readouterr().out)
    assert len(ticks) == 20
    assert any("tick24" in l for l in ticks)  # newest present
    assert not any("tick4 " in l or "tick4\n" in l or l.rstrip().endswith("tick4")
                   for l in ticks)  # 6th-newest cut off
    assert any("tick5" in l for l in ticks)   # oldest kept


def test_ledger_explicit_n(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    _seed_ticks(repo, 25)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger", "3"]) == 0
    ticks = _ticks(capsys.readouterr().out)
    assert len(ticks) == 3
    assert any("tick24" in l for l in ticks)


def test_ledger_nonpositive_n_falls_back_to_20(cc_home, tmp_path, monkeypatch,
                                               capsys):
    repo = make_repo(tmp_path)
    _seed_ticks(repo, 25)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger", "0"]) == 0
    assert len(_ticks(capsys.readouterr().out)) == 20


def test_ledger_no_session_exits_1(cc_home, tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cli.main(["ledger"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no session" in captured.err


def test_ledger_missing_ledger_file_is_empty_ok(cc_home, tmp_path,
                                                monkeypatch, capsys):
    from cc_continuity import project
    repo = make_repo(tmp_path)
    key = project.project_key_for_root(project.resolve_root(repo))
    state.create_session_state(key, "spike-repo-001", 1, SID)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CC_CONTINUITY_SESSION", SID)
    assert cli.main(["ledger"]) == 0
    assert capsys.readouterr().out == ""
