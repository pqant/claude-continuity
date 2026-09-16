"""managed.py: delimited text blocks (PRD §17.4/§17.6)."""

from cc_continuity import managed

B = "<!-- cc-continuity:begin -->"
E = "<!-- cc-continuity:end -->"
ZB = "# >>> cc-continuity >>>"
ZE = "# <<< cc-continuity <<<"


def test_upsert_into_empty_file():
    assert managed.upsert_block("", B, E, "hello") == f"{B}\nhello\n{E}\n"


def test_upsert_appends_with_single_blank_separator():
    out = managed.upsert_block("hello\n", B, E, "c1\nc2")
    assert out == f"hello\n\n{B}\nc1\nc2\n{E}\n"


def test_upsert_without_trailing_newline_normalizes():
    out = managed.upsert_block("hello", B, E, "c")
    assert out == f"hello\n\n{B}\nc\n{E}\n"


def test_upsert_collapses_existing_trailing_blanks_to_one():
    out = managed.upsert_block("hello\n\n", B, E, "c")
    assert out == f"hello\n\n{B}\nc\n{E}\n"


def test_upsert_replaces_in_place():
    mid = f"a\n{B}\nOLD\n{E}\nb\n"
    out = managed.upsert_block(mid, B, E, "NEW1\nNEW2")
    assert out == f"a\n{B}\nNEW1\nNEW2\n{E}\nb\n"


def test_upsert_idempotent_on_same_content():
    once = managed.upsert_block("t\n", ZB, ZE, "fn")
    assert managed.upsert_block(once, ZB, ZE, "fn") == once


def test_remove_block_roundtrip_appended():
    assert managed.remove_block(managed.upsert_block("hello\n", B, E, "c"), B, E) == "hello\n"


def test_remove_block_roundtrip_empty_file():
    assert managed.remove_block(managed.upsert_block("", ZB, ZE, "c"), ZB, ZE) == ""


def test_remove_block_roundtrip_middle():
    mid = f"a\n{B}\nOLD\n{E}\nb\n"
    assert managed.remove_block(mid, B, E) == "a\nb\n"


def test_remove_block_absent_unchanged():
    assert managed.remove_block("plain\nfile\n", B, E) == "plain\nfile\n"


def test_multiple_blocks_upsert_keeps_first_removes_rest():
    two = f"{B}\no1\n{E}\nmid\n{B}\no2\n{E}\n"
    out = managed.upsert_block(two, B, E, "new")
    assert out.count(B) == 1 and out.count(E) == 1
    assert out == f"{B}\nnew\n{E}\nmid\n"


def test_malformed_begin_without_end_treated_as_absent():
    # begin token mid-line is NOT a marker (line-exact match) -> treated as
    # absent: a well-formed block is appended, the junk line stays, and
    # remove_block round-trips back to the original text.
    t = "junk <!-- cc-continuity:begin -->\n"
    out = managed.upsert_block(t, B, E, "c")
    assert managed.block_present(out, B, E)
    assert managed.remove_block(out, B, E) == t
