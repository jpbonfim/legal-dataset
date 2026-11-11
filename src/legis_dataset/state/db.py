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
    return connection


def _apply_schema(connection: sqlite3.Connection) -> None:
    schema = resources.files("legis_dataset.state").joinpath("schema.sql").read_text("utf-8")
    connection.executescript(schema)
