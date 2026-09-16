"""The six /continuity-* user skills (PRD §16, P7-D11).

Each skill is a thin instruction: run one exact command and report its
output verbatim. rotate/off/on carry ``disable-model-invocation: true``
so the model can never toggle continuity on its own (PRD §16).
Uninstall removes EXACTLY these six directories — foreign skills
(e.g. graphify) are never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SKILLS: dict[str, dict] = {
    "continuity-status": {
        "command": "cc-continuity status",
        "description": ("Show cc-continuity status for the current project "
                        "(context ratio, state, next session name). Use when "
                        "the user asks about continuity/rotation status."),
        "disable_model_invocation": False,
    },
    "continuity-checkpoint": {
        "command": "cc-continuity checkpoint",
        "description": ("Write and finalize a cc-continuity checkpoint (handoff) "
                        "now, keeping the session running."),
        "disable_model_invocation": False,
    },
    "continuity-rotate": {
        "command": "cc-continuity request-rotation",
        "description": ("Ask cc-continuity to rotate this session at the next "
                        "clean boundary."),
        "disable_model_invocation": True,
    },
    "continuity-off": {
        "command": "cc-continuity off",
        "description": "Disable cc-continuity for the current session.",
        "disable_model_invocation": True,
    },
    "continuity-on": {
        "command": "cc-continuity on",
        "description": "Re-enable cc-continuity for the current session.",
        "disable_model_invocation": True,
    },
    "continuity-history": {
        "command": "cc-continuity history",
        "description": "Show the session chain (history) for the current project.",
        "disable_model_invocation": False,
    },
}


def skill_names() -> tuple[str, ...]:
    return tuple(SKILLS)


def skill_text(name: str) -> str:
    spec = SKILLS[name]
    lines = ["---", f"name: {name}", f"description: {spec['description']}"]
    if spec["disable_model_invocation"]:
        lines.append("disable-model-invocation: true")
    lines.append("---")
    body = ("\nRun this exact command and report its output verbatim to the "
            "user. Do not paraphrase or add commentary.\n\n```bash\n"
            f"{spec['command']}\n```\n")
    return "\n".join(lines) + "\n" + body


def skill_files(skills_dir: Path) -> list[Path]:
    return [Path(skills_dir) / name / "SKILL.md" for name in SKILLS]


def install_skills(skills_dir: Path) -> list[Path]:
    """Write the six SKILL.md files (idempotent). Returns their paths."""
    skills_dir = Path(skills_dir)
    out = []
    for name in SKILLS:
        d = skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "SKILL.md"
        f.write_text(skill_text(name), encoding="utf-8")
        out.append(f)
    return out


def uninstall_skills(skills_dir: Path) -> list[Path]:
    """Remove exactly the six managed skill dirs. Returns removed dirs."""
    removed = []
    for name in SKILLS:
        d = Path(skills_dir) / name
        if d.is_dir():
            shutil.rmtree(d)
            removed.append(d)
    return removed
