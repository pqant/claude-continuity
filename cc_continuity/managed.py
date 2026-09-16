"""Managed text blocks for user files (PRD §17.4/§17.6, P7-D6/P7-D7).

A block is delimited by two marker LINES (exact line match):

    <begin>
    ...content...
    <end>

``upsert_block`` inserts (appending, normalized to exactly one blank
separator line) or replaces in place; ``remove_block`` deletes exactly the
marker lines plus body. Both operate on raw text, so unmanaged content is
preserved byte-for-byte. Gate 7 relies on the round-trip invariant
``remove_block(upsert_block(t)) == t`` (files that lack a trailing newline
receive one — documented normalization).
"""

from __future__ import annotations


def _locate(lines: list[str], begin: str, end: str) -> tuple[int, int] | None:
    """(begin_idx, end_idx) of the FIRST well-formed marker pair, else None.

    A begin line with no following end line is malformed -> treated as
    "no block" (returns None).
    """
    for i, line in enumerate(lines):
        if line != begin:
            continue
        for j in range(i + 1, len(lines)):
            if lines[j] == end:
                return (i, j)
        return None
    return None


def block_present(text: str, begin: str, end: str) -> bool:
    return _locate(text.split("\n"), begin, end) is not None


def _drop_duplicates(lines: list[str], begin: str, end: str) -> list[str]:
    """Keep the first well-formed block, delete any further ones."""
    out = list(lines)
    while True:
        loc = _locate(out, begin, end)
        if loc is None:
            return out
        # remove all but the first: drop blocks after the first pair
        first = loc
        loc = _locate(out[first[1] + 1:], begin, end)
        if loc is None:
            return out
        gi = first[1] + 1 + loc[0]
        gj = first[1] + 1 + loc[1]
        del out[gi: gj + 1]


def upsert_block(text: str, begin: str, end: str, content: str) -> str:
    """Insert or replace the managed block (contract in Interfaces section)."""
    lines = text.split("\n")
    rendered = [begin] + content.split("\n") + [end]
    loc = _locate(lines, begin, end)
    if loc is not None:
        new = lines[: loc[0]] + rendered + lines[loc[1] + 1:]
        return "\n".join(_drop_duplicates(new, begin, end))
    # absent -> append; normalize trailing newline, collapse trailing blanks
    body = lines[:-1] if lines and lines[-1] == "" else lines
    while body and body[-1] == "":
        body.pop()
    if body:
        return "\n".join(body + [""] + rendered + [""])
    # empty file: the block itself is the whole file (no leading blank line)
    return "\n".join(rendered + [""])


def remove_block(text: str, begin: str, end: str) -> str:
    """Delete the managed block; text without a block is returned unchanged.

    The tail is normalized to exactly one trailing newline, which makes
    ``remove_block(upsert_block(t)) == t`` hold for every ``t`` ending in
    exactly one newline (Gate 7 files do).
    """
    lines = text.split("\n")
    loc = _locate(lines, begin, end)
    if loc is None:
        return text
    new = lines[: loc[0]] + lines[loc[1] + 1 :]
    while len(new) > 1 and new[-1] == "":
        new.pop()
    if new == [""]:
        return ""
    return "\n".join(new) + "\n"
