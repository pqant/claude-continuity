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


def _frame_delays_ms(path: Path) -> list[int]:
    """Per-frame delay from each Graphic Control Extension (cs -> ms)."""
    b = path.read_bytes()
    delays = []
    i = 0
    while True:
        i = b.find(b"\x21\xf9", i)
        if i < 0:
            return delays
        if b[i + 2] == 4:  # GCE block size is fixed at 4
            delays.append((b[i + 4] | (b[i + 5] << 8)) * 10)
        i += 2


def test_handoff_frames_are_readable():
    # issue #80: the rotation story frames (the ACTIVE hold, HANDOFF! x2,
    # ROTATING, the successor hold) must dwell long enough to actually read
    delays = _frame_delays_ms(ASSET)
    assert len(delays) == 13
    for idx in (7, 8, 9, 10, 12):
        assert delays[idx] >= 400, (idx, delays[idx])
    # the sweep keeps moving but the whole loop stays followable
    assert min(delays) >= 80
    assert sum(delays) >= 4000
