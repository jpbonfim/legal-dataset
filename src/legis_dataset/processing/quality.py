"""Document-level quality filters. Each returns a rejection reason or ``None``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from legis_dataset.parsing.lines import ARTICLE_START
from legis_dataset.processing.normalize import mojibake_ratio

MAX_MOJIBAKE_PER_1000_CHARS = 1.0

# Notice that a whole norm was revoked, as the Planalto prints it under the title, e.g.
# "Revogado pelo Decreto nº 12.807, de 2025". The revoked text itself is struck through
# and removed by the parser, so only the header with this notice is left.
REVOCATION_NOTICE = re.compile(r"\bRevogad[oa]\s+(?:pel[oa]|integralmente)\b", re.IGNORECASE)


@dataclass(frozen=True)
class QualityRules:
    #: Below this size, a text without any article is reported as ``too_short`` (an empty
    #: or broken page) rather than ``no_articles``. Texts with articles are never dropped
    #: for their size: a short law in force is still a law in force.
    min_chars: int


def rejection_reason(text: str, rules: QualityRules) -> str | None:
    if not any(ARTICLE_START.match(line) for line in text.split("\n")):
        if REVOCATION_NOTICE.search(text):
            return "revoked_in_source"
        if len(text) < rules.min_chars:
            return "too_short"
        return "no_articles"
    if mojibake_ratio(text) > MAX_MOJIBAKE_PER_1000_CHARS:
        return "broken_encoding"
    return None
