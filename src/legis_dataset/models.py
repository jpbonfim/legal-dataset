"""Source-independent domain models shared by every pipeline stage."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum


class NormKind(StrEnum):
    """Kinds of norms. Values follow the LexML URN vocabulary."""

    CONSTITUICAO = "constituicao"
    EMENDA_CONSTITUCIONAL = "emenda.constitucional"
    LEI_COMPLEMENTAR = "lei.complementar"
    LEI = "lei"
    DECRETO_LEI = "decreto.lei"
    MEDIDA_PROVISORIA = "medida.provisoria"
    DECRETO = "decreto"


class Sphere(StrEnum):
    FEDERAL = "federal"
    ESTADUAL = "estadual"
    MUNICIPAL = "municipal"


class LegalForce(StrEnum):
    IN_FORCE = "in_force"
    NOT_IN_FORCE = "not_in_force"
    UNKNOWN = "unknown"


class TextVersion(StrEnum):
    """How current a downloaded text is."""

    CURRENT = "current"  # consolidated text containing only provisions in force
    COMPILED = "compiled"  # consolidated text that still shows struck-through provisions
    ORIGINAL = "original"  # text as originally published


@dataclass(frozen=True)
class Jurisdiction:
    """Where a norm comes from. ``uf``/``municipality`` use LexML locality names."""

    sphere: Sphere
    uf: str | None = None
    municipality: str | None = None

    @property
    def urn_authority(self) -> str:
        """LexML locality + authority segment, e.g. ``br:federal`` or ``br;sao.paulo:estadual``."""
        locality = ";".join(part for part in ("br", self.uf, self.municipality) if part)
        return f"{locality}:{self.sphere.value}"


FEDERAL = Jurisdiction(Sphere.FEDERAL)


def build_urn(jurisdiction: Jurisdiction, kind: NormKind, signed_on: dt.date, number: str) -> str:
    """Build a LexML URN such as ``urn:lex:br:federal:lei:1990-09-11;8078``."""
    return f"urn:lex:{jurisdiction.urn_authority}:{kind.value}:{signed_on.isoformat()};{number}"


def safe_file_stem(norm_id: str) -> str:
    """Filesystem-safe, collision-free name for a norm identifier."""
    readable = re.sub(r"[^A-Za-z0-9.-]+", "_", norm_id.removeprefix("urn:lex:"))[:120]
    digest = hashlib.sha1(norm_id.encode()).hexdigest()[:10]
    return f"{readable}__{digest}"


@dataclass
class Norm:
    """A norm as listed by a catalog source, before its text is located."""

    norm_id: str  # LexML URN; canonical identifier across sources
    kind: NormKind
    number: str
    signed_on: dt.date
    jurisdiction: Jurisdiction
    title: str
    summary: str | None
    catalog_source: str
    catalog_ref: str  # identifier of the norm inside the catalog source
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TextSource:
    """Where the text of a norm can be downloaded."""

    source_name: str
    url: str
    text_version: TextVersion


@dataclass(frozen=True)
class RawPage:
    """A downloaded text page with the validators needed for conditional re-downloads."""

    content: bytes
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class Location:
    """What a locator learned about a norm. Any field may be unknown."""

    legal_force: LegalForce = LegalForce.UNKNOWN
    text_source: TextSource | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    resolved_urn: str | None = None  # set when the source knows the norm by another URN
    # The text page itself, when finding the source already required downloading it.
    page: RawPage | None = None


@dataclass
class Annotation:
    """Editorial note removed from the text, e.g. "(Redação dada pela Lei nº ...)"."""

    kind: str
    text: str
    referenced_norm: str | None = None  # e.g. "Lei nº 9.430, de 1996"


@dataclass
class ParsedDocument:
    norm_id: str
    title: str
    text: str
    annotations: list[Annotation] = field(default_factory=list)
