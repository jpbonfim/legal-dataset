"""Parquet schema and sharded, atomic writing of a subset."""

from __future__ import annotations

import datetime as dt
import re
import shutil
from collections.abc import Iterable, Iterator
from itertools import islice
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from legis_dataset.stats.token_counter import TokenCounter

_WORDS = re.compile(r"\w+", re.UNICODE)
TOKENIZE_BATCH = 256

ANNOTATION_TYPE = pa.struct(
    [("tipo", pa.string()), ("texto", pa.string()), ("norma_ref", pa.string())]
)
SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("text", pa.string()),
        ("title", pa.string()),
        ("ementa", pa.string()),
        ("tipo", pa.string()),
        ("numero", pa.string()),
        ("data_assinatura", pa.date32()),
        ("esfera", pa.string()),
        ("uf", pa.string()),
        ("municipio", pa.string()),
        ("fonte", pa.string()),
        ("url", pa.string()),
        ("versao_texto", pa.string()),
        ("annotations", pa.list_(ANNOTATION_TYPE)),
        ("n_chars", pa.int64()),
        ("n_palavras", pa.int64()),
        ("n_tokens", pa.int64()),
        ("sha256", pa.string()),
        ("data_coleta", pa.date32()),
    ]
)


def _batched(records: Iterable[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    iterator = iter(records)
    while batch := list(islice(iterator, size)):
        yield batch


def to_rows(records: list[dict[str, object]], counter: TokenCounter) -> list[dict[str, object]]:
    texts = [str(record["text"]) for record in records]
    token_counts = counter.count(texts)
    rows: list[dict[str, object]] = []
    for record, text, tokens in zip(records, texts, token_counts, strict=True):
        row = {name: record.get(name) for name in SCHEMA.names}
        row["data_assinatura"] = dt.date.fromisoformat(str(record["data_assinatura"]))
        row["data_coleta"] = dt.date.fromisoformat(str(record["data_coleta"])[:10])
        row["n_chars"] = len(text)
        row["n_palavras"] = len(_WORDS.findall(text))
        row["n_tokens"] = tokens
        rows.append(row)
    return rows


def write_subset(
    records: Iterable[dict[str, object]],
    target_dir: Path,
    *,
    counter: TokenCounter,
    max_rows_per_shard: int,
    compression: str,
) -> int:
    """Write ``records`` as ``part-00000.parquet``... and swap the folder in atomically."""
    staging = target_dir.with_name(f".{target_dir.name}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    metadata = {
        b"tokenizer": counter.name.encode(),
        b"tokens_are_estimate": b"true" if counter.is_estimate else b"false",
        b"created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").encode(),
    }
    schema = SCHEMA.with_metadata(metadata)
    total = 0
    shard_index = 0
    writer: pq.ParquetWriter | None = None
    rows_in_shard = 0
    try:
        for batch in _batched(records, TOKENIZE_BATCH):
            for row_batch in _split_for_shards(batch, max_rows_per_shard, rows_in_shard):
                if writer is None:
                    path = staging / f"part-{shard_index:05d}.parquet"
                    writer = pq.ParquetWriter(path, schema, compression=compression)
                table = pa.Table.from_pylist(to_rows(row_batch, counter), schema=schema)
                writer.write_table(table)
                rows_in_shard += len(row_batch)
                total += len(row_batch)
                if rows_in_shard >= max_rows_per_shard:
                    writer.close()
                    writer, rows_in_shard, shard_index = None, 0, shard_index + 1
    finally:
        if writer is not None:
            writer.close()
    _swap_in(staging, target_dir)
    return total


def _split_for_shards(
    batch: list[dict[str, object]], max_rows: int, rows_in_shard: int
) -> Iterator[list[dict[str, object]]]:
    """Split a batch so that no shard exceeds ``max_rows``."""
    room = max_rows - rows_in_shard
    while batch:
        yield batch[:room]
        batch = batch[room:]
        room = max_rows


def _swap_in(staging: Path, target: Path) -> None:
    previous = target.with_name(f".{target.name}.previous")
    shutil.rmtree(previous, ignore_errors=True)
    if target.exists():
        target.rename(previous)
    staging.rename(target)
    shutil.rmtree(previous, ignore_errors=True)
