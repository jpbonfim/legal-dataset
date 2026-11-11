"""Turns page blocks into the final document text.

Shared by every parser: annotation removal, boilerplate removal, dropping provisions that
are only "(Revogado)"/"(VETADO)", removing the signature block, and spacing between
articles.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from legis_dataset.models import Annotation
from legis_dataset.parsing.annotations import extract_annotations
from legis_dataset.parsing.html_utils import Block
from legis_dataset.processing.normalize import normalize_line

ARTICLE_START = re.compile(r"^(?:Art\.?|Artigo)\s*\d", re.IGNORECASE)
HEADING_START = re.compile(
    r"^(?:PARTE|LIVRO|T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O|ANEXO)\b"
)
ANNEX_START = re.compile(r"^ANEXO\b", re.IGNORECASE)

# Label of a provision with nothing else left after annotations were removed.
_LABEL = (
    r"(?:Art\.?\s*\d+[º°o]?(?:-[A-Z]+)?\.?"
    r"|§+\s*\d+[º°o]?(?:-[A-Z]+)?\.?"
    r"|Par[áa]grafo\s+[úu]nico\.?"
    r"|[IVXLCDM]+(?:-[A-Z]+)?\s*[-–—.]?"
    r"|[a-z](?:-[A-Z]+)?\)"
    r"|\d+(?:\.\d+)*\.?\s*[-–—)]?)"
)
BARE_LABEL = re.compile(rf"^{_LABEL}\s*[-–—:.;]*$")
VETOED = re.compile(rf"^(?:{_LABEL}\s*)?[-–—]?\s*\(?\s*VETAD[OA]S?\s*\)?\s*[.;]?$", re.IGNORECASE)
REVOKED_PLACEHOLDER = re.compile(
    rf"^(?:{_LABEL}\s*)?[-–—]?\s*\(?\s*(?:REVOGAD[OA]S?|SUPRIMID[OA]S?)\s*\)?\s*[.;]?$",
    re.IGNORECASE,
)
CLOSING_FORMULA = re.compile(r"\bda\s+Independ[êe]ncia\b.*\bda\s+Rep[úu]blica\b", re.IGNORECASE)
BOILERPLATE = re.compile(
    r"^(?:\*+|Presid[êe]ncia da Rep[úu]blica|Casa Civil|"
    r"Subchefia para Assuntos Jur[íi]dicos|Secretaria Especial para Assuntos Jur[íi]dicos|"
    r"Secretaria-Geral|C[ÂA]MARA DOS DEPUTADOS|Centro de Documenta[çc][ãa]o e Informa[çc][ãa]o|"
    r"Este texto n[ãa]o substitui .*|Texto compilado|Mensagem de veto|"
    r"Coordena[çc][ãa]o de Estudos Legislativos.*)$",
    re.IGNORECASE,
)


def blocks_to_text(blocks: Iterable[Block], annotations: list[Annotation], title: str) -> str:
    """Build the document text, starting with ``title``.

    Annotations found in the text are appended to ``annotations``.
    """
    lines = [text for text, _ in _drop_navigation(_clean_lines(blocks, annotations))]
    lines = _drop_signatures(lines)
    if not lines:
        return ""
    if title and _comparable(lines[0]) != _comparable(title):
        lines.insert(0, title)
    return _layout(lines)


def _comparable(line: str) -> str:
    return re.sub(r"\W+", "", line).casefold()


def _clean_lines(blocks: Iterable[Block], annotations: list[Annotation]) -> list[tuple[str, bool]]:
    cleaned: list[tuple[str, bool]] = []
    for block in blocks:
        for raw_line in block.text.split("\n"):
            line, found = extract_annotations(normalize_line(raw_line))
            annotations.extend(found)
            line = normalize_line(line)
            if not line or BOILERPLATE.match(line):
                continue
            if BARE_LABEL.match(line) or VETOED.match(line) or REVOKED_PLACEHOLDER.match(line):
                continue
            cleaned.append((line, block.link_only))
    return cleaned


def _drop_navigation(lines: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Link-only lines before the first article are navigation ("Vigência", "Regulamento")."""
    first_article = next(
        (index for index, (text, _) in enumerate(lines) if ARTICLE_START.match(text)), None
    )
    if first_article is None:
        return lines
    head = [(text, link_only) for text, link_only in lines[:first_article] if not link_only]
    return head + lines[first_article:]


def _drop_signatures(lines: list[str]) -> list[str]:
    """Remove the closing formula and the signatures that follow (annexes are kept)."""
    result: list[str] = []
    skipping = False
    for line in lines:
        if CLOSING_FORMULA.search(line):
            skipping = True
            continue
        if skipping and ANNEX_START.match(line):
            skipping = False
        if not skipping:
            result.append(line)
    return result


def _layout(lines: list[str]) -> str:
    """One provision per line; a blank line before each article and heading."""
    output: list[str] = []
    for line in lines:
        if output and (ARTICLE_START.match(line) or HEADING_START.match(line)):
            output.append("")
        output.append(line)
    return "\n".join(output).strip()
