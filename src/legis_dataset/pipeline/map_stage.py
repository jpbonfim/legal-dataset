"""Stage 1 — build the work list: every norm listed in the catalog.

Discovery is checkpointed per catalog partition (kind × year), so an interrupted run
resumes with the partitions it had not listed yet. Everything per norm (legal status,
text location, download) happens in the fetch stage.
"""

from __future__ import annotations

import datetime as dt

from legis_dataset.http.client import HttpClient
from legis_dataset.models import NormKind
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.reports import write_source_map_report
from legis_dataset.pipeline.runner import Outcome, RunSummary, run_concurrently
from legis_dataset.sources.base import CatalogPartition, CatalogSource, build_catalog
from legis_dataset.state.repository import Status

DISCOVERY_STAGE = "map:catalog"
RECHECK_STAGE = "map:catalog (recheck empty)"


class MapStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._settings = context.settings
        self._repository = context.repository

    async def run(self) -> RunSummary:
        async with self._context.new_http_client() as client:
            catalog = build_catalog(self._settings.sources.catalog, client, self._settings)
            summary = await self._discover(catalog, client)
        # Earlier editions of re-issued MPs are known from the catalog alone.
        self._repository.mark_superseded_editions(NormKind.MEDIDA_PROVISORIA)
        write_source_map_report(self._repository, self._settings.paths.reports_dir)
        return summary

    # ------------------------------------------------------------- discovery

    async def _discover(self, catalog: CatalogSource, client: HttpClient) -> RunSummary:
        catalog_settings = self._settings.catalog
        current_year = dt.date.today().year
        confirmed = self._repository.confirmed_partitions(catalog.name)
        partitions = [
            partition
            for partition in catalog.partitions(
                self._context.options.effective_kinds(self._settings),
                catalog_settings.first_year,
                catalog_settings.effective_last_year,
            )
            # The current year keeps receiving new norms, so it is always refreshed.
            if partition.year == current_year or partition.key not in confirmed
        ]
        summary, empty = await self._list_partitions(DISCOVERY_STAGE, partitions, catalog, client)
        if empty and not self._context.shutdown.requested:
            # An overloaded catalog may answer "no norms" for a while; ask again later.
            _, empty = await self._list_partitions(RECHECK_STAGE, empty, catalog, client)
        if empty:
            keys = ", ".join(partition.key for partition in empty[:10])
            more = f" (+{len(empty) - 10} more)" if len(empty) > 10 else ""
            self._context.reporter.info(
                f"[yellow]{len(empty)} catalog partitions are still empty: {keys}{more}. "
                "They will be queried again on the next run.[/]"
            )
        return summary

    async def _list_partitions(
        self,
        stage: str,
        partitions: list[CatalogPartition],
        catalog: CatalogSource,
        client: HttpClient,
    ) -> tuple[RunSummary, list[CatalogPartition]]:
        """List ``partitions`` and return the summary plus those that came back empty."""
        self._context.reporter.stage_started(stage, len(partitions), catalog.hosts)
        empty: list[CatalogPartition] = []

        async def discover(partition: CatalogPartition) -> Outcome:
            norms = await catalog.list_partition(partition)
            subset_by_kind = self._settings.subset_by_kind
            self._repository.upsert_norms((norm, subset_by_kind[norm.kind]) for norm in norms)
            return Outcome(Status.DONE, message=str(len(norms)))

        def record(partition: CatalogPartition, outcome: Outcome) -> None:
            count = int(outcome.message or 0) if outcome.status is Status.DONE else 0
            message = "" if outcome.status is Status.DONE else outcome.message or ""
            if outcome.status is Status.DONE and count == 0:
                empty.append(partition)
            self._repository.record_partition(
                catalog.name, partition.key, outcome.status, count, message
            )

        summary = await run_concurrently(
            stage,
            partitions,
            discover,
            record,
            concurrency=client.limiters.total_concurrency(list(catalog.hosts)),
            shutdown=self._context.shutdown,
            reporter=self._context.reporter,
        )
        self._context.reporter.stage_finished(stage, summary)
        return summary, empty
