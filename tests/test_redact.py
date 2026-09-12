"""redact.py: secret redaction for ledger and handoffs (PRD §15, §19)."""

from cc_continuity import redact


def test_key_value_pattern_variants():
    r = redact.Redactor()
    for text in [
        "api_key = supersecret123",
        "API_KEY: hunter2",
        "token=abc.def.ghi",
        "my-secret: s3cret-value",
        "password = p@ss w0rd",  # stops at whitespace
    ]:
        out, n = r.redact(text)
        assert n >= 1, text
        assert "[REDACTED]" in out, text


def test_plain_words_without_separator_not_redacted():
    r = redact.Redactor()
    text = "the token in this sentence has no separator"
    out, n = r.redact(text)
    assert out == text
    assert n == 0


def test_sk_token_pattern():
    r = redact.Redactor()
    key = "sk-" + "A" * 28  # 20+ chars
    out, n = r.redact(f"use {key} now")
    assert n == 1 and key not in out
    short = "sk-" + "A" * 19  # too short
    out2, n2 = r.redact(short)
    assert n2 == 0 and out2 == short


def test_ghp_pattern():
    r = redact.Redactor()
    tok = "ghp_" + "b" * 36
    out, n = r.redact(tok)
    assert n == 1 and tok not in out


def test_multiple_secrets_counted():
    r = redact.Redactor()
    text = f"password = one and token=two"
    out, n = r.redact(text)
    assert n == 2
    assert "one" not in out and "two" not in out


def test_redaction_is_idempotent():
    r = redact.Redactor()
    text = "password = hunter2 and done"
    once, _ = r.redact(text)
    twice, n = r.redact(once)
    assert twice == once
    assert n == 0


def test_custom_patterns_replace_defaults():
    r = redact.Redactor([r"CID-\d+"])
    out, n = r.redact("ticket CID-12345 closed")
    assert n == 1 and "CID-12345" not in out
    # Default patterns must NOT apply when custom ones are given.
    out2, n2 = r.redact("password = hunter2")
    assert n2 == 0 and out2 == "password = hunter2"


def test_convenience_function_returns_str():
    result = redact.redact("password = hunter2")
    assert isinstance(result, str)
    assert "hunter2" not in result
