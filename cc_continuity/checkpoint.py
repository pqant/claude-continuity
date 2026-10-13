"""Checkpoint engine (PRD §10). Implemented in Phase 4.

- Draft validation: 9 template headings in order (Appendix B), required
  sections non-empty, ≤ ``handoff_max_words`` words, no redact-pattern
  secrets (PRD §10.1).
- Deterministic snapshot (PRD §10.2) produced by code only.
- Git tree snapshot via :mod:`cc_continuity.gitsnap`.
- Finalization (PRD §10.4): render handoff.md into ``checkpoint.tmp-<pid>/``,
  fsync, SHA256SUMS, re-read/verify, ``os.replace`` → ``checkpoint``; only
  then ``CHECKPOINT_VERIFIED`` + ``latest_checkpoint`` pointer.
- Degraded checkpoint (deterministic parts only, C.7 header) after 3 failed
  handoff attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import (atomic, config, gitsnap, ledger, lock, naming, notify, paths,
               redact, state)

#: Appendix B, exact order and exact strings.
TEMPLATE_HEADINGS: tuple[str, ...] = (
    "Objective",
    "Current Task and Exact Stopping Point",
    "Completed This Session",
    "Next Atomic Action",
    "Remaining Plan",
    "Decisions and Rationale",
    "Rejected Approaches — Do Not Repeat",
    "Open Problems, Risks, Questions for the User",
    "Runtime Environment Notes",
)
#: 0-based indices into TEMPLATE_HEADINGS of sections that must be
#: non-empty (PRD §10.1: sections 1, 2, 4 of the template).
REQUIRED_NONEMPTY: tuple[int, ...] = (0, 1, 3)
MIN_REQUIRED_CHARS = 20

#: Appendix C.7 (degraded checkpoint header).
C7 = ("DEGRADED CHECKPOINT: the model did not produce a valid handoff; only "
      "deterministic state is available. Reconstruct intent from the user "
      "prompts and changed files before continuing.")


class CheckpointError(RuntimeError):
    """Checkpoint build/finalization failed (message is user-facing)."""


@dataclass
class DraftReport:
    ok: bool
    problems: list          # hard failures → C.6 text
    warnings: int           # redactions applied (non-failing)
    redacted_text: str
    word_count: int


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _parse_headings(text: str) -> list[tuple[int, str]]:
    out = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip()))
    return out


def _section_bodies(text: str) -> dict:
    """Map level-2 title → body text (up to the next heading of any level)."""
    bodies: dict = {}
    current = None
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 2:
                current = m.group(2).strip()
                bodies.setdefault(current, "")
            else:
                current = None
        elif current is not None:
            bodies[current] += line + "\n"
    return bodies


def validate_draft(text: str, cfg: dict) -> DraftReport:
    """PRD §10.1 — validate a model-written handoff draft.

    ``problems`` holds hard failures (draft unusable, C.6). Secret matches
    are replaced with ``[REDACTED]`` in ``redacted_text`` and counted in
    ``warnings`` — redaction never fails the draft.
    """
    redacted_text, warnings = redact.Redactor(config.redact_patterns(cfg)).redact(text)
    problems: list = []
    h2 = [t for lvl, t in _parse_headings(redacted_text) if lvl == 2]
    for h in TEMPLATE_HEADINGS:
        if h not in h2:
            problems.append(f"missing heading: {h}")
    seen: list = []
    for h in h2:
        if h in TEMPLATE_HEADINGS and h not in seen:
            seen.append(h)
    expected = [h for h in TEMPLATE_HEADINGS if h in h2]
    if seen != expected:
        problems.append(f"headings out of order: got {seen}")
    for h in dict.fromkeys(h2):
        if h in TEMPLATE_HEADINGS and h2.count(h) > 1:
            problems.append(f"duplicate heading: {h}")
    for h in dict.fromkeys(h2):
        if h not in TEMPLATE_HEADINGS:
            problems.append(f"unexpected heading: {h}")
    bodies = _section_bodies(redacted_text)
    for idx in REQUIRED_NONEMPTY:
        h = TEMPLATE_HEADINGS[idx]
        if h in bodies and len(bodies[h].strip()) < MIN_REQUIRED_CHARS:
            problems.append(f"section too short (< {MIN_REQUIRED_CHARS} chars): {h}")
    word_count = len(redacted_text.split())
    max_words = cfg.get("handoff_max_words", 5000)
    if word_count > max_words:
        problems.append(f"too long: {word_count} > {max_words} words")
    return DraftReport(ok=not problems, problems=problems, warnings=warnings,
                       redacted_text=redacted_text, word_count=word_count)


def headings_in_order(text: str) -> bool:
    """True when all 9 template headings appear, in order, among the H2s.

    Used on the FINAL handoff.md, whose appendix adds extra H2s (allowed).
    """
    h2 = [t for lvl, t in _parse_headings(text) if lvl == 2]
    it = iter(h2)
    return all(any(h == t for t in it) for h in TEMPLATE_HEADINGS)


MAX_STATUS_LINES = 200    # PRD §10.2
MAX_DIFF_LINES = 100


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    # rstrip the newline only — leading spaces are significant (porcelain
    # status encodes staged/unstaged in the first two columns).
    return out.returncode, out.stdout.rstrip("\n")


def git_facts(root: Path) -> dict:
    """PRD §10.2 working-tree facts. Non-git dir → nulls/empties (no raise)."""
    facts: dict = {"git_root": None, "branch": None, "head": None,
                   "ahead": None, "behind": None, "status": [], "diff_stat": [],
                   "staged": 0, "unstaged": 0}
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return facts
    facts["git_root"] = top
    rc, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    facts["branch"] = branch or None
    rc, head = _git(root, "rev-parse", "HEAD")
    facts["head"] = head if rc == 0 else None
    rc, upstream = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    if rc == 0 and upstream:
        rc, counts = _git(root, "rev-list", "--left-right", "--count",
                          f"{upstream}...HEAD")
        if rc == 0:
            parts = counts.split()  # tab-separated, e.g. "0\t1"
            if len(parts) == 2:
                facts["behind"], facts["ahead"] = int(parts[0]), int(parts[1])
    rc, status = _git(root, "status", "--porcelain=v1")
    lines = status.splitlines() if rc == 0 else []
    facts["status"] = lines[:MAX_STATUS_LINES]
    staged = unstaged = 0
    for line in lines:
        if len(line) >= 2:
            if line[0] in "MADRC":
                staged += 1
            if line[1] in "MD":
                unstaged += 1
    facts["staged"], facts["unstaged"] = staged, unstaged
    rc, stat = _git(root, "diff", "--stat", "HEAD")
    if rc != 0:                      # unborn HEAD → staged vs empty tree
        rc, stat = _git(root, "diff", "--cached", "--stat")
    facts["diff_stat"] = stat.splitlines()[:MAX_DIFF_LINES] if rc == 0 else []
    return facts


MAX_TOUCHED = 50
MAX_COMMANDS = 30
MAX_PROMPTS = 10
PROMPT_CHARS = 500


def ledger_summary(session_dir: Path) -> dict:
    """PRD §10.2 — aggregate the session ledger into snapshot sections.

    Inputs are already redacted/truncated by the Phase 3 hooks.
    """
    entries = ledger.read(session_dir)
    touched: dict = {}
    order: list = []
    commands: list = []
    failed: list = []
    tasks: dict = {}
    prompts: list = []
    for e in entries:
        t = e.get("type")
        if t == "tool_use" and e.get("file_path"):
            p = e["file_path"]
            if p not in touched:
                touched[p] = {"path": p, "op": e.get("tool"), "count": 0}
                order.append(p)
            touched[p]["count"] += 1
            touched[p]["op"] = e.get("tool")
        elif t == "tool_use" and e.get("tool") == "Bash" and e.get("command"):
            commands.append({"command": e["command"],
                             "interrupted": bool(e.get("interrupted"))})
        elif t == "tool_failure":
            failed.append({"tool": e.get("tool"),
                           "command": e.get("command"),
                           "error_first_line": e.get("error_first_line")})
        elif t in ("task_created", "task_completed"):
            tid = e.get("task_id")
            if tid is None:
                continue
            task = tasks.setdefault(tid,
                                    {"id": tid, "subject": None, "status": "created"})
            if e.get("subject"):
                task["subject"] = e["subject"]
            if e.get("status"):
                task["status"] = e["status"]
        elif t == "user_prompt":
            prompts.append(str(e.get("text", ""))[:PROMPT_CHARS])
    return {
        "files_touched": [touched[p] for p in reversed(order)][:MAX_TOUCHED],
        "commands": commands[-MAX_COMMANDS:],
        "failed_commands": failed,
        "tasks": list(tasks.values()),
        "user_prompts": prompts[-MAX_PROMPTS:],
    }


def build_snapshot(*, key: str, nnn: int, root: Path, cfg: dict,
                   metric: dict | None, degraded: bool, created: str,
                   state_record: dict, project_record: dict) -> dict:
    """PRD §10.2 — the deterministic part, produced by code only.

    ``created`` comes from the state record (never ``time.time()`` here) so
    a run's output is deterministic for its inputs.
    """
    root = Path(root)
    gf = git_facts(root)
    slug = project_record.get("slug") or naming.slug_for_dir(root)
    snap: dict = {"ref": None, "commit": None, "tree": None}
    snap_error: str | None = None
    if cfg.get("git_snapshot") and gf["git_root"] is not None:
        try:
            snap = gitsnap.make_snapshot(root, slug, nnn)
            gitsnap.prune_snapshots(root, slug,
                                    int(cfg.get("snapshot_retention", 20)))
        except gitsnap.GitSnapError as e:
            snap, snap_error = {"ref": None, "commit": None, "tree": None}, str(e)
    metric = metric or {}
    return {
        "schema_version": 1,
        "checkpoint": f"cp-{naming.pad_nnn(nnn)}",
        "created": created,
        "degraded": degraded,
        "session": {
            "name": state_record["name"],
            "nnn": nnn,
            "session_id": state_record["session_id"],
            "predecessor": state_record.get("predecessor"),
            "successor": state_record.get("successor"),
            "cc_version": metric.get("cc_version"),
            "ratio": metric.get("ratio"),
            "window_tokens": metric.get("window_tokens"),
            "state_at_checkpoint": state_record["state"],
        },
        "project": {
            "root": str(root),
            "slug": slug,
            "git_root": gf["git_root"],
            "branch": gf["branch"],
            "head": gf["head"],
            "ahead": gf["ahead"],
            "behind": gf["behind"],
        },
        "git": {
            "status": gf["status"],
            "diff_stat": gf["diff_stat"],
            "staged": gf["staged"],
            "unstaged": gf["unstaged"],
            "snapshot": snap if snap["ref"] else None,
            "snapshot_error": snap_error,
        },
        **ledger_summary(paths.session_dir(key, nnn)),
    }


def render_handoff(snapshot: dict, draft_text: str | None,
                   degraded: bool) -> str:
    """PRD §10.4 step 1 — meta header + model sections (or C.7) + appendix."""
    s, p, g = snapshot["session"], snapshot["project"], snapshot["git"]
    lines: list = [
        f"# Handoff {snapshot['checkpoint']} — "
        f"{naming.logical_name(p['slug'], s['nnn'])}",
        "",
        f"- session: `{s['name']}` ({naming.pad_nnn(s['nnn'])}) "
        f"· sid `{s['session_id']}`",
        f"- predecessor: `{s['predecessor']}`" if s["predecessor"]
        else "- predecessor: —",
        f"- created: {snapshot['created']}",
        f"- cc version: {s['cc_version'] or '—'}",
    ]
    ratio = s["ratio"]
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        wt = s.get("window_tokens")
        lines.append(f"- ratio at rotation: {ratio * 100:.1f}%"
                     + (f" (window {wt})" if wt else ""))
    else:
        lines.append("- ratio at rotation: —")
    proj = f"- project: {p['root']}"
    if p["branch"]:
        proj += f" (branch `{p['branch']}`"
        if p["head"]:
            proj += f", HEAD `{p['head'][:7]}`"
        proj += ")"
    lines.append(proj)
    snap = g.get("snapshot")
    if snap:
        lines.append(f"- git snapshot: `{snap['ref']}` @ `{snap['commit'][:7]}`"
                     f" (tree `{snap['tree'][:7]}`)")
    elif g.get("snapshot_error"):
        lines.append(f"- git snapshot: — ({g['snapshot_error']})")
    else:
        lines.append("- git snapshot: —")
    lines.append("")
    if degraded:
        lines += [f"> {C7}", ""]
    else:
        lines += [draft_text.rstrip(), ""]
    lines += ["## Appendix — Deterministic state (auto-generated)", ""]
    lines.append(f"### Git working tree (staged: {g['staged']} · "
                 f"unstaged: {g['unstaged']})")
    lines.append("")
    if g["status"]:
        lines += ["```"] + g["status"] + ["```", ""]
    else:
        lines += ["(working tree clean)", ""]
    if g["diff_stat"]:
        lines += ["```"] + g["diff_stat"] + ["```", ""]
    lines += ["### Files touched this session (last 50)", ""]
    lines += [f"- `{f['path']}` — {f['op']} ×{f['count']}"
              for f in snapshot["files_touched"]] or ["(none recorded)"]
    lines += ["", "### Commands run this session (last 30)", ""]
    lines += [f"- `{c['command']}`" + (" (interrupted)" if c["interrupted"] else "")
              for c in snapshot["commands"]] or ["(none recorded)"]
    lines += ["", "### Failed commands", ""]
    lines += [f"- {f['tool']}: {f.get('error_first_line') or 'no error line'}"
              + (f" — `{f['command']}`" if f.get("command") else "")
              for f in snapshot["failed_commands"]] or ["(none recorded)"]
    lines += ["", "### Tasks (last known)", ""]
    lines += [f"- {t['id']} {t['subject'] or ''} — {t['status']}".rstrip()
              for t in snapshot["tasks"]] or ["(none recorded)"]
    lines += ["", "### User prompts this session (last 10)", ""]
    lines += [f"> {pr}" for pr in snapshot["user_prompts"]] or ["(none recorded)"]
    return "\n".join(lines).rstrip() + "\n"


HANDOFF_WALK: dict[str, list[str]] = {
    "BOOTSTRAPPING": ["ACTIVE", "ROTATION_PENDING", "HANDOFF_REQUESTED"],
    "ACTIVE": ["ROTATION_PENDING", "HANDOFF_REQUESTED"],
    "WARNED": ["ROTATION_PENDING", "HANDOFF_REQUESTED"],
    "ROTATION_PENDING": ["HANDOFF_REQUESTED"],
    "HANDOFF_REQUESTED": [],
    "HANDOFF_FAILED": [],
    "CHECKPOINT_WRITING": [],
}


def _read_project_record(key: str) -> dict:
    """Lock-free read — callers hold the project lock."""
    pfile = paths.project_dir(key) / "project.json"
    return json.loads(pfile.read_text(encoding="utf-8"))


def _write_file_fsync(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _verify_checkpoint_dir(tmp: Path, *, snapshot: dict, state_record: dict,
                           root: Path, degraded: bool) -> None:
    """PRD §10.4 step 3 — re-read and verify. Raises CheckpointError."""
    for name in ("handoff.md", "state.json", "snapshot.json",
                 "ledger.jsonl", "SHA256SUMS"):
        if not (tmp / name).is_file():
            raise CheckpointError(f"verify: missing {name}")
    sums: dict = {}
    for line in (tmp / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest
    expected = {"handoff.md", "state.json", "snapshot.json", "ledger.jsonl"}
    if set(sums) != expected:
        raise CheckpointError("verify: SHA256SUMS covers wrong file set")
    for name, digest in sums.items():
        actual = hashlib.sha256((tmp / name).read_bytes()).hexdigest()
        if actual != digest:
            raise CheckpointError(f"verify: checksum mismatch: {name}")
    try:
        cp_state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
        cp_snap = json.loads((tmp / "snapshot.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CheckpointError(f"verify: unparseable JSON: {e}")
    handoff = (tmp / "handoff.md").read_text(encoding="utf-8")
    if degraded:
        if C7 not in handoff:
            raise CheckpointError("verify: degraded header missing")
    elif not headings_in_order(handoff):
        raise CheckpointError("verify: handoff headings missing/out of order")
    if cp_snap["project"]["root"] != str(root):
        raise CheckpointError("verify: project_dir mismatch")
    if (cp_snap["session"]["session_id"] != state_record["session_id"]
            or cp_state["session_id"] != state_record["session_id"]):
        raise CheckpointError("verify: session_id mismatch")


def _set_latest_checkpoint(key: str, nnn: int, snapshot: dict,
                           final: Path) -> None:
    pfile = paths.project_dir(key) / "project.json"
    record = json.loads(pfile.read_text(encoding="utf-8"))
    record["latest_checkpoint"] = {
        "cp_id": snapshot["checkpoint"],
        "nnn": nnn,
        "created": snapshot["created"],
        "handoff_path": str(final / "handoff.md"),
        "pending_consumption": True,
    }
    atomic.atomic_write_json(pfile, record)


def finalize(key: str, nnn: int, *, cfg: dict, root: Path,
             metric: dict | None = None,
             fault: Callable[[int], None] | None = None) -> tuple[str, bool]:
    """PRD §10.4 — build + finalize the checkpoint for session ``nnn``.

    The whole sequence runs under the project lock. ``fault(step)`` (tests
    only) may raise before a step to simulate a kill; step numbers are
    documented in the module plan (0 stale-tmp … 8 state+pointer). A crash
    at any point leaves either the previous valid checkpoint or a
    ``checkpoint.tmp-*`` dir, never ``CHECKPOINT_VERIFIED`` state.
    Returns ``(checkpoint_id, degraded)``.
    """
    root = Path(root)

    def step(i: int) -> None:
        if fault is not None:
            fault(i)

    with lock.held(key):
        step(0)
        pdir = paths.session_dir(key, nnn)
        for stale in pdir.glob("checkpoint.tmp-*"):
            shutil.rmtree(stale, ignore_errors=True)
        step(1)
        record = state.load_state(key, nnn)
        if record["state"] not in HANDOFF_WALK:
            raise CheckpointError(
                f"cannot checkpoint from state {record['state']} "
                f"(session {nnn:03d})")
        for target in HANDOFF_WALK[record["state"]]:
            state._transition_unlocked(key, nnn, target)
        state._transition_unlocked(key, nnn, "CHECKPOINT_WRITING")
        record = state.load_state(key, nnn)
        step(2)
        draft_path = pdir / "handoff.draft.md"
        draft_text = None
        if draft_path.is_file():
            report = validate_draft(draft_path.read_text(encoding="utf-8"), cfg)
            if report.ok:
                draft_text = report.redacted_text
        degraded = draft_text is None
        step(3)
        snapshot = build_snapshot(key=key, nnn=nnn, root=root, cfg=cfg,
                                  metric=metric, degraded=degraded,
                                  created=record["updated"],
                                  state_record=record,
                                  project_record=_read_project_record(key))
        step(4)
        handoff = render_handoff(snapshot, draft_text, degraded)
        step(5)
        tmp = pdir / f"checkpoint.tmp-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        state_copy = dict(record)
        state_copy["degraded"] = degraded
        _write_file_fsync(tmp / "handoff.md", handoff.encode("utf-8"))
        _write_file_fsync(tmp / "state.json",
                          (json.dumps(state_copy, indent=2, ensure_ascii=False)
                           + "\n").encode("utf-8"))
        _write_file_fsync(tmp / "snapshot.json",
                          (json.dumps(snapshot, indent=2, ensure_ascii=False)
                           + "\n").encode("utf-8"))
        ledger_file = pdir / "ledger.jsonl"
        _write_file_fsync(tmp / "ledger.jsonl",
                          ledger_file.read_bytes() if ledger_file.is_file()
                          else b"")
        sum_lines = []
        for name in ("handoff.md", "state.json", "snapshot.json",
                     "ledger.jsonl"):
            digest = hashlib.sha256((tmp / name).read_bytes()).hexdigest()
            sum_lines.append(f"{digest}  {name}")
        _write_file_fsync(tmp / "SHA256SUMS",
                          ("\n".join(sum_lines) + "\n").encode("utf-8"))
        step(6)
        try:
            _verify_checkpoint_dir(tmp, snapshot=snapshot, state_record=record,
                                   root=root, degraded=degraded)
        except CheckpointError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        step(7)
        final = pdir / "checkpoint"
        if final.exists():
            shutil.rmtree(final)
        os.rename(tmp, final)
        atomic.fsync_dir(pdir)
        step(8)
        state._transition_unlocked(key, nnn, "CHECKPOINT_VERIFIED",
                                   degraded=degraded)
        _set_latest_checkpoint(key, nnn, snapshot, final)
    # Phase 9 — spec §5 rows 4/8 (P9-D7: push must never break finalize)
    slug = notify.project_slug(key)
    if degraded:
        notify.safe_send(cfg, "error", slug=slug, nnn=nnn,
                         dedup_key=f"degraded:cp-{nnn:03d}",
                         what="checkpoint",
                         detail="degraded — handoff draft missing/invalid")
    else:
        notify.safe_send(cfg, "handoff_verified", slug=slug, nnn=nnn)
    return snapshot["checkpoint"], degraded
