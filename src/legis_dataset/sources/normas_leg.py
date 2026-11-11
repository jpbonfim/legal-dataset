"""normas.leg.br (Congresso Nacional) — legal status and consolidated text in force.

The public API returns schema.org JSON-LD. ``legislationLegalForce == "NotInForce"`` marks
revoked norms, and ``encoding`` entries with ``version == "Current"`` point to a
consolidated text that already contains only the provisions in force (CC BY 4.0).
"""

from __future__ import annotations

from legis_dataset.config import Settings
from legis_dataset.http.client import HttpClient
from legis_dataset.models import LegalForce, Location, Norm, Sphere, TextSource, TextVersion
from legis_dataset.sources.base import Capability, register_locator

HOST = "normas.leg.br"
NORM_URL = f"https://{HOST}/api/public/normas"
SOURCE_NAME = "normas_leg"
# Text URLs inside the JSON use a non-public path that answers 404; the public one works.
_PRIVATE_BINARY_PATH = "/api/binario/"
_PUBLIC_BINARY_PATH = "/api/public/binario/"
_METADATA_FIELDS = ("alternateName", "keywords", "legislationDate", "datePublished", "headline")


def _encodings(payload: dict[str, object]) -> list[dict[str, object]]:
    value = payload.get("encoding")
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def select_current_text(payload: dict[str, object]) -> dict[str, object] | None:
    """The Portuguese HTML encoding of the text in force, if the API has one."""
    for encoding in _encodings(payload):
        if encoding.get("version") != "Current":
            continue
        if encoding.get("inLanguage", "pt") != "pt":
            continue  # e.g. the Constitution translated into English/French
        if "html" not in str(encoding.get("encodingFormat", "html")):
            continue
        if encoding.get("contentUrl"):
            return encoding
    return None


def public_content_url(url: str) -> str:
    return url.replace(_PRIVATE_BINARY_PATH, _PUBLIC_BINARY_PATH, 1)


def payload_to_location(payload: dict[str, object]) -> Location:
    if "@type" not in payload:
        # Unknown norms come back as 200 with just {"urn": ...}.
        return Location()
    not_in_force = payload.get("legislationLegalForce") == "NotInForce"
    metadata: dict[str, object] = {
        key: payload[key] for key in _METADATA_FIELDS if payload.get(key) is not None
    }
    encoding = select_current_text(payload)
    text_source = None
    if encoding is not None:
        text_source = TextSource(
            SOURCE_NAME, public_content_url(str(encoding["contentUrl"])), TextVersion.CURRENT
        )
        metadata["text_name"] = encoding.get("name")
        metadata["text_type"] = encoding.get("additionalType")
    return Location(
        legal_force=LegalForce.NOT_IN_FORCE if not_in_force else LegalForce.IN_FORCE,
        text_source=text_source,
        metadata=metadata,
        resolved_urn=str(payload.get("legislationIdentifier") or "") or None,
    )


class NormasLegLocator:
    name = SOURCE_NAME
    hosts = (HOST,)
    provides = Capability.LEGAL_STATUS | Capability.TEXT

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def supports(self, norm: Norm) -> bool:
        return norm.jurisdiction.sphere is Sphere.FEDERAL

    async def locate(self, norm: Norm) -> Location:
        return await self.locate_urn(norm.norm_id)

    async def locate_urn(self, urn: str) -> Location:
        payload = await self._client.get_json(
            NORM_URL, params={"urn": urn, "tipo_documento": "maior-detalhe"}
        )
        if not isinstance(payload, dict):
            return Location()
        return payload_to_location(payload)


@register_locator(SOURCE_NAME)
def _build(client: HttpClient, settings: Settings) -> NormasLegLocator:
    return NormasLegLocator(client)
