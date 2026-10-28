"""TmuxHost (spec §7): the tmux-session daemon host for WSL/Linux.

The recorder models the tmux surface the host uses: ``has-session``,
``new-session`` (``-t`` target-spec, ``-e`` supervisor marker),
``kill-session`` and ``show-environment`` (the live marker check — a
state-file marker alone cannot see a user-created foreign session).
"""

from cc_continuity import hosting


def _resolve_target(sessions, target):
    """Model real tmux -t resolution: exact match, else the UNIQUE prefix
    (a bare ``cc-d-watchdog`` matches the surviving ``cc-d-watchdog-
    digest`` — the run #3 e2e 6a false-FAIL); ``=name`` is exact only."""
    if target.startswith("="):
        name = target[1:]
        return name if name in sessions else None
    if target in sessions:
        return target
    prefixed = [s for s in sessions if s.startswith(target)]
    return prefixed[0] if len(prefixed) == 1 else None


def TmuxRecorder():
    state = {"sessions": set(), "markers": set(), "commands": []}

    def runner(cmd, **kw):
        c = " ".join(cmd)
        state["commands"].append(c)
        if "has-session" in c:
            m = _resolve_target(state["sessions"], c.split()[-1])

            class R:
                returncode = 0 if m else 1
                stdout = ""
                stderr = ""

            return R()
        if "new-session" in c:
            s = c.split(" -s ")[-1].split()[0]
            state["sessions"].add(s)
            if "CC_CONTINUITY_SUPERVISOR=1" in c:
                state["markers"].add(s)
        elif "kill-session" in c:
            m = _resolve_target(state["sessions"], c.split()[-1])
            if m:
                state["sessions"].discard(m)
                state["markers"].discard(m)
        elif "show-environment" in c:
            m = _resolve_target(state["sessions"], c.split("-t")[-1].split()[0])
            marked = m in state["markers"]

            class R:
                returncode = 0 if marked else 1
                stdout = "CC_CONTINUITY_SUPERVISOR=1\n" if marked else ""
                stderr = ""

            return R()
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    return runner, state


def test_ensure_creates_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_HOME",
                       str(tmp_path / ".claude-continuity"))
    run, st = TmuxRecorder()
    host = hosting.TmuxHost(runner=run)
    host.install("watchdog", tmp_path)
    assert "cc-d-watchdog" in st["sessions"]
    # race safety (Review Focus 5): creation names the session with -s
    # (a racing duplicate fails clean, never replaces)
    assert any("new-session" in c and " -s " in c for c in st["commands"])
    from cc_continuity.daemon_supervisor import state_path
    assert state_path("watchdog", tmp_path).is_file()


def test_ensure_noop_when_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_HOME",
                       str(tmp_path / ".claude-continuity"))
    run, st = TmuxRecorder()
    host = hosting.TmuxHost(runner=run)
    host.install("watchdog", tmp_path)
    n = len(st["sessions"])
    host.ensure("watchdog", tmp_path)
    assert len(st["sessions"]) == n  # no second session
    # a true no-op: the live marker is trusted, not kill+recreate
    assert not any("kill-session" in c for c in st["commands"])


def test_name_collision_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_CONTINUITY_HOME",
                       str(tmp_path / ".claude-continuity"))
    run, st = TmuxRecorder()
    host = hosting.TmuxHost(runner=run)
    st["sessions"].add("cc-d-watchdog")      # foreign session, no marker
    host.install("watchdog", tmp_path)
    assert len([s for s in st["sessions"] if s == "cc-d-watchdog"]) == 1 \
        # replaced, not duplicated
    assert any("kill-session" in c for c in st["commands"])


def test_uninstall_drops_rc_line(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("CC_CONTINUITY_HOME",
                       str(tmp_path / ".claude-continuity"))
    run, st = TmuxRecorder()
    host = hosting.TmuxHost(runner=run)
    from cc_continuity import hosting as h
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(h.rc_block_content(
        ["tmux has-session -t cc-d-watchdog 2>/dev/null || "
         "cc-continuity daemon ensure cc-d-watchdog"]))
    host.uninstall("watchdog", tmp_path)
    assert "daemon ensure" not in bashrc.read_text()


def test_install_upgrades_legacy_rc_line(tmp_path, monkeypatch):
    """The one-time migration: install replaces the pre-fix bare-target
    line in place (no duplicate, no stale form left behind)."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("CC_CONTINUITY_HOME",
                       str(tmp_path / ".claude-continuity"))
    run, st = TmuxRecorder()
    host = hosting.TmuxHost(runner=run)
    from cc_continuity import hosting as h
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(h.rc_block_content(
        ["tmux has-session -t cc-d-watchdog 2>/dev/null || "
         "cc-continuity daemon ensure cc-d-watchdog"]))
    host.install("watchdog", tmp_path)
    rc = bashrc.read_text()
    assert "tmux has-session -t =cc-d-watchdog" in rc
    assert "tmux has-session -t cc-d-watchdog 2>/dev/null" not in rc
