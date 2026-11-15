"""Print the release notes for one version from CHANGELOG.md (issue #33).

Usage: python tools/changelog_notes.py <version> [changelog-path]

Prints the body of the ``## [<version>]`` section (everything up to the
next ``## `` heading, blank edges trimmed) to stdout. Exits 1 when no
such section exists or its body is empty, so release.yml fails fast
instead of publishing blank notes. Stdlib only: release.yml runs this
on a fresh runner before anything from the project is installed.
"""

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(?P<ver>[^\]]+)\](?:\s+-\s+\d{4}-\d{2}-\d{2})?\s*$")


def extract_notes(text, version):
    """Return the trimmed body of the ``[<version>]`` section, or None."""
    wanted = version[1:] if version.startswith("v") else version
    notes = None
    for line in text.splitlines():
        match = HEADING.match(line)
        if notes is None:
            if match and match.group("ver") == wanted:
                notes = []
        elif line.startswith("## "):
            break
        else:
            notes.append(line)
    if notes is None:
        return None
    body = "\n".join(notes).strip()
    return body or None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Print the CHANGELOG notes for one version.")
    parser.add_argument("version", help="version like 0.3.42 (a leading v is fine)")
    parser.add_argument(
        "changelog", nargs="?",
        default=Path(__file__).resolve().parents[1] / "CHANGELOG.md",
        help="path to the changelog (default: the repo CHANGELOG.md)")
    args = parser.parse_args(argv)
    path = Path(args.changelog)
    notes = None
    if path.is_file():
        notes = extract_notes(path.read_text(encoding="utf-8"), args.version)
    if notes is None:
        shown = args.version[1:] if args.version.startswith("v") else args.version
        print(f"no non-empty CHANGELOG section for [{shown}] in {path}",
              file=sys.stderr)
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
