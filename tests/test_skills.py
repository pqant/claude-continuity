"""skills.py: the seven /continuity-* user skills (PRD section 16)."""

from cc_continuity import skills


def test_skill_names_seven():
    assert skills.skill_names() == (
        "continuity-status", "continuity-checkpoint", "continuity-rotate",
        "continuity-off", "continuity-on", "continuity-history",
        "continuity-excellence")


def test_skill_text_frontmatter_and_command():
    text = skills.skill_text("continuity-status")
    assert text.startswith("---\n")
    assert "name: continuity-status" in text
    assert "description: " in text
    assert "```bash\ncc-continuity status\n```" in text
    assert "disable-model-invocation" not in text


def test_command_mapping():
    expect = {
        "continuity-status": "cc-continuity status",
        "continuity-checkpoint": "cc-continuity checkpoint",
        "continuity-rotate": "cc-continuity request-rotation",
        "continuity-off": "cc-continuity off",
        "continuity-on": "cc-continuity on",
        "continuity-history": "cc-continuity history",
    }
    for name, cmd in expect.items():
        assert f"```bash\n{cmd}\n```" in skills.skill_text(name)


def test_disable_model_invocation_only_rotate_off_on_excellence():
    for name in ("continuity-rotate", "continuity-off", "continuity-on",
                 "continuity-excellence"):
        assert "disable-model-invocation: true" in skills.skill_text(name)
    for name in ("continuity-status", "continuity-checkpoint",
                 "continuity-history"):
        assert "disable-model-invocation" not in skills.skill_text(name)


def test_excellence_pins_the_perfection_loop():
    text = skills.skill_text("continuity-excellence")
    assert text.startswith("---\n")
    assert "name: continuity-excellence" in text
    # the perfection dimensions named in issue #81
    for dimension in ("explicit goal", "scope boundaries",
                      "environment and paths", "constraints",
                      "output format", "edge cases", "success criteria"):
        assert dimension in text
    # the approval gate: exactly one question, three options
    assert "run it, change it, or cancel" in text
    # original wording stays visible next to the perfected block
    assert "original wording verbatim" in text
    # safety: only the approved perfected prompt may ever run
    assert "Never run the raw prompt directly" in text
    # it is a behavior skill: no shell-command fence
    assert "```bash" not in text


def test_install_creates_seven_files(tmp_path):
    paths = skills.install_skills(tmp_path / "skills")
    assert len(paths) == 7
    for name in skills.skill_names():
        f = tmp_path / "skills" / name / "SKILL.md"
        assert f.is_file()
        assert f.read_text(encoding="utf-8") == skills.skill_text(name)


def test_install_idempotent(tmp_path):
    once = skills.install_skills(tmp_path / "skills")
    twice = skills.install_skills(tmp_path / "skills")
    assert once == twice


def test_uninstall_removes_exactly_seven(tmp_path):
    d = tmp_path / "skills"
    skills.install_skills(d)
    (d / "graphify").mkdir()                      # foreign skill must survive
    (d / "graphify" / "SKILL.md").write_text("x")
    removed = skills.uninstall_skills(d)
    assert len(removed) == 7
    assert (d / "graphify" / "SKILL.md").is_file()
    assert not (d / "continuity-status").exists()


def test_uninstall_missing_dir_noop(tmp_path):
    assert skills.uninstall_skills(tmp_path / "nope") == []


def test_all_descriptions_carry_the_continuity_keyword():
    # the /cc slash filter fuzzy-matches name + description; every managed
    # skill must carry "cc-continuity" so it stays discoverable (issue 82)
    for name in skills.skill_names():
        assert "cc-continuity" in skills.SKILLS[name]["description"]


def test_history_skill_pins_search_usage():
    # issue #84: the skill text must teach the keyword search so the /cc
    # filter and the invoking agent both see it.
    text = skills.skill_text("continuity-history")
    assert "cc-continuity history <query>" in text
    assert "search" in text.lower()
    # the plain-chain command fence (test_command_mapping) still holds
    assert "```bash\ncc-continuity history\n```" in text
