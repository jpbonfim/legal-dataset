from __future__ import annotations

import re

from conftest import fixture_bytes

from legis_dataset.parsing.annotations import extract_annotations, is_annotation
from legis_dataset.parsing.base import get_parser
from legis_dataset.parsing.html_utils import parse, text_of
from legis_dataset.parsing.planalto import remove_struck_text

ANNOTATION_LEFTOVERS = re.compile(r"\((?:Reda[çc][ãa]o dada|Inclu[íi]d|Revogad|Acrescid|Vide)")


def _parse(source: str, fixture: str):
    return get_parser(source).parse(fixture_bytes(fixture), norm_id="x", fallback_title="?")


# ----------------------------------------------------------------- annotations


def test_extract_annotations_keeps_ordinary_parentheses() -> None:
    line = "Art. 3º R$ 20.000,00 (vinte mil reais). (Redação dada pela Lei nº 9.430, de 1996)"
    cleaned, found = extract_annotations(line)
    assert "(vinte mil reais)" in cleaned
    assert "Redação" not in cleaned
    assert [a.kind for a in found] == ["redacao"]
    assert found[0].referenced_norm == "Lei nº 9.430, de 1996"


def test_nested_and_split_annotations() -> None:
    cleaned, found = extract_annotations("§ 1º Texto. (Vide ADI nº 1 (medida cautelar))")
    assert cleaned.strip() == "§ 1º Texto."
    assert found[0].kind == "vide"
    assert is_annotation("(Acrescido pela Medida Provisória nº 627, de 11/11/2013,")
    assert not is_annotation("(vinte mil reais)")


# ------------------------------------------------------------------ normas.leg


def test_normas_leg_text_in_force_without_annotations() -> None:
    document = _parse("normas_leg", "normas_leg_lei_9249_current.html")
    assert document.title == "LEI Nº 9.249, DE 26 DE DEZEMBRO DE 1995"
    assert document.text.startswith(document.title)
    assert "Art. 1º As bases de cálculo" in document.text
    assert not ANNOTATION_LEFTOVERS.search(document.text)
    assert "CÂMARA DOS DEPUTADOS" not in document.text
    # Provisions that are only "(Revogado ...)" or "(VETADO)" disappear entirely.
    assert not re.search(r"^§ 4º\s*$", document.text, re.MULTILINE)
    assert "Art. 33" not in document.text
    # Signatures and the closing formula are removed.
    assert "FERNANDO HENRIQUE CARDOSO" not in document.text
    assert "Independência" not in document.text
    assert len(document.annotations) > 20
    # Words split across <span>s keep their spaces ("o caput deste").
    assert "ocaputdeste" not in document.text


# -------------------------------------------------------------------- Planalto


def test_planalto_removes_struck_text_but_keeps_ordinals() -> None:
    tree = parse(
        "<body><p>§ 1<s>º</s> Vigente. <strike>Revogado.</strike>"
        '<span style="text-decoration: line-through">Antigo.</span></p></body>'
    )
    remove_struck_text(tree)
    assert text_of(tree.body) == "§ 1º Vigente."


def test_planalto_law_only_current_wording() -> None:
    document = _parse("planalto", "planalto_lei_9249.htm")
    assert document.title.startswith("LEI Nº 9.249")
    text = document.text
    assert text.startswith("LEI Nº 9.249")
    # The superseded wording of § 1º (R$ 240.000,00) is struck through on the page.
    assert "240.000,00" not in text
    assert "R$ 20.000,00 (vinte mil reais)" in text
    assert not ANNOTATION_LEFTOVERS.search(text)
    assert "Presidência da República" not in text
    assert "Este texto não substitui" not in text


def test_planalto_compiled_code_keeps_structure() -> None:
    text = _parse("planalto", "planalto_lei_8078_compilado.htm").text
    assert "TÍTULO I\nDos Direitos do Consumidor" in text
    assert "\n\nArt. 2° Consumidor é toda pessoa física" in text
    assert "Regulamento\n" not in text  # navigation links before the body
    assert "Brasília, 11 de setembro de 1990" not in text


def test_planalto_decree_keeps_inciso_labels_and_annexes() -> None:
    text = _parse("planalto", "planalto_decreto_11930.htm").text
    assert "\nI - coordenar e monitorar" in text
    assert "\nANEXO II" in text
    assert "CCE 1.15 | 5,04 | 1 | 5,04" in text
