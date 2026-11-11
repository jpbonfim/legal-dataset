from __future__ import annotations

from legis_dataset.processing.dedup import Deduplicator
from legis_dataset.processing.quality import QualityRules, rejection_reason

BASE = " ".join(f"Art. {i}º Fica estabelecida a regra número {i} desta norma." for i in range(60))


def test_exact_and_near_duplicates_point_to_first_copy() -> None:
    deduplicator = Deduplicator(threshold=0.9, permutations=128)
    assert deduplicator.decide("a", BASE).duplicate_of is None
    exact = deduplicator.decide("b", BASE)
    assert (exact.duplicate_of, exact.kind) == ("a", "exact")
    near = deduplicator.decide("c", BASE + " Art. 61. Vigência imediata.")
    assert (near.duplicate_of, near.kind) == ("a", "near")
    other = "Art. 1º " + " ".join(f"palavra{i}" for i in range(400))
    assert deduplicator.decide("d", other).duplicate_of is None


def test_quality_rules() -> None:
    rules = QualityRules(min_chars=50)
    assert rejection_reason("Página vazia.", rules) == "too_short"
    assert rejection_reason("Sem artigos aqui. " * 10, rules) == "no_articles"
    # A short text with an article is a norm in force, never dropped for its size.
    assert rejection_reason("Art. 1º Curto.", rules) is None
    # A whole norm struck through by the Planalto leaves only the header with the notice.
    revoked = (
        "DECRETO Nº 12.343, DE 30 DE DEZEMBRO DE 2024 Revogado pelo Decreto nº 12.807, de 2025"
    )
    assert rejection_reason(revoked, rules) == "revoked_in_source"
    assert rejection_reason(f"{revoked}\nArt. 1º Texto que sobrou.", rules) is None
    assert rejection_reason("Art. 1º InformaÃ§Ã£o quebrada Ã§ " * 5, rules) == "broken_encoding"
    assert rejection_reason(BASE, rules) is None
