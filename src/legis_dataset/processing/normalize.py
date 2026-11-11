"""Character-level normalization shared by parsers and the cleaning stage."""

from __future__ import annotations

import re
import unicodedata

_TRANSLATION = str.maketrans(
    {
        " ": " ",  # no-break space
        " ": " ",
        " ": " ",
        "​": "",  # zero-width space
        "﻿": "",
        "­": "",  # soft hyphen
        "\t": " ",
        "\r": "",
    }
)
_SPACES = re.compile(r"[ ]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r" +([,.;:!?)\]])")
_SPACE_AFTER_OPENING = re.compile(r"([(\[]) +")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_line(line: str) -> str:
    """NFC, unify invisible/odd spaces, collapse runs of spaces, trim."""
    line = unicodedata.normalize("NFC", line).translate(_TRANSLATION)
    line = _SPACES.sub(" ", line)
    line = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", line)
    line = _SPACE_AFTER_OPENING.sub(r"\1", line)
    return line.strip()


def normalize_text(text: str) -> str:
    lines = (normalize_line(line) for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


# Typical mojibake produced by decoding UTF-8 bytes as Latin-1/cp1252 ("Ã§", "Ã£", ...).
_MOJIBAKE = re.compile(r"Ã[\u0080-¿§£¡¢©ª³µº¼½¾]|Â[ -¿]|�")


def mojibake_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_MOJIBAKE.findall(text)) / max(1, len(text) / 1000)
