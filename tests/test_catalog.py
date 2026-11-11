"""Catalog discovery must never take a suspicious answer as "this year has no norms"."""

from __future__ import annotations

import httpx
import pytest
import respx

from legis_dataset.config import Settings
from legis_dataset.http.errors import PermanentError
from legis_dataset.models import NormKind
from legis_dataset.pipeline.context import PipelineContext, RunOptions
from legis_dataset.pipeline.orchestrator import StageName, run_stages
from legis_dataset.sources.senado import listed_items
from legis_dataset.state.repository import StateRepository

SENADO_LIST = "https://legis.senado.leg.br/dadosabertos/legislacao/lista"
LAW = {
    "id": "550016",
    "tipo": "LEI-n",
    "numero": "8140",
    "norma": "LEI-8140-1990-12-28",
    "normaNome": "Lei nº 8.140 de 28/12/1990",
    "dataassinatura": "28/12/1990",
}


def _listing(*items: dict[str, str]) -> dict[str, object]:
    if not items:  # how the API answers a year without norms
        return {"ListaDocumento": {"Metadados": {}, "documentos": ""}}
    return {"ListaDocumento": {"Metadados": {}, "documentos": {"documento": list(items)}}}


def test_listed_items_accepts_listings_and_rejects_anything_else() -> None:
    assert listed_items(_listing(LAW), "u") == [LAW]
    assert listed_items(_listing(), "u") == []
    assert listed_items({"ListaDocumento": {"Metadados": {}}}, "u") == []
    with pytest.raises(PermanentError):
        listed_items({"erro": "Serviço indisponível"}, "u")
    with pytest.raises(PermanentError):
        listed_items([], "u")
    with pytest.raises(PermanentError):
        listed_items({"ListaDocumento": {"documentos": "erro"}}, "u")


def _context(settings: Settings, repository: StateRepository) -> PipelineContext:
    settings.catalog.first_year = settings.catalog.last_year = 1990
    settings.sources.locators = []
    for policy in settings.http.hosts.values():
        policy.rate_per_second = 1000
    return PipelineContext(settings, repository, RunOptions(kinds=[NormKind.LEI]))


async def test_empty_partition_is_checked_again_in_the_same_run(
    settings: Settings, repository: StateRepository
) -> None:
    context = _context(settings, repository)
    with respx.mock as router:
        route = router.get(SENADO_LIST).mock(
            side_effect=[
                httpx.Response(200, json=_listing()),
                httpx.Response(200, json=_listing(LAW)),
            ]
        )
        assert await run_stages(context, [StageName.MAP])
    assert route.call_count == 2
    assert repository.norm_count() == 1
    assert repository.confirmed_partitions("senado") == {"lei/1990"}


async def test_partition_still_empty_is_queried_on_the_next_run(
    settings: Settings, repository: StateRepository
) -> None:
    context = _context(settings, repository)
    with respx.mock as router:
        router.get(SENADO_LIST).mock(return_value=httpx.Response(200, json=_listing()))
        assert await run_stages(context, [StageName.MAP])
    assert repository.confirmed_partitions("senado") == set()

    with respx.mock as router:
        route = router.get(SENADO_LIST).mock(return_value=httpx.Response(200, json=_listing(LAW)))
        assert await run_stages(context, [StageName.MAP])
    assert route.call_count == 1
    assert repository.norm_count() == 1
