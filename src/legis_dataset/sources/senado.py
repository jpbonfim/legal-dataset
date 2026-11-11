"""Senado Federal open data (legis.senado.leg.br/dadosabertos).

Used as the catalog of federal norms (list by kind and year) and as a fallback provider of
legal status (``situacao == "R"`` means revoked).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from legis_dataset.config import Settings
from legis_dataset.http.client import HttpClient
from legis_dataset.http.errors import PermanentError
from legis_dataset.models import (
    FEDERAL,
    LegalForce,
    Location,
    Norm,
    NormKind,
    Sphere,
    build_urn,
)
from legis_dataset.sources.base import (
    Capability,
    CatalogPartition,
    register_catalog,
    register_locator,
)

HOST = "legis.senado.leg.br"
BASE_URL = f"https://{HOST}/dadosabertos/legislacao"
SOURCE_NAME = "senado"


@dataclass(frozen=True)
class SenadoKind:
    """How a norm kind is queried and filtered in the Senado catalog."""

    code: str  # value of the `tipo` query parameter
    accepted_types: frozenset[str] | None  # item `tipo` values to keep; None keeps all
    first_year: int | None = None
    last_year: int | None = None


SENADO_KINDS: dict[NormKind, SenadoKind] = {
    # Only the Constitution in force ("CON-v"); earlier constitutions are not in force.
    NormKind.CONSTITUICAO: SenadoKind("CON", frozenset({"CON-v"}), 1988, 1988),
    # Amendments to the 1988 Constitution started in 1992.
    NormKind.EMENDA_CONSTITUCIONAL: SenadoKind("EMC", frozenset({"EMC-n"}), 1992),
    NormKind.LEI_COMPLEMENTAR: SenadoKind("LCP", None, 1967),
    # "LEI-n" = numbered laws; unnumbered acts are left out.
    NormKind.LEI: SenadoKind("LEI", frozenset({"LEI-n"})),
    NormKind.DECRETO_LEI: SenadoKind("DEL", None, 1937, 1988),
    NormKind.MEDIDA_PROVISORIA: SenadoKind("MPV", None, 1988),
    # "DEC-n" = numbered decrees; unnumbered ones ("DEC-sn") are mostly one-off acts.
    NormKind.DECRETO: SenadoKind("DEC", frozenset({"DEC-n"})),
}

# "MPV-2229-43-2001-09-06" -> base number 2229, edition 43.
_MP_IDENTIFIER = re.compile(r"^MPV-(?P<base>\d+)(?:-(?P<edition>\d+))?-\d{4}-\d{2}-\d{2}$")


def _as_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def listed_items(payload: object, url: str) -> list[dict[str, object]]:
    """Items of a ``/lista`` answer; raises if the answer does not look like a listing.

    The API answers a year without norms with ``"documentos": ""`` (absent is accepted too).
    Anything else (an error envelope, a different root) must not be mistaken for "no norms".
    """
    listing = payload.get("ListaDocumento") if isinstance(payload, dict) else None
    if not isinstance(listing, dict):
        keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise PermanentError(url, f"unexpected catalog payload: {keys}")
    container = listing.get("documentos")
    if container is None or container == "":
        return []
    if not isinstance(container, dict):
        raise PermanentError(url, f"unexpected 'documentos' value: {container!r:.200}")
    return _as_list(container.get("documento"))


def _parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%d/%m/%Y").date()


def item_to_norm(kind: NormKind, item: dict[str, object]) -> Norm | None:
    """Convert one item of the Senado list into a :class:`Norm` (``None`` if unusable)."""
    signed = str(item.get("dataassinatura") or "")
    if not signed:
        return None
    signed_on = _parse_date(signed)
    identifier = str(item.get("norma") or "")
    extra: dict[str, str] = {"senado_identifier": identifier}

    if kind is NormKind.CONSTITUICAO:
        number = str(signed_on.year)
    elif kind is NormKind.MEDIDA_PROVISORIA:
        match = _MP_IDENTIFIER.match(identifier)
        if not match:
            return None
        base, edition = match["base"], match["edition"]
        number = f"{base}-{edition}" if edition else base
        extra |= {"base_number": base, "edition": edition or "0"}
    else:
        number = str(item.get("numero") or "").replace(".", "").strip().lower()
        if not number:
            return None

    return Norm(
        norm_id=build_urn(FEDERAL, kind, signed_on, number),
        kind=kind,
        number=number,
        signed_on=signed_on,
        jurisdiction=FEDERAL,
        title=str(item.get("normaNome") or identifier),
        summary=str(item["ementa"]) if item.get("ementa") else None,
        catalog_source=SOURCE_NAME,
        catalog_ref=str(item.get("id") or ""),
        extra=extra,
    )


class SenadoCatalog:
    name = SOURCE_NAME
    hosts = (HOST,)

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def partitions(
        self, kinds: list[NormKind], first_year: int, last_year: int
    ) -> list[CatalogPartition]:
        partitions: list[CatalogPartition] = []
        for kind in kinds:
            spec = SENADO_KINDS.get(kind)
            if spec is None:
                continue
            start = max(first_year, spec.first_year or first_year)
            end = min(last_year, spec.last_year or last_year)
            partitions += [CatalogPartition(kind, year) for year in range(end, start - 1, -1)]
        return partitions

    async def list_partition(self, partition: CatalogPartition) -> list[Norm]:
        spec = SENADO_KINDS[partition.kind]
        url = f"{BASE_URL}/lista"
        payload = await self._client.get_json(
            url, params={"tipo": spec.code, "ano": str(partition.year)}
        )
        items = listed_items(payload, f"{url}?tipo={spec.code}&ano={partition.year}")
        norms: list[Norm] = []
        for item in items:
            if spec.accepted_types is not None and item.get("tipo") not in spec.accepted_types:
                continue
            norm = item_to_norm(partition.kind, item)
            if norm is not None:
                norms.append(norm)
        return norms


class SenadoStatusLocator:
    """Legal status from the Senado norm detail. Used when normas.leg.br does not know a norm."""

    name = "senado_status"
    hosts = (HOST,)
    provides = Capability.LEGAL_STATUS

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def supports(self, norm: Norm) -> bool:
        return norm.catalog_source == SOURCE_NAME and norm.jurisdiction.sphere is Sphere.FEDERAL

    async def locate(self, norm: Norm) -> Location:
        payload = await self._client.get_json(f"{BASE_URL}/{norm.catalog_ref}")
        detail = payload.get("DetalheDocumento", {}) if isinstance(payload, dict) else {}
        documents = detail.get("documentos", {}) if isinstance(detail, dict) else {}
        found = _as_list(documents.get("documento") if isinstance(documents, dict) else None)
        if not found:
            return Location()
        identification = found[0].get("identificacao")
        identification = identification if isinstance(identification, dict) else {}
        revoked = identification.get("situacao") == "R"
        url = str(identification.get("urlDocumento") or "")
        resolved_urn = url.split("urn=", 1)[1] if "urn=" in url else None
        return Location(
            legal_force=LegalForce.NOT_IN_FORCE if revoked else LegalForce.IN_FORCE,
            metadata={"senado_situacao": identification.get("situacao")},
            resolved_urn=resolved_urn,
        )


@register_catalog(SOURCE_NAME)
def _build_catalog(client: HttpClient, settings: Settings) -> SenadoCatalog:
    return SenadoCatalog(client)


@register_locator(SenadoStatusLocator.name)
def _build_status_locator(client: HttpClient, settings: Settings) -> SenadoStatusLocator:
    return SenadoStatusLocator(client)
