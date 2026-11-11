"""SQLite connection management for the pipeline state."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and create if needed) the state database.

    WAL mode keeps the database consistent if the process is killed mid-write and lets
    `status` read it while a stage is running.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    _apply_schema(connection)
    _migrate_per_norm_map_status(connection)
    return connection


def _apply_schema(connection: sqlite3.Connection) -> None:
    schema = resources.files("legis_dataset.state").joinpath("schema.sql").read_text("utf-8")
    connection.executescript(schema)


def _migrate_per_norm_map_status(connection: sqlite3.Connection) -> None:
    """Fold the per-norm results of the former separate locate pass into ``fetch``.

    Locating a norm's text used to be its own pass (stage ``map``) before ``fetch``. Its
    knowledge is kept: revoked and superseded norms stay skipped, and the text sources it
    found stay in ``text_sources`` so ``fetch`` downloads them without locating again.
    Norms it could not locate simply become pending for ``fetch``. Idempotent.
    """
    # The connection is in autocommit mode, so the transaction is explicit.
    connection.execute("BEGIN")
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO stage_status (norm_id, stage, status, attempts, reason,
                                                message, updated_at)
            SELECT norm_id, 'fetch', status, attempts, reason, message, updated_at
            FROM stage_status WHERE stage = 'map' AND status = 'skipped'
            """
        )
        connection.execute("DELETE FROM stage_status WHERE stage = 'map'")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
