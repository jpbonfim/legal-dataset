"""Low-level HTML helpers: decoding, node removal and block extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser, LexborNode

_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)
_NON_SPACE = re.compile(r"\S")
_WHITESPACE = re.compile(r"\s+")
# Stands in for <br> so that source-code line wrapping can be collapsed while real line
# breaks survive. U+2028 (LINE SEPARATOR) does not occur in the pages.
LINE_BREAK = " "
BLOCK_SELECTOR = "p, h1, h2, h3, h4, h5, h6, li, tr"


def decode_html(raw: bytes, default_encoding: str) -> str:
    """Decode using the declared charset; fall back to ``default_encoding``."""
    declared = _META_CHARSET.search(raw[:4096])
    candidates = [declared.group(1).decode("ascii").lower()] if declared else []
    candidates += [default_encoding, "utf-8", "cp1252"]
    for encoding in dict.fromkeys(candidates):
        if encoding in {"iso-8859-1", "latin-1", "latin1"}:
            # Pages that declare Latin-1 are in practice cp1252 (curly quotes, en dashes).
            encoding = "cp1252"
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("cp1252", errors="replace")


def parse(html: str) -> LexborHTMLParser:
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, noscript, img, head, iframe, form"):
        node.decompose()
    for br in tree.css("br"):
        br.replace_with(LINE_BREAK)
    return tree


def text_of(node: LexborNode) -> str:
    """Visible text with whitespace collapsed; ``<br>`` becomes ``\\n``."""
    raw = node.text(deep=True, separator="", strip=False)
    lines = (_WHITESPACE.sub(" ", part).strip() for part in raw.split(LINE_BREAK))
    return "\n".join(lines)


def visible_length(text: str) -> int:
    return len(_NON_SPACE.findall(text))


@dataclass
class Block:
    """One paragraph-like unit of the page, in document order."""

    text: str
    link_only: bool  # every visible character comes from <a> elements (navigation links)
    in_table: bool


def iter_blocks(tree: LexborHTMLParser) -> list[Block]:
    """Paragraphs, headings, list items and table rows, without double counting nesting."""
    body = tree.body
    if body is None:
        return []
    blocks: list[Block] = []
    for node in body.css(BLOCK_SELECTOR):
        if node.tag != "tr" and _has_block_ancestor(node):
            continue
        if node.tag == "tr":
            if _has_ancestor(node, "tr"):
                continue  # nested tables are rendered with their outer row
            # Cells made only of links are navigation ("Vigência", "Regulamento").
            cells = [
                text_of(cell).replace("\n", " ").strip()
                for cell in node.css("td, th")
                if not _is_link_only(cell)
            ]
            text = " | ".join(cell for cell in cells if cell)
        else:
            text = text_of(node)
        if not text.strip():
            continue
        blocks.append(
            Block(
                text=text,
                link_only=node.tag != "tr" and _is_link_only(node),
                in_table=node.tag == "tr" or _has_ancestor(node, "table"),
            )
        )
    covered = sum(visible_length(block.text) for block in blocks)
    if covered < 0.5 * visible_length(text_of(body)):
        # Old pages put text directly in <font>/<div> with <br>; fall back to raw lines.
        return [
            Block(text=line, link_only=False, in_table=False)
            for line in text_of(body).split("\n")
            if line.strip()
        ]
    return blocks


def _is_link_only(node: LexborNode) -> bool:
    text_length = visible_length(text_of(node))
    link_length = sum(visible_length(text_of(link)) for link in node.css("a"))
    return text_length > 0 and link_length >= text_length


def _has_ancestor(node: LexborNode, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _has_block_ancestor(node: LexborNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in {"p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            return True
        parent = parent.parent
    return False
