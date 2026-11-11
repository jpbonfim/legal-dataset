"""Statistics computed from the final Parquet files (no network needed)."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import orjson
import pyarrow.parquet as pq

STATS_COLUMNS = ["tipo", "fonte", "data_assinatura", "n_chars", "n_palavras", "n_tokens"]


@dataclass
class GroupStats:
    documents: int = 0
    tokens: int = 0

    def add(self, tokens: int) -> None:
        self.documents += 1
        self.tokens += tokens


@dataclass
class SubsetStats:
    name: str
    documents: int = 0
    tokens: int = 0
    chars: int = 0
    words: int = 0
    tokens_mean: float = 0.0
    tokens_median: float = 0.0
    tokens_p95: float = 0.0
    disk_bytes: int = 0
    shards: int = 0
    by_kind: dict[str, GroupStats] = field(default_factory=dict)
    by_source: dict[str, GroupStats] = field(default_factory=dict)
    by_decade: dict[str, GroupStats] = field(default_factory=dict)


@dataclass
class DatasetStats:
    tokenizer: str
    tokens_are_estimate: bool
    subsets: list[SubsetStats]
    removed: dict[str, int] = field(default_factory=dict)  # reason -> documents

    @property
    def total_documents(self) -> int:
        return sum(subset.documents for subset in self.subsets)

    @property
    def total_tokens(self) -> int:
        return sum(subset.tokens for subset in self.subsets)

    def to_json(self) -> bytes:
        payload = asdict(self) | {
            "total_documents": self.total_documents,
            "total_tokens": self.total_tokens,
        }
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2)


def _percentile(sorted_values: list[int], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return float(sorted_values[index])


def compute_subset_stats(subset_dir: Path) -> SubsetStats:
    stats = SubsetStats(name=subset_dir.name)
    shards = sorted(subset_dir.glob("*.parquet"))
    token_counts: list[int] = []
    for shard in shards:
        stats.disk_bytes += shard.stat().st_size
        table = pq.read_table(shard, columns=STATS_COLUMNS)
        for row in table.to_pylist():
            tokens = int(row["n_tokens"])
            token_counts.append(tokens)
            stats.chars += int(row["n_chars"])
            stats.words += int(row["n_palavras"])
            stats.by_kind.setdefault(str(row["tipo"]), GroupStats()).add(tokens)
            stats.by_source.setdefault(str(row["fonte"]), GroupStats()).add(tokens)
            decade = f"{row['data_assinatura'].year // 10 * 10}s"
            stats.by_decade.setdefault(decade, GroupStats()).add(tokens)
    stats.shards = len(shards)
    stats.documents = len(token_counts)
    stats.tokens = sum(token_counts)
    if token_counts:
        token_counts.sort()
        stats.tokens_mean = statistics.fmean(token_counts)
        stats.tokens_median = float(statistics.median(token_counts))
        stats.tokens_p95 = _percentile(token_counts, 0.95)
    stats.by_decade = dict(sorted(stats.by_decade.items()))
    return stats


def compute_dataset_stats(final_dir: Path, *, removed: Counter[str] | None = None) -> DatasetStats:
    subsets = (
        [
            compute_subset_stats(path)
            for path in sorted(final_dir.iterdir())
            if path.is_dir() and any(path.glob("*.parquet"))
        ]
        if final_dir.exists()
        else []
    )
    tokenizer, estimate = _tokenizer_metadata(final_dir)
    return DatasetStats(
        tokenizer=tokenizer,
        tokens_are_estimate=estimate,
        subsets=subsets,
        removed=dict(removed or {}),
    )


def _tokenizer_metadata(final_dir: Path) -> tuple[str, bool]:
    for shard in sorted(final_dir.glob("*/*.parquet")):
        metadata = pq.read_schema(shard).metadata or {}
        name = metadata.get(b"tokenizer", b"unknown").decode()
        estimate = metadata.get(b"tokens_are_estimate", b"false") == b"true"
        return name, estimate
    return "unknown", False


def write_stats(stats: DatasetStats, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(stats.to_json())
    tmp.replace(path)
