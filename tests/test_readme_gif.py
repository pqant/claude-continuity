"""Issue #40: README status-line GIF.

The animated status line is the most visible feature of the tool, and
until now the README described it only in words. Pin the shipped asset
(a short GIF: the context-usage sweep plus the rotation handoff
moments, drawn from the real status line renderer) and its position
inside the "How it works" section, so a later edit cannot silently drop
it or move it out of context.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "docs" / "assets" / "cc40-statusline-rotation.gif"
REF = "docs/assets/cc40-statusline-rotation.gif"
TEXT = (ROOT / "README.md").read_text(encoding="utf-8")


def test_gif_asset_is_shipped():
    assert ASSET.is_file()
    assert ASSET.read_bytes()[:6] in (b"GIF87a", b"GIF89a")
    # README comfort: comfortably under GitHub's hard size limit (issue #40).
    assert ASSET.stat().st_size < 500_000


def test_gif_is_embedded_in_how_it_works():
    how = TEXT.index("## How it works")
    nxt = TEXT.index("\n## ", how + 1)
    at = TEXT.index(REF)
    assert how < at < nxt
    assert TEXT.count(REF) == 1


def test_gif_reference_line_has_alt_text():
    line = next(l for l in TEXT.splitlines() if REF in l)
    assert line.startswith("![") and f"]({REF})" in line
    assert line[2:line.index("](")].strip()
