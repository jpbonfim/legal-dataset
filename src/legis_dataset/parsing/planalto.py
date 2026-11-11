"""Parser for Planalto pages (www.planalto.gov.br/ccivil_03).

Planalto pages show revoked or superseded wording struck through (``<strike>``, ``<s>``,
``<del>`` or ``text-decoration: line-through``). Struck text is removed so that only the
law in force remains — except ``<s>º</s>``, which the Planalto also uses for ordinals.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

from legis_dataset.models import Annotation, ParsedDocument
from legis_dataset.parsing.annotations import is_annotation, make_annotation
from legis_dataset.parsing.base import ParseError
from legis_dataset.parsing.html_utils import decode_html, iter_blocks, parse, text_of
from legis_dataset.parsing.lines import blocks_to_text
from legis_dataset.processing.normalize import normalize_line

_LINE_THROUGH = re.compile(r"line-through", re.IGNORECASE)
_ORDINAL_ONLY = re.compile(r"^\s*[ºª°oa]?\s*$")
_TITLE = re.compile(
    r"^(?:CONSTITUI[ÇC][ÃA]O|EMENDA CONSTITUCIONAL|LEI|DECRETO|MEDIDA PROVIS[ÓO]RIA)\b.*\d",
    re.IGNORECASE,
)
_HEADER_MARKERS = ("Presidência da República", "Casa Civil")


class PlanaltoParser:
    source_name = "planalto"

    def parse(self, raw: bytes, *, norm_id: str, fallback_title: str) -> ParsedDocument:
        tree = parse(decode_html(raw, "cp1252"))
        remove_struck_text(tree)
        _remove_header_table(tree)
        annotations = _remove_annotation_links(tree)
        blocks = iter_blocks(tree)
        title = next(
            (
                normalize_line(block.text)
                for block in blocks[:15]
                if _TITLE.match(normalize_line(block.text))
            ),
            fallback_title,
        )
        text = blocks_to_text(blocks, annotations, title)
        if not text:
            raise ParseError("no text content")
        return ParsedDocument(norm_id=norm_id, title=title, text=text, annotations=annotations)


def _is_struck(node: LexborNode) -> bool:
    if node.tag in {"strike", "s", "del"}:
        return True
    style = node.attributes.get("style") or ""
    return bool(_LINE_THROUGH.search(style))


def remove_struck_text(tree: LexborHTMLParser) -> None:
    body = tree.body
    if body is None:
        return
    # Collect first: mutating while traversing would skip nodes.
    struck = [node for node in body.traverse() if _is_struck(node)]
    for node in struck:
        if node.parent is None:
            continue  # already removed together with a struck ancestor
        if node.tag == "s" and _ORDINAL_ONLY.match(text_of(node)):
            node.unwrap()  # "§ 1<s>º</s>" is an ordinal, not a revocation
        else:
            node.decompose()


def _remove_header_table(tree: LexborHTMLParser) -> None:
    for table in tree.css("table")[:3]:
        content = text_of(table)
        if all(marker in content for marker in _HEADER_MARKERS) and len(content) < 500:
            table.decompose()
            return


def _remove_annotation_links(tree: LexborHTMLParser) -> list[Annotation]:
    """Links that are whole editorial notes, e.g. <a>(Redação dada pela Lei nº ...)</a>.

    Links inside the body that merely cite another norm are kept as plain text.
    """
    annotations: list[Annotation] = []
    for link in tree.css("a"):
        text = normalize_line(text_of(link))
        if text and is_annotation(text):
            annotations.append(make_annotation(text))
            link.decompose()
    return annotations
