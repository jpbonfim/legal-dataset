"""Planalto (www.planalto.gov.br/ccivil_03) — fallback source of texts.

The Planalto has no API or stable index, so the URL of a norm is guessed from known
folder conventions and confirmed with a HEAD request. Consolidated pages
(``...compilado.htm``) are preferred; they still show revoked provisions struck through,
which the Planalto parser removes.
"""

from __future__ import annotations

from collections.abc import Callable

from legis_dataset.config import Settings
from legis_dataset.http.client import HttpClient
from legis_dataset.models import Location, Norm, NormKind, Sphere, TextSource, TextVersion
from legis_dataset.sources.base import Capability, register_locator

HOST = "www.planalto.gov.br"
BASE_URL = f"https://{HOST}/ccivil_03"
SOURCE_NAME = "planalto"
COMPILED_SUFFIXES = ("compilado", "compilada")


def ato_folder(year: int) -> str:
    """Folder used for acts since 2004, grouped by presidential term."""
    if year <= 2006:
        return "_ato2004-2006"
    start = 2007 + 4 * ((year - 2007) // 4)
    return f"_ato{start}-{start + 3}"


def dotted(number: str) -> str:
    """``10973`` -> ``10.973`` (used by some 2004–2006 law pages)."""
    if not number.isdigit():
        return number
    return f"{int(number):,}".replace(",", ".")


def _with_compiled(stem: str) -> list[str]:
    """Consolidated variants first, then the plain page."""
    return [f"{stem}compilado.htm", f"{stem}.htm"]


def _lei(number: str, year: int) -> list[str]:
    if year >= 2004:
        folder = f"{ato_folder(year)}/{year}/lei"
        stems = (
            [f"{folder}/l{dotted(number)}", f"{folder}/l{number}"]
            if year <= 2006
            else [f"{folder}/l{number}"]
        )
        return [path for stem in stems for path in _with_compiled(stem)]
    if year >= 2002:
        stem = f"leis/{year}/l{number}"
        return [f"{stem}compilada.htm", *_with_compiled(stem)]
    if year == 2001:
        return _with_compiled(f"leis/leis_2001/l{number}")
    return [f"leis/l{number}compilada.htm", *_with_compiled(f"leis/l{number}")]


def _decreto(number: str, year: int) -> list[str]:
    if year >= 2004:
        return _with_compiled(f"{ato_folder(year)}/{year}/decreto/d{number}")
    if year >= 2000:
        return _with_compiled(f"decreto/{year}/d{number}")
    paths = _with_compiled(f"decreto/d{number}")
    for first, last in ((1930, 1949), (1950, 1969), (1990, 1994)):
        if first <= year <= last:
            paths.append(f"decreto/{first}-{last}/d{number}.htm")
    return paths


def _medida_provisoria(number: str, year: int) -> list[str]:
    if year >= 2002:
        return [
            *_with_compiled(f"{ato_folder(year)}/{year}/mpv/mpv{number}"),
            f"{ato_folder(year)}/{year}/mpv/{number}.htm",
        ]
    if year == 2001:
        return [f"mpv/antigas_2001/{number}.htm", f"mpv/{number}.htm"]
    return [f"mpv/antigas/{number}.htm", f"mpv/{number}.htm"]


def _decreto_lei(number: str, year: int) -> list[str]:
    return _with_compiled(f"decreto-lei/del{number}")


CandidateBuilder = Callable[[str, int], list[str]]

CANDIDATES: dict[NormKind, CandidateBuilder] = {
    NormKind.CONSTITUICAO: lambda number, year: ["constituicao/constituicao.htm"],
    NormKind.EMENDA_CONSTITUCIONAL: lambda number, year: [
        f"constituicao/emendas/emc/emc{number}.htm"
    ],
    NormKind.LEI_COMPLEMENTAR: lambda number, year: _with_compiled(f"leis/lcp/lcp{number}"),
    NormKind.LEI: _lei,
    NormKind.DECRETO_LEI: _decreto_lei,
    NormKind.MEDIDA_PROVISORIA: _medida_provisoria,
    NormKind.DECRETO: _decreto,
}


def candidate_urls(norm: Norm) -> list[str]:
    builder = CANDIDATES.get(norm.kind)
    if builder is None:
        return []
    unique = dict.fromkeys(builder(norm.number, norm.signed_on.year))
    return [f"{BASE_URL}/{path}" for path in unique]


def text_version_of(url: str) -> TextVersion:
    stem = url.rsplit("/", 1)[-1].removesuffix(".htm")
    if stem.endswith(COMPILED_SUFFIXES) or stem == "constituicao":
        return TextVersion.COMPILED
    return TextVersion.ORIGINAL


class PlanaltoLocator:
    name = SOURCE_NAME
    hosts = (HOST,)
    provides = Capability.TEXT

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def supports(self, norm: Norm) -> bool:
        return norm.jurisdiction.sphere is Sphere.FEDERAL and norm.kind in CANDIDATES

    async def locate(self, norm: Norm) -> Location:
        tried: list[str] = []
        for url in candidate_urls(norm):
            tried.append(url)
            if await self._client.exists(url):
                return Location(
                    text_source=TextSource(SOURCE_NAME, url, text_version_of(url)),
                    metadata={"planalto_candidates_tried": len(tried)},
                )
        return Location(metadata={"planalto_candidates_tried": len(tried)})


@register_locator(SOURCE_NAME)
def _build(client: HttpClient, settings: Settings) -> PlanaltoLocator:
    return PlanaltoLocator(client)
