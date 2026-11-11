"""Stage 4 — normalize, filter and deduplicate the parsed documents.

This stage is global (duplicates can only be found by looking at everything), so it is
recomputed from all parsed records on every run. It is fast compared to the network
stages, and its output replaces the previous one atomically.
"""

from __future__ import annotations

from collections import defaultdict

from legis_dataset.models import TextVersion
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.records import read_latest_records, write_jsonl_atomically
from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.processing.dedup import Deduplicator, content_hash
from legis_dataset.processing.normalize import normalize_text
from legis_dataset.processing.quality import QualityRules, rejection_reason
from legis_dataset.state.repository import Stage, Status

STAGE = "dedup"

# Which copy to keep when two documents are duplicates: texts in force first.
_VERSION_PREFERENCE = {TextVersion.CURRENT: 0, TextVersion.COMPILED: 1, TextVersion.ORIGINAL: 2}


def _preference(record: dict[str, object]) -> tuple[int, str, str]:
    """Texts in force first, then the oldest norm, so that the original act is kept."""
    version = _VERSION_PREFERENCE.get(TextVersion(str(record["versao_texto"])), 9)
    return (version, str(record["data_assinatura"]), str(record["id"]))


class DedupStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._repository = context.repository
        self._settings = context.settings

    async def run(self) -> RunSummary:
        parsed_ids = {
            item.norm.norm_id
            for item in self._repository.items_with_status(Stage.PARSE, Status.DONE)
        }
        records = [
            record
            for record in read_latest_records(self._settings.paths.parsed_dir)
            if record["id"] in parsed_ids
        ]
        self._context.reporter.stage_started(STAGE, len(records), ["local CPU"])
        rules = QualityRules(min_chars=self._settings.processing.min_chars)
        kept_by_subset: dict[str, list[dict[str, object]]] = defaultdict(list)
        summary = RunSummary()
        decisions: list[tuple[str, Outcome]] = []

        by_subset: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in records:
            by_subset[str(record["subset"])].append(record)

        for subset, subset_records in by_subset.items():
            deduplicator = Deduplicator(
                self._settings.processing.near_duplicate_threshold,
                self._settings.processing.minhash_permutations,
            )
            for record in sorted(subset_records, key=_preference):
                text = normalize_text(str(record["text"]))
                outcome = self._judge(record, text, rules, deduplicator)
                if outcome.status is Status.DONE:
                    kept_by_subset[subset].append(
                        record | {"text": text, "sha256": content_hash(text)}
                    )
                decisions.append((str(record["id"]), outcome))
                summary.add(outcome)
                self._context.reporter.item_finished(STAGE, outcome)

        clean_dir = self._settings.paths.clean_dir
        for subset in self._settings.subsets:
            write_jsonl_atomically(clean_dir / f"{subset}.jsonl", kept_by_subset.get(subset, []))
        with self._repository.transaction():
            for doc_id, outcome in decisions:
                self._repository.mark(
                    doc_id,
                    Stage.CLEAN,
                    outcome.status,
                    reason=outcome.reason,
                    message=outcome.message,
                )
        self._context.reporter.stage_finished(STAGE, summary)
        return summary

    @staticmethod
    def _judge(
        record: dict[str, object], text: str, rules: QualityRules, deduplicator: Deduplicator
    ) -> Outcome:
        reason = rejection_reason(text, rules)
        if reason is not None:
            return Outcome(Status.SKIPPED, f"quality:{reason}")
        decision = deduplicator.decide(str(record["id"]), text)
        if decision.duplicate_of is not None:
            return Outcome(Status.SKIPPED, f"duplicate:{decision.kind}", decision.duplicate_of)
        return Outcome(Status.DONE)
