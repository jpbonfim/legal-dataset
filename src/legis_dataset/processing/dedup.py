"""Exact and near-duplicate detection.

Documents are processed in preference order (the caller sorts them), so the first copy
seen is the one kept and later copies point to it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from datasketch import MinHash, MinHashLSH

_WORD = re.compile(r"\w+", re.UNICODE)
SHINGLE_SIZE = 5


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[bytes]:
    words = _WORD.findall(text.casefold())
    if len(words) < size:
        return {" ".join(words).encode()}
    return {" ".join(words[i : i + size]).encode() for i in range(len(words) - size + 1)}


@dataclass(frozen=True)
class DuplicateDecision:
    doc_id: str
    duplicate_of: str | None  # None means "keep"
    kind: str | None  # "exact" | "near"


class Deduplicator:
    def __init__(self, threshold: float, permutations: int) -> None:
        self._permutations = permutations
        self._lsh = MinHashLSH(threshold=threshold, num_perm=permutations)
        self._hashes: dict[str, str] = {}

    def decide(self, doc_id: str, text: str) -> DuplicateDecision:
        digest = content_hash(text)
        if digest in self._hashes:
            return DuplicateDecision(doc_id, self._hashes[digest], "exact")
        signature = MinHash(num_perm=self._permutations)
        signature.update_batch(list(shingles(text)))
        similar = self._lsh.query(signature)
        if similar:
            return DuplicateDecision(doc_id, sorted(similar)[0], "near")
        self._hashes[digest] = doc_id
        self._lsh.insert(doc_id, signature)
        return DuplicateDecision(doc_id, None, None)
