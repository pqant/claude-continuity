"""skills.py: the six /continuity-* user skills (PRD §16)."""

from cc_continuity import skills


def test_skill_names_six():
    assert skills.skill_names() == (
        "continuity-status", "continuity-checkpoint", "continuity-rotate",
        "continuity-off", "continuity-on", "continuity-history")


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


def test_disable_model_invocation_only_rotate_off_on():
    for name in ("continuity-rotate", "continuity-off", "continuity-on"):
        assert "disable-model-invocation: true" in skills.skill_text(name)
    for name in ("continuity-status", "continuity-checkpoint",
                 "continuity-history"):
        assert "disable-model-invocation" not in skills.skill_text(name)


def test_install_creates_six_files(tmp_path):
    paths = skills.install_skills(tmp_path / "skills")
    assert len(paths) == 6
    for name in skills.skill_names():
        f = tmp_path / "skills" / name / "SKILL.md"
        assert f.is_file()
        assert f.read_text(encoding="utf-8") == skills.skill_text(name)


def test_install_idempotent(tmp_path):
    once = skills.install_skills(tmp_path / "skills")
    twice = skills.install_skills(tmp_path / "skills")
    assert once == twice


def test_uninstall_removes_exactly_six(tmp_path):
    d = tmp_path / "skills"
    skills.install_skills(d)
    (d / "graphify").mkdir()                      # foreign skill must survive
    (d / "graphify" / "SKILL.md").write_text("x")
    removed = skills.uninstall_skills(d)
    assert len(removed) == 6
    assert (d / "graphify" / "SKILL.md").is_file()
    assert not (d / "continuity-status").exists()


def test_uninstall_missing_dir_noop(tmp_path):
    assert skills.uninstall_skills(tmp_path / "nope") == []
