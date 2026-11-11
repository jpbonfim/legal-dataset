"""Editorial annotations added by consolidators, e.g. "(Redação dada pela Lei nº 9.430, de 1996)".

They are removed from the training text (they repeat thousands of times and are not part
of the norm) and kept as structured metadata instead.
"""

from __future__ import annotations

import re

from legis_dataset.models import Annotation

# Optional subject ("Parágrafo", "Inciso", "“Caput” do artigo", ...) followed by the action.
_SUBJECT = (
    r"(?:primitiv[oa]\s+)?"
    r"(?:[“\"]?caput[”\"]?|artigo|art\.|par[áa]grafo(?:\s+[úu]nico)?|§+|incisos?|al[íi]neas?|"
    r"itens?|subitens?|express[ãa]o|texto|anexos?|cap[íi]tulo|se[çc][ãa]o|subse[çc][ãa]o|"
    r"t[íi]tulo|livro|parte|dispositivos?|norma|lei|denomina[çc][ãa]o)"
    r"[^()]{0,80}?\s+"
)
_ACTIONS: dict[str, str] = {
    r"(?:com\s+)?(?:nova\s+)?reda[çc][ãa]o": "redacao",
    r"inclu[íi]d[oa]s?": "inclusao",
    r"acrescid[oa]s?": "inclusao",
    r"acrescentad[oa]s?": "inclusao",
    r"revogad[oa]s?": "revogacao",
    r"renumerad[oa]s?": "renumeracao",
    r"transformad[oa]s?": "renumeracao",
    r"suprimid[oa]s?": "revogacao",
    r"vetad[oa]s?": "veto",
    r"vide": "vide",
    r"vig[êe]ncia": "vigencia",
    r"produ[çc][ãa]o\s+de\s+efeitos?": "vigencia",
    r"em\s+vigor": "vigencia",
    r"regulamento": "regulamento",
    r"regulamentad[oa]": "regulamento",
    r"revigorad[oa]s?": "revigoracao",
    r"declarad[oa]s?": "decisao_judicial",
    r"execu[çc][ãa]o\s+suspensa": "decisao_judicial",
    r"convers[ãa]o": "conversao",
    r"convertid[oa]": "conversao",
    r"promulga[çc][ãa]o": "promulgacao",
    r"mantid[oa]s?": "veto",
    r"prejudicad[oa]s?": "outro",
}
_ACTION = "|".join(f"(?P<a{index}>{pattern})" for index, pattern in enumerate(_ACTIONS))
_KINDS = list(_ACTIONS.values())

# A parenthesis that starts with an annotation keyword; one level of nested parentheses is
# allowed, e.g. "(Redação dada pela Lei nº 13.874, de 2019) (Vide ADI 1)".
ANNOTATION_PATTERN = re.compile(
    rf"\(\s*(?:{_SUBJECT})?(?:{_ACTION})\b[^()]*(?:\([^()]*\)[^()]*)*\)",
    re.IGNORECASE,
)
# Annotation opening that is not closed on the same line (split across links/paragraphs).
OPEN_ANNOTATION_PATTERN = re.compile(rf"\(\s*(?:{_SUBJECT})?(?:{_ACTION})\b[^()]*$", re.IGNORECASE)


_REFERENCED_NORM = re.compile(
    r"(?:Emenda Constitucional|Lei Complementar|Decreto-Lei|Decreto Legislativo|Decreto|"
    r"Medida Provis[óo]ria|Lei|Resolu[çc][ãa]o|ADI|ADC|ADPF)\s*(?:n[º°o.]\s*)?[\d.]+(?:-\d+)?"
    r"(?:,?\s*de\s*[\d/.]+(?:\s*de\s*\d{4})?)?",
    re.IGNORECASE,
)


def referenced_norm(annotation_text: str) -> str | None:
    match = _REFERENCED_NORM.search(annotation_text)
    return match.group(0).strip() if match else None


def make_annotation(annotation_text: str) -> Annotation:
    return Annotation(
        kind=classify(annotation_text),
        text=annotation_text,
        referenced_norm=referenced_norm(annotation_text),
    )


def classify(annotation_text: str) -> str:
    match = ANNOTATION_PATTERN.search(annotation_text) or OPEN_ANNOTATION_PATTERN.search(
        annotation_text
    )
    if match is None:
        return "outro"
    for name, value in match.groupdict().items():
        if value is not None and name.startswith("a"):
            return _KINDS[int(name[1:])]
    return "outro"


def is_annotation(text: str) -> bool:
    """True if ``text`` is (the start of) an annotation and nothing else."""
    stripped = text.strip()
    if not stripped.startswith("("):
        return False
    full = ANNOTATION_PATTERN.fullmatch(stripped)
    return full is not None or OPEN_ANNOTATION_PATTERN.fullmatch(stripped) is not None


def extract_annotations(line: str) -> tuple[str, list[Annotation]]:
    """Remove annotations from ``line`` and return them separately."""
    found: list[Annotation] = []

    def collect(match: re.Match[str]) -> str:
        found.append(make_annotation(match.group(0)))
        return " "

    cleaned = ANNOTATION_PATTERN.sub(collect, line)
    return cleaned, found
