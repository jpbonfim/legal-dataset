"""Token counting with a Hugging Face tokenizer, with a clearly labelled fallback."""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# Rough average for Portuguese legal text with modern BPE tokenizers; only used when the
# configured tokenizer cannot be loaded, and always reported as an estimate.
ESTIMATED_CHARS_PER_TOKEN = 4.0


class TokenCounter(Protocol):
    name: str
    is_estimate: bool

    def count(self, texts: list[str]) -> list[int]: ...


class HuggingFaceTokenCounter:
    is_estimate = False

    def __init__(self, tokenizer_name: str) -> None:
        from tokenizers import Tokenizer

        self.name = tokenizer_name
        self._tokenizer = Tokenizer.from_pretrained(tokenizer_name)

    def count(self, texts: list[str]) -> list[int]:
        encodings = self._tokenizer.encode_batch(texts, add_special_tokens=False)
        return [len(encoding.ids) for encoding in encodings]


class EstimatedTokenCounter:
    is_estimate = True

    def __init__(self, reason: str) -> None:
        self.name = f"estimate (~{ESTIMATED_CHARS_PER_TOKEN:g} chars/token; {reason})"

    def count(self, texts: list[str]) -> list[int]:
        return [round(len(text) / ESTIMATED_CHARS_PER_TOKEN) for text in texts]


def load_token_counter(tokenizer_name: str) -> TokenCounter:
    try:
        return HuggingFaceTokenCounter(tokenizer_name)
    except Exception as error:  # network, missing repo, auth...
        logger.warning("could not load tokenizer %s: %s", tokenizer_name, error)
        return EstimatedTokenCounter(f"tokenizer {tokenizer_name!r} unavailable")
