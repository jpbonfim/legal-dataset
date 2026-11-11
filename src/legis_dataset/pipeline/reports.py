"""Reports written to ``data/reports`` while the pipeline runs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import orjson

from legis_dataset.state.repository import StateRepository


def write_source_map_report(repository: StateRepository, reports_dir: Path) -> None:
    """Catalog coverage and, per kind of norm, legal status and source of the texts."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "catalog_partitions": [dict(row) for row in repository.partition_summary()],
        "norms": [dict(row) for row in repository.source_map_summary()],
    }
    target = reports_dir / "source_map.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    tmp.replace(target)
