"""The seven /continuity-* user skills (PRD §16, P7-D11).

Six skills are a thin instruction: run one exact command and report its
output verbatim. continuity-excellence is a behavior skill: it carries a
custom ``body`` (no shell command) that pins the prompt-perfection loop.
rotate/off/on/excellence carry ``disable-model-invocation: true``
so the model can never toggle continuity on its own (PRD §16).
Uninstall removes EXACTLY these seven directories; foreign skills
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
    "continuity-excellence": {
        "body": (
            "When invoked, follow this exact loop with the raw prompt the "
            "user supplied as\nthe argument:\n\n"
            "1. If no raw prompt came with the invocation, ask for it first "
            "and wait.\n"
            "2. Perfect the prompt across these dimensions, filling every gap "
            "you can\n   infer from the session context: explicit goal, scope "
            "boundaries,\n   environment and paths, constraints, output "
            "format, edge cases, and\n   success criteria.\n"
            "3. Present both versions: the user's original wording verbatim, "
            "then the\n   perfected prompt as one copyable block.\n"
            "4. Ask exactly once, with these three options: run it, change "
            "it, or cancel.\n"
            "5. Run: execute the perfected prompt in this session now.\n"
            "   Change: apply the requested edits, return to step 3, and ask "
            "again.\n"
            "   Cancel: do nothing, confirm, and stop.\n\n"
            "Never run the raw prompt directly; only the approved perfected "
            "version runs."
        ),
        "description": (
            "Perfect a raw vague prompt multi-dimensionally, present it, ask "
            "run/change/cancel, and act on the answer. Use when the user has "
            "a rough prompt they want polished and executed."),
        "disable_model_invocation": True,
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
    custom = spec.get("body")
    if custom is not None:
        return "\n".join(lines) + "\n" + "\n" + custom + "\n"
    body = ("\nRun this exact command and report its output verbatim to the "
            "user. Do not paraphrase or add commentary.\n\n```bash\n"
            f"{spec['command']}\n```\n")
    return "\n".join(lines) + "\n" + body


def skill_files(skills_dir: Path) -> list[Path]:
    return [Path(skills_dir) / name / "SKILL.md" for name in SKILLS]


def install_skills(skills_dir: Path) -> list[Path]:
    """Write the seven SKILL.md files (idempotent). Returns their paths."""
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
    """Remove exactly the seven managed skill dirs. Returns removed dirs."""
    removed = []
    for name in SKILLS:
        d = Path(skills_dir) / name
        if d.is_dir():
            shutil.rmtree(d)
            removed.append(d)
    return removed
