"""The document record passed between parse, dedup and export (one JSON object per line)."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import orjson

from legis_dataset.models import ParsedDocument
from legis_dataset.state.repository import WorkItem


def build_record(item: WorkItem, document: ParsedDocument) -> dict[str, object]:
    norm = item.norm
    assert item.text_source is not None
    return {
        "id": norm.norm_id,
        "title": document.title,
        "ementa": norm.summary,
        "tipo": norm.kind.value,
        "numero": norm.number,
        "data_assinatura": norm.signed_on.isoformat(),
        "esfera": norm.jurisdiction.sphere.value,
        "uf": norm.jurisdiction.uf,
        "municipio": norm.jurisdiction.municipality,
        "subset": item.subset,
        "fonte": item.text_source.source_name,
        "url": item.text_source.url,
        "versao_texto": item.text_source.text_version.value,
        "text": document.text,
        "annotations": [
            {"tipo": a.kind, "texto": a.text, "norma_ref": a.referenced_norm}
            for a in document.annotations
        ],
        "data_coleta": (item.fetched_at or dt.datetime.now(dt.UTC).isoformat())[:10],
    }


class JsonlPartWriter:
    """Appends records to one new part file per subset and run.

    Each line is flushed and fsynced before the caller marks the item as done, so a crash
    can at worst leave a record whose status is still pending (it is re-parsed and the
    later copy wins when parts are read back).
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
        self._files: dict[str, IO[bytes]] = {}

    def write(self, subset: str, record: dict[str, object]) -> None:
        file = self._files.get(subset)
        if file is None:
            path = self._directory / subset / f"part-{self._run_id}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            file = path.open("ab")
            self._files[subset] = file
        file.write(orjson.dumps(record) + b"\n")
        file.flush()
        os.fsync(file.fileno())

    def close(self) -> None:
        for file in self._files.values():
            file.close()
        self._files.clear()

    def __enter__(self) -> JsonlPartWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_latest_records(directory: Path) -> Iterator[dict[str, object]]:
    """Read every part of every subset; for repeated ids the most recent part wins."""
    latest: dict[str, dict[str, object]] = {}
    for path in sorted(directory.glob("*/part-*.jsonl")):
        with path.open("rb") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    record = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue  # a line cut short by a crash
                latest[str(record["id"])] = record
    yield from latest.values()


def write_jsonl_atomically(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as file:
        for record in records:
            file.write(orjson.dumps(record) + b"\n")
        file.flush()
        os.fsync(file.fileno())
    tmp.replace(path)


def read_jsonl(path: Path) -> Iterator[dict[str, object]]:
    with path.open("rb") as file:
        for line in file:
            if line.strip():
                yield orjson.loads(line)
