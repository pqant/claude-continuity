"""hooks/user_prompt.py: ledger entry + ``turn_active`` (PRD §9).

``{type:user_prompt, text:redact(truncate(prompt, 2000))}`` plus
``state.update_fields(turn_active=True)``. ``CONTINUITY RESUME`` handling
arrives in Phase 6.
"""

from cc_continuity import ledger, paths, state

from hookutil import fixture_payload, make_repo, payload, register_session, run_hook


def test_fixture_prompt_is_logged_verbatim_and_turn_becomes_active(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    p = fixture_payload("UserPromptSubmit", repo)
    assert run_hook("UserPromptSubmit", p) == 0

    entries = ledger.read(paths.session_dir(key, nnn))
    types = [e["type"] for e in entries]
    assert types == ["session_start", "user_prompt"]
    assert entries[1]["text"] == p["prompt"]  # 456 chars < 2000: no truncation

    st = state.load_state(key, nnn)
    assert st["turn_active"] is True


def test_prompt_truncated_to_2000_chars(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt="x" * 2500))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry["type"] == "user_prompt"
    assert entry["text"] == "x" * 2000


def test_prompt_is_redacted_before_logging(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit",
             payload("UserPromptSubmit", repo,
                     prompt="deploy with api_key=sk-abcdefghijklmnopqrstuvwx now"))
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert "sk-abcdefghijklmnopqrstuvwx" not in entry["text"]
    assert "[REDACTED]" in entry["text"]


def test_several_prompts_append_in_order(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    for text in ("first", "second", "third"):
        run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt=text))
    entries = ledger.read(paths.session_dir(key, nnn))
    assert [e["text"] for e in entries if e["type"] == "user_prompt"] == \
        ["first", "second", "third"]
    ts = [e["ts"] for e in entries if e["type"] == "user_prompt"]
    assert ts == sorted(ts)


def test_unregistered_session_is_silently_ignored(cc_home, tmp_path):
    from cc_continuity import project

    repo = make_repo(tmp_path)
    p = payload("UserPromptSubmit", repo, session_id="no-such-session", prompt="hi")
    assert run_hook("UserPromptSubmit", p) == 0
    # no project was registered by this event: no state dirs, no ledgers
    key = project.project_key_for_root(project.resolve_root(repo))
    assert not (paths.project_dir(key) / "sessions").is_dir()


def test_missing_prompt_field_is_harmless(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    assert run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo)) == 0
    entry = ledger.read(paths.session_dir(key, nnn))[-1]
    assert entry == {"type": "user_prompt", "text": "", "ts": entry["ts"]}


def test_turn_active_not_appended_to_ledger(cc_home, tmp_path):
    repo = make_repo(tmp_path)
    key, nnn = register_session(repo)
    run_hook("UserPromptSubmit", payload("UserPromptSubmit", repo, prompt="hi"))
    for e in ledger.read(paths.session_dir(key, nnn)):
        assert "turn_active" not in e
