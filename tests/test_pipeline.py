"""End-to-end run of every stage against mocked official sources (no network)."""

from __future__ import annotations

import datetime as dt
import re

import httpx
import orjson
import pyarrow.parquet as pq
import respx
from conftest import fixture_bytes

from legis_dataset.config import Settings
from legis_dataset.models import NormKind
from legis_dataset.pipeline.context import PipelineContext, RunOptions
from legis_dataset.pipeline.orchestrator import ALL_STAGES, run_stages
from legis_dataset.state.repository import StateRepository
from legis_dataset.stats.dataset_stats import compute_dataset_stats
from legis_dataset.stats.token_counter import EstimatedTokenCounter

SENADO = "https://legis.senado.leg.br/dadosabertos/legislacao"
NORMAS = "https://normas.leg.br/api/public"
PLANALTO = "https://www.planalto.gov.br/ccivil_03"


def _senado_list(*items: dict[str, str]) -> dict[str, object]:
    return {"ListaDocumento": {"documentos": {"documento": list(items)}}}


LAW = {
    "id": "551125",
    "tipo": "LEI-n",
    "numero": "9249",
    "norma": "LEI-9249-1995-12-26",
    "normaNome": "Lei nº 9.249 de 26/12/1995",
    "ementa": "Altera o IR.",
    "dataassinatura": "26/12/1995",
}
REVOKED = {
    "id": "1",
    "tipo": "LEI-n",
    "numero": "3071",
    "norma": "LEI-3071-1995-01-01",
    "normaNome": "Lei nº 3.071",
    "dataassinatura": "01/01/1995",
}
DECREE = {
    "id": "2",
    "tipo": "DEC-n",
    "numero": "11930",
    "norma": "DEC-11930-2024-02-27",
    "normaNome": "Decreto nº 11.930 de 27/02/2024",
    "dataassinatura": "27/02/2024",
}
UNNUMBERED_DECREE = {
    "id": "3",
    "tipo": "DEC-sn",
    "norma": "DEC-2024-01-01-s0",
    "dataassinatura": "01/01/2024",
}


def _mock_sources(router: respx.MockRouter) -> None:
    def senado_list(request: httpx.Request) -> httpx.Response:
        kind, year = request.url.params["tipo"], request.url.params["ano"]
        if kind == "LEI" and year == "1995":
            return httpx.Response(200, json=_senado_list(LAW, REVOKED))
        if kind == "DEC" and year == "2024":
            return httpx.Response(200, json=_senado_list(DECREE, UNNUMBERED_DECREE))
        return httpx.Response(200, json=_senado_list())

    def normas(request: httpx.Request) -> httpx.Response:
        urn = request.url.params["urn"]
        if urn.endswith(";9249"):
            return httpx.Response(200, content=fixture_bytes("normas_leg_lei_9249.json"))
        if urn.endswith(";3071"):
            return httpx.Response(200, content=fixture_bytes("normas_leg_lei_3071_revoked.json"))
        return httpx.Response(200, json={"urn": urn})  # unknown to normas.leg.br

    router.get(f"{SENADO}/lista").mock(side_effect=senado_list)
    router.get(f"{SENADO}/2").mock(return_value=httpx.Response(200, json={}))
    router.get(f"{NORMAS}/normas").mock(side_effect=normas)
    router.get(url__regex=rf"{re.escape(NORMAS)}/binario/.*").mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("normas_leg_lei_9249_current.html"),
            headers={"content-type": "text/html"},
        )
    )
    decree_dir = f"{PLANALTO}/_ato2023-2026/2024/decreto"
    router.head(f"{decree_dir}/d11930compilado.htm").mock(return_value=httpx.Response(404))
    router.head(f"{decree_dir}/d11930.htm").mock(return_value=httpx.Response(200))
    router.get(f"{decree_dir}/d11930.htm").mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("planalto_decreto_11930.htm"),
            headers={"content-type": "text/html", "etag": '"v1"'},
        )
    )


async def test_full_pipeline_produces_parquet_subsets(
    settings: Settings, repository: StateRepository, monkeypatch
) -> None:
    settings.catalog.first_year, settings.catalog.last_year = 1995, 2024
    for policy in settings.http.hosts.values():
        policy.rate_per_second = 1000
    monkeypatch.setattr(
        "legis_dataset.pipeline.export_stage.load_token_counter",
        lambda name: EstimatedTokenCounter("offline test"),
    )
    context = PipelineContext(
        settings, repository, RunOptions(kinds=[NormKind.LEI, NormKind.DECRETO])
    )

    with respx.mock(assert_all_called=False) as router:
        _mock_sources(router)
        assert await run_stages(context, ALL_STAGES)

    final = settings.paths.final_dir
    laws = pq.read_table(final / "leis").to_pylist()
    decrees = pq.read_table(final / "decretos").to_pylist()
    assert [row["id"] for row in laws] == ["urn:lex:br:federal:lei:1995-12-26;9249"]
    assert [row["id"] for row in decrees] == ["urn:lex:br:federal:decreto:2024-02-27;11930"]
    law = laws[0]
    assert law["fonte"] == "normas_leg" and law["versao_texto"] == "current"
    assert law["data_assinatura"] == dt.date(1995, 12, 26)
    assert law["n_tokens"] > 0 and law["annotations"]
    assert decrees[0]["fonte"] == "planalto"

    stats = compute_dataset_stats(final)
    assert stats.total_documents == 2
    assert stats.tokens_are_estimate
    assert stats.total_tokens == sum(row["n_tokens"] for row in laws + decrees)
    saved = orjson.loads((final / "stats.json").read_bytes())
    assert saved["removed"]["map:not_in_force"] == 1

    # A second run finds nothing new to do and keeps the same dataset.
    with respx.mock(assert_all_called=False) as router:
        _mock_sources(router)
        assert await run_stages(context, ALL_STAGES)
    assert pq.read_table(final / "leis").num_rows == 1
