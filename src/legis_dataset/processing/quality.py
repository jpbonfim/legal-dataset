"""Document-level quality filters. Each returns a rejection reason or ``None``."""

from __future__ import annotations

from dataclasses import dataclass

from legis_dataset.parsing.lines import ARTICLE_START
from legis_dataset.processing.normalize import mojibake_ratio

MAX_MOJIBAKE_PER_1000_CHARS = 1.0


@dataclass(frozen=True)
class QualityRules:
    min_chars: int


def rejection_reason(text: str, rules: QualityRules) -> str | None:
    if len(text) < rules.min_chars:
        return "too_short"
    if mojibake_ratio(text) > MAX_MOJIBAKE_PER_1000_CHARS:
        return "broken_encoding"
    if not any(ARTICLE_START.match(line) for line in text.split("\n")):
        return "no_articles"
    return None
