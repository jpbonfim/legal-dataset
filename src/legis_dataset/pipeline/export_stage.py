"""Stage 5 — write the final Parquet datasets (one folder per subset) and their stats."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from legis_dataset.export.parquet import write_subset
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.records import read_jsonl
from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.state.repository import StateRepository, Status
from legis_dataset.stats.dataset_stats import DatasetStats, compute_dataset_stats, write_stats
from legis_dataset.stats.token_counter import load_token_counter

STAGE = "export"


class ExportStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._settings = context.settings

    async def run(self) -> RunSummary:
        paths = self._settings.paths
        subsets = [
            subset
            for subset in self._settings.subsets
            if (paths.clean_dir / f"{subset}.jsonl").exists()
        ]
        self._context.reporter.stage_started(STAGE, len(subsets), ["local CPU"])
        counter = load_token_counter(self._settings.stats.tokenizer)
        self._context.reporter.info(f"Counting tokens with: {counter.name}")
        summary = RunSummary()
        for subset in subsets:
            written = write_subset(
                read_jsonl(paths.clean_dir / f"{subset}.jsonl"),
                paths.final_dir / subset,
                counter=counter,
                max_rows_per_shard=self._settings.export.max_rows_per_shard,
                compression=self._settings.export.compression,
            )
            outcome = Outcome(Status.DONE, message=f"{subset}: {written} documents")
            summary.add(outcome)
            self._context.reporter.item_finished(STAGE, outcome)
        write_stats(
            current_stats(self._context.repository, self._settings.paths.final_dir),
            paths.final_dir / "stats.json",
        )
        self._context.reporter.stage_finished(STAGE, summary)
        return summary


def removal_reasons(repository: StateRepository) -> Counter[str]:
    """How many norms were dropped and why (revoked, no text, quality, duplicates)."""
    reasons: Counter[str] = Counter()
    for row in repository.status_counts():
        if row["status"] == Status.SKIPPED or (
            row["status"] == Status.FAILED and row["reason"] == "no_text_source"
        ):
            reasons[f"{row['stage']}:{row['reason']}"] += row["total"]
    return reasons


def current_stats(repository: StateRepository | None, final_dir: Path) -> DatasetStats:
    removed = removal_reasons(repository) if repository is not None else None
    return compute_dataset_stats(final_dir, removed=removed)
