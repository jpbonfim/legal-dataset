"""Parser for the consolidated texts served by normas.leg.br (compiled by the Câmara).

These texts already contain only the provisions in force. Editorial notes are italic
hyperlinks (``<a><span class="Hyperlink">``), sometimes split over several links or even
paragraphs, so they are removed structurally before the text-level pass.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from legis_dataset.models import Annotation, ParsedDocument
from legis_dataset.parsing.annotations import make_annotation
from legis_dataset.parsing.base import ParseError
from legis_dataset.parsing.html_utils import decode_html, iter_blocks, parse, text_of
from legis_dataset.parsing.lines import blocks_to_text
from legis_dataset.processing.normalize import normalize_line

_WHITESPACE = re.compile(r"\s+")


class NormasLegParser:
    source_name = "normas_leg"

    def parse(self, raw: bytes, *, norm_id: str, fallback_title: str) -> ParsedDocument:
        tree = parse(decode_html(raw, "utf-8"))
        annotations = _remove_annotation_links(tree)
        for header in tree.css("p.Header"):
            header.decompose()
        title_node = tree.css_first("h1")
        title = (normalize_line(text_of(title_node)) if title_node else "") or fallback_title
        text = blocks_to_text(iter_blocks(tree), annotations, title)
        if not text:
            raise ParseError("no text content")
        return ParsedDocument(norm_id=norm_id, title=title, text=text, annotations=annotations)


def _remove_annotation_links(tree: LexborHTMLParser) -> list[Annotation]:
    """Remove editorial-note links, merging pieces of one note split across several links."""
    annotations: list[Annotation] = []
    pending: list[str] = []
    for link in tree.css("a"):
        if link.css_first("span.Hyperlink") is None:
            continue
        piece = _WHITESPACE.sub(" ", text_of(link)).strip()
        link.decompose()
        if not piece:
            continue
        pending.append(piece)
        joined = " ".join(pending)
        if joined.count("(") <= joined.count(")"):
            annotations.append(make_annotation(joined))
            pending = []
    if pending:
        joined = " ".join(pending)
        annotations.append(make_annotation(joined))
    return annotations
