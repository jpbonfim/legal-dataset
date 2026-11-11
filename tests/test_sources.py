from __future__ import annotations

import orjson
from conftest import fixture_bytes, make_norm

from legis_dataset.models import LegalForce, NormKind, TextVersion
from legis_dataset.sources.normas_leg import payload_to_location
from legis_dataset.sources.planalto import ato_folder, candidate_urls, dotted, text_version_of
from legis_dataset.sources.senado import SENADO_KINDS, item_to_norm


def test_normas_leg_in_force_with_current_text() -> None:
    location = payload_to_location(orjson.loads(fixture_bytes("normas_leg_lei_9249.json")))
    assert location.legal_force is LegalForce.IN_FORCE
    assert location.text_source is not None
    assert location.text_source.text_version is TextVersion.CURRENT
    assert "/api/public/binario/" in location.text_source.url


def test_normas_leg_revoked_and_not_found() -> None:
    revoked = payload_to_location(orjson.loads(fixture_bytes("normas_leg_lei_3071_revoked.json")))
    assert revoked.legal_force is LegalForce.NOT_IN_FORCE
    unknown = payload_to_location(orjson.loads(fixture_bytes("normas_leg_not_found.json")))
    assert unknown.legal_force is LegalForce.UNKNOWN
    assert unknown.text_source is None


def test_senado_mp_editions_become_distinct_urns() -> None:
    payload = orjson.loads(fixture_bytes("senado_lista_mpv_2001.json"))
    items = payload["ListaDocumento"]["documentos"]["documento"]
    norms = [item_to_norm(NormKind.MEDIDA_PROVISORIA, item) for item in items]
    first = norms[0]
    assert first is not None
    assert first.norm_id == "urn:lex:br:federal:medida.provisoria:2001-09-06;2229-43"
    assert first.extra == {
        "senado_identifier": "MPV-2229-43-2001-09-06",
        "base_number": "2229",
        "edition": "43",
    }


def test_senado_kinds_cover_every_norm_kind() -> None:
    assert set(SENADO_KINDS) == set(NormKind)


def test_planalto_candidates_follow_folder_conventions() -> None:
    assert ato_folder(2005) == "_ato2004-2006"
    assert ato_folder(2024) == "_ato2023-2026"
    assert dotted("10973") == "10.973"
    law_2004 = candidate_urls(make_norm("10973", NormKind.LEI, 2004))
    assert law_2004[0].endswith("_ato2004-2006/2004/lei/l10.973compilado.htm")
    decree = candidate_urls(make_norm("11930", NormKind.DECRETO, 2024))
    assert decree == [
        "https://www.planalto.gov.br/ccivil_03/_ato2023-2026/2024/decreto/d11930compilado.htm",
        "https://www.planalto.gov.br/ccivil_03/_ato2023-2026/2024/decreto/d11930.htm",
    ]
    assert text_version_of(decree[0]) is TextVersion.COMPILED
    assert text_version_of(decree[1]) is TextVersion.ORIGINAL
