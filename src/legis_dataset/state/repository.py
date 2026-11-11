"""Read/write access to the pipeline state. All SQL lives here."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

import orjson

from legis_dataset.models import (
    Jurisdiction,
    Location,
    Norm,
    NormKind,
    Sphere,
    TextSource,
    TextVersion,
)


class Stage(StrEnum):
    MAP = "map"
    FETCH = "fetch"
    PARSE = "parse"
    CLEAN = "clean"


class Status(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# Failures with these reasons are retried automatically on the next run; any other failure
# needs an explicit `retry-failed`.
RETRYABLE_REASONS = ("transient", "interrupted")

# The stage whose `done` status makes a norm eligible for the given stage.
PREREQUISITE: dict[Stage, Stage | None] = {
    Stage.MAP: None,
    Stage.FETCH: Stage.MAP,
    Stage.PARSE: Stage.FETCH,
    Stage.CLEAN: Stage.PARSE,
}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class WorkItem:
    """A norm together with what earlier stages recorded about it."""

    norm: Norm
    subset: str
    text_source: TextSource | None
    raw_path: str | None
    etag: str | None
    last_modified: str | None
    fetched_at: str | None


class StateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # ---------------------------------------------------------------- catalog

    def confirmed_partitions(self, source: str) -> set[str]:
        """Partitions that were listed successfully and returned at least one norm.

        An empty listing is never taken as final: catalog APIs sometimes answer a valid but
        empty page while overloaded, so empty partitions are queried again on every run.
        """
        rows = self._db.execute(
            "SELECT partition_key FROM discovery_pages "
            "WHERE source = ? AND status = ? AND item_count > 0",
            (source, Status.DONE),
        ).fetchall()
        return {row["partition_key"] for row in rows}

    def record_partition(
        self, source: str, partition_key: str, status: Status, item_count: int, message: str = ""
    ) -> None:
        self._db.execute(
            """
            INSERT INTO discovery_pages (source, partition_key, status, item_count, message,
                                         updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, partition_key) DO UPDATE SET
                status = excluded.status, item_count = excluded.item_count,
                message = excluded.message, updated_at = excluded.updated_at
            """,
            (source, partition_key, status, item_count, message, _now()),
        )

    def upsert_norms(self, norms: Iterable[tuple[Norm, str]]) -> int:
        """Insert new norms (with their subset); refresh descriptive fields of known ones."""
        now = _now()
        rows = [
            (
                norm.norm_id,
                norm.kind,
                norm.number,
                norm.signed_on.isoformat(),
                norm.jurisdiction.sphere,
                norm.jurisdiction.uf,
                norm.jurisdiction.municipality,
                subset,
                norm.title,
                norm.summary,
                norm.catalog_source,
                norm.catalog_ref,
                orjson.dumps(norm.extra).decode(),
                now,
                now,
            )
            for norm, subset in norms
        ]
        with self.transaction():
            self._db.executemany(
                """
                INSERT INTO norms (norm_id, kind, number, signed_on, sphere, uf, municipality,
                                   subset, title, summary, catalog_source, catalog_ref,
                                   extra_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (norm_id) DO UPDATE SET
                    title = excluded.title, summary = excluded.summary,
                    subset = excluded.subset, extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------- work items

    def pending_items(
        self,
        stage: Stage,
        *,
        kinds: Iterable[NormKind] | None = None,
        limit: int | None = None,
    ) -> list[WorkItem]:
        """Norms that still need ``stage``: never attempted, pending, or retryably failed."""
        prerequisite = PREREQUISITE[stage]
        sql = [
            """
            SELECT n.*, t.source_name, t.url, t.text_version, t.raw_path, t.etag, t.last_modified,
                   t.fetched_at
            FROM norms n
            LEFT JOIN text_sources t ON t.norm_id = n.norm_id
            LEFT JOIN stage_status s ON s.norm_id = n.norm_id AND s.stage = ?
            """
        ]
        params: list[object] = [stage]
        if prerequisite is not None:
            sql.append(
                "JOIN stage_status p ON p.norm_id = n.norm_id AND p.stage = ? AND p.status = ?"
            )
            params += [prerequisite, Status.DONE]
        placeholders = ",".join("?" * len(RETRYABLE_REASONS))
        sql.append(
            f"""
            WHERE (s.status IS NULL OR s.status = ?
                   OR (s.status = ? AND s.reason IN ({placeholders})))
            """
        )
        params += [Status.PENDING, Status.FAILED, *RETRYABLE_REASONS]
        if kinds is not None:
            kind_list = list(kinds)
            sql.append(f"AND n.kind IN ({','.join('?' * len(kind_list))})")
            params += kind_list
        sql.append("ORDER BY n.signed_on DESC, n.norm_id")
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(limit)
        rows = self._db.execute("\n".join(sql), params).fetchall()
        return [self._row_to_item(row) for row in rows]

    def items_with_status(self, stage: Stage, status: Status) -> list[WorkItem]:
        rows = self._db.execute(
            """
            SELECT n.*, t.source_name, t.url, t.text_version, t.raw_path, t.etag, t.last_modified,
                   t.fetched_at
            FROM norms n
            JOIN stage_status s ON s.norm_id = n.norm_id AND s.stage = ? AND s.status = ?
            LEFT JOIN text_sources t ON t.norm_id = n.norm_id
            ORDER BY n.norm_id
            """,
            (stage, status),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> WorkItem:
        norm = Norm(
            norm_id=row["norm_id"],
            kind=NormKind(row["kind"]),
            number=row["number"],
            signed_on=dt.date.fromisoformat(row["signed_on"]),
            jurisdiction=Jurisdiction(Sphere(row["sphere"]), row["uf"], row["municipality"]),
            title=row["title"],
            summary=row["summary"],
            catalog_source=row["catalog_source"],
            catalog_ref=row["catalog_ref"],
            extra=orjson.loads(row["extra_json"]),
        )
        text_source = None
        if row["source_name"] is not None:
            text_source = TextSource(
                row["source_name"], row["url"], TextVersion(row["text_version"])
            )
        return WorkItem(
            norm=norm,
            subset=row["subset"],
            text_source=text_source,
            raw_path=row["raw_path"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            fetched_at=row["fetched_at"],
        )

    # ---------------------------------------------------------------- results

    def mark(
        self,
        norm_id: str,
        stage: Stage,
        status: Status,
        *,
        reason: str | None = None,
        message: str | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO stage_status (norm_id, stage, status, attempts, reason, message,
                                      updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT (norm_id, stage) DO UPDATE SET
                status = excluded.status, attempts = stage_status.attempts + 1,
                reason = excluded.reason, message = excluded.message,
                updated_at = excluded.updated_at
            """,
            (norm_id, stage, status, reason, (message or "")[:1000] or None, _now()),
        )

    def save_location(self, norm_id: str, location: Location) -> None:
        with self.transaction():
            self._db.execute(
                "UPDATE norms SET legal_force = ?, metadata_json = ?, updated_at = ? "
                "WHERE norm_id = ?",
                (
                    location.legal_force,
                    orjson.dumps(location.metadata, default=str).decode(),
                    _now(),
                    norm_id,
                ),
            )
            if location.text_source is None:
                self._db.execute("DELETE FROM text_sources WHERE norm_id = ?", (norm_id,))
                return
            source = location.text_source
            self._db.execute(
                """
                INSERT INTO text_sources (norm_id, source_name, url, text_version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (norm_id) DO UPDATE SET
                    source_name = excluded.source_name, url = excluded.url,
                    text_version = excluded.text_version,
                    raw_path = CASE WHEN text_sources.url = excluded.url
                                    THEN text_sources.raw_path END,
                    etag = CASE WHEN text_sources.url = excluded.url
                                THEN text_sources.etag END,
                    last_modified = CASE WHEN text_sources.url = excluded.url
                                         THEN text_sources.last_modified END
                """,
                (norm_id, source.source_name, source.url, source.text_version),
            )

    def save_download(
        self, norm_id: str, raw_path: str, etag: str | None, last_modified: str | None
    ) -> None:
        self._db.execute(
            "UPDATE text_sources SET raw_path = ?, etag = ?, last_modified = ?, fetched_at = ? "
            "WHERE norm_id = ?",
            (raw_path, etag, last_modified, _now(), norm_id),
        )

    def norm_metadata(self, norm_id: str) -> dict[str, object]:
        row = self._db.execute(
            "SELECT metadata_json FROM norms WHERE norm_id = ?", (norm_id,)
        ).fetchone()
        return orjson.loads(row["metadata_json"]) if row else {}

    # ------------------------------------------------------------ maintenance

    def mark_superseded_editions(self, kind: NormKind) -> int:
        """Skip earlier editions of norms that were re-issued (e.g. MPs re-edited until 2001).

        Editions share ``extra.base_number``; only the one with the highest ``extra.edition``
        is kept.
        """
        rows = self._db.execute(
            """
            SELECT n.norm_id FROM norms n
            WHERE n.kind = ?
              AND json_extract(n.extra_json, '$.base_number') IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM norms later
                WHERE later.kind = n.kind
                  AND json_extract(later.extra_json, '$.base_number')
                      = json_extract(n.extra_json, '$.base_number')
                  AND CAST(json_extract(later.extra_json, '$.edition') AS INTEGER)
                      > CAST(json_extract(n.extra_json, '$.edition') AS INTEGER)
              )
              AND NOT EXISTS (
                SELECT 1 FROM stage_status s
                WHERE s.norm_id = n.norm_id AND s.stage = 'map' AND s.status = 'skipped'
              )
            """,
            (kind,),
        ).fetchall()
        with self.transaction():
            for row in rows:
                self.mark(row["norm_id"], Stage.MAP, Status.SKIPPED, reason="superseded_edition")
        return len(rows)

    def reopen_failed(self, stage: Stage | None = None) -> int:
        """Turn failures back into pending so that the next run retries them."""
        sql = "UPDATE stage_status SET status = ?, updated_at = ? WHERE status = ?"
        params: list[object] = [Status.PENDING, _now(), Status.FAILED]
        if stage is not None:
            sql += " AND stage = ?"
            params.append(stage)
        return self._db.execute(sql, params).rowcount

    # --------------------------------------------------------------- reports

    def status_counts(self) -> list[sqlite3.Row]:
        return self._db.execute(
            """
            SELECT stage, status, COALESCE(reason, '') AS reason, COUNT(*) AS total
            FROM stage_status GROUP BY stage, status, reason ORDER BY stage, status, reason
            """
        ).fetchall()

    def norm_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM norms").fetchone()[0]

    def source_map_summary(self) -> list[sqlite3.Row]:
        """Per kind: how many norms are in force and which source provides their text."""
        return self._db.execute(
            """
            SELECT n.kind, n.legal_force,
                   COALESCE(t.source_name || ':' || t.text_version, '-') AS text_source,
                   COALESCE(s.status, 'not_mapped') AS map_status,
                   COUNT(*) AS total
            FROM norms n
            LEFT JOIN text_sources t ON t.norm_id = n.norm_id
            LEFT JOIN stage_status s ON s.norm_id = n.norm_id AND s.stage = 'map'
            GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
            """
        ).fetchall()

    def partition_summary(self) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT source, status, COUNT(*) AS partitions, SUM(item_count) AS items "
            "FROM discovery_pages GROUP BY source, status"
        ).fetchall()
