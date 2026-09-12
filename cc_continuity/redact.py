"""Secret redaction for ledger entries and handoffs (PRD §15, §19).

Patterns come from the merged config's ``redact_patterns`` (PRD defaults below).
Every match is replaced with a single ``[REDACTED]`` marker; the replacement is
idempotent (``[REDACTED]`` itself never matches the default patterns).
"""

from __future__ import annotations

import re
from typing import Sequence

from . import config

REPLACEMENT = "[REDACTED]"


class Redactor:
    """Compiled set of redaction patterns. None → PRD §15 defaults."""

    def __init__(self, patterns: Sequence[str | re.Pattern] | None = None):
        if patterns is None:
            patterns = config.DEFAULTS["redact_patterns"]
        self._patterns = [
            p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns
        ]

    @property
    def patterns(self) -> list[re.Pattern]:
        return list(self._patterns)

    def redact(self, text: str) -> tuple[str, int]:
        """Return (redacted_text, replacement_count)."""
        count = 0
        out = text
        for pat in self._patterns:
            out, n = pat.subn(REPLACEMENT, out)
            count += n
        return out, count

    def has_secrets(self, text: str) -> bool:
        """True if any pattern matches (used by the draft validator)."""
        return any(pat.search(text) for pat in self._patterns)


def redact(text: str, patterns: Sequence[str | re.Pattern] | None = None) -> str:
    """Convenience: redacted string only."""
    return Redactor(patterns).redact(text)[0]
