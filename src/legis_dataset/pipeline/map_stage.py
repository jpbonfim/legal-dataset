"""Stage 1 — map the sources.

1. Discover norms in the catalog (checkpointed per kind × year).
2. For each norm, ask the locators whether it is in force and where its text is.
3. Write a report of which source covers which kinds of norms.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import orjson

from legis_dataset.http.client import HttpClient
from legis_dataset.models import LegalForce, Location, NormKind
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.runner import Outcome, RunSummary, run_concurrently
from legis_dataset.sources.base import (
    Capability,
    CatalogPartition,
    CatalogSource,
    Locator,
    build_catalog,
    build_locators,
)
from legis_dataset.state.repository import Stage, Status, WorkItem

DISCOVERY_STAGE = "map:catalog"
RECHECK_STAGE = "map:catalog (recheck empty)"
LOCATE_STAGE = "map:locate"


class MapStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._settings = context.settings
        self._repository = context.repository

    async def run(self) -> list[RunSummary]:
        async with self._context.new_http_client() as client:
            catalog = build_catalog(self._settings.sources.catalog, client, self._settings)
            locators = build_locators(self._settings.sources.locators, client, self._settings)
            discovery = await self._discover(catalog, client)
            if self._context.shutdown.requested:
                return [discovery]
            self._repository.mark_superseded_editions(NormKind.MEDIDA_PROVISORIA)
            location = await self._locate(locators, client)
        self._write_report()
        return [discovery, location]

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

    # -------------------------------------------------------------- location

    async def _locate(self, locators: Sequence[Locator], client: HttpClient) -> RunSummary:
        items = self._repository.pending_items(
            Stage.MAP,
            kinds=self._context.options.effective_kinds(self._settings),
            limit=self._context.options.limit,
        )
        hosts = sorted({host for locator in locators for host in locator.hosts})
        self._context.reporter.stage_started(LOCATE_STAGE, len(items), hosts)

        async def locate(item: WorkItem) -> Outcome:
            location = await locate_norm(item, locators)
            self._repository.save_location(item.norm.norm_id, location)
            if location.legal_force is LegalForce.NOT_IN_FORCE:
                return Outcome(Status.SKIPPED, "not_in_force")
            if location.text_source is None:
                # Recorded as a failure so that `retry-failed` tries again, e.g. after new
                # URL patterns are added to a locator.
                return Outcome(Status.FAILED, "no_text_source")
            return Outcome(Status.DONE, location.text_source.source_name)

        def record(item: WorkItem, outcome: Outcome) -> None:
            self._repository.mark(
                item.norm.norm_id,
                Stage.MAP,
                outcome.status,
                reason=outcome.reason,
                message=outcome.message,
            )

        summary = await run_concurrently(
            LOCATE_STAGE,
            items,
            locate,
            record,
            concurrency=client.limiters.total_concurrency(hosts),
            shutdown=self._context.shutdown,
            reporter=self._context.reporter,
        )
        self._context.reporter.stage_finished(LOCATE_STAGE, summary)
        return summary

    # ---------------------------------------------------------------- report

    def _write_report(self) -> None:
        reports_dir = self._settings.paths.reports_dir
        reports_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "catalog_partitions": [dict(row) for row in self._repository.partition_summary()],
            "norms": [dict(row) for row in self._repository.source_map_summary()],
        }
        target = reports_dir / "source_map.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
        tmp.replace(target)


async def locate_norm(item: WorkItem, locators: Sequence[Locator]) -> Location:
    """Ask locators in order, each only for information that is still unknown."""
    norm = item.norm
    result = Location()
    for locator in locators:
        if result.legal_force is LegalForce.NOT_IN_FORCE:
            break  # nothing else matters for a revoked norm
        needed = Capability(0)
        if result.legal_force is LegalForce.UNKNOWN:
            needed |= Capability.LEGAL_STATUS
        if result.text_source is None:
            needed |= Capability.TEXT
        if not needed or not (locator.provides & needed) or not locator.supports(norm):
            continue
        found = await locator.locate(norm)
        if result.legal_force is LegalForce.UNKNOWN:
            result.legal_force = found.legal_force
        if result.text_source is None and found.text_source is not None:
            result.text_source = found.text_source
        result.metadata.update({f"{locator.name}.{k}": v for k, v in found.metadata.items()})
        result.resolved_urn = result.resolved_urn or found.resolved_urn
    return result
