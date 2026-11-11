"""Parser contract and lookup by text source name."""

from __future__ import annotations

from typing import Protocol

from legis_dataset.models import ParsedDocument


class ParseError(Exception):
    """The page does not contain a usable norm text."""


class Parser(Protocol):
    source_name: str

    def parse(self, raw: bytes, *, norm_id: str, fallback_title: str) -> ParsedDocument: ...


def get_parser(source_name: str) -> Parser:
    from legis_dataset.parsing.normas_leg import NormasLegParser
    from legis_dataset.parsing.planalto import PlanaltoParser

    parsers: dict[str, Parser] = {
        NormasLegParser.source_name: NormasLegParser(),
        PlanaltoParser.source_name: PlanaltoParser(),
    }
    try:
        return parsers[source_name]
    except KeyError:
        raise ValueError(f"no parser for text source {source_name!r}") from None
