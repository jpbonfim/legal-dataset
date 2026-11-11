"""Stage 2 — for each norm: find whether it is in force and where its text is, then download it.

Locating and downloading happen in one pass per norm. A locator that has to download a
page to confirm its URL (the Planalto) hands the page over, so it is never requested twice.
Norms whose text source is already known (e.g. retries) are downloaded directly.
"""

from __future__ import annotations

import gzip
import os
from collections.abc import Callable, Sequence
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit

from legis_dataset.http.client import HttpClient
from legis_dataset.http.errors import PermanentError
from legis_dataset.models import LegalForce, Location, RawPage, TextSource, safe_file_stem
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.reports import write_source_map_report
from legis_dataset.pipeline.runner import Outcome, RunSummary, run_concurrently
from legis_dataset.sources.base import Locator, build_locators, locate_norm
from legis_dataset.state.repository import Stage, Status, WorkItem

STAGE = "fetch"
MIN_PAGE_BYTES = 200
# Queue key of items whose text source still has to be located.
UNLOCATED = ""


def text_host(item: WorkItem) -> str:
    """Host of the item's known text source, or :data:`UNLOCATED`."""
    if item.text_source is None:
        return UNLOCATED
    return urlsplit(item.text_source.url).hostname or UNLOCATED


def interleave[T](items: list[T], key: Callable[[T], str]) -> list[T]:
    """Round-robin items across groups so no worker idles waiting on one busy host."""
    queues: dict[str, list[T]] = {}
    for item in items:
        queues.setdefault(key(item), []).append(item)
    ordered: list[T] = []
    for row in zip_longest(*queues.values()):
        ordered.extend(item for item in row if item is not None)
    return ordered


def write_atomically(path: Path, data: bytes) -> None:
    """Write via a temporary file + rename so a crash never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    tmp.replace(path)


class FetchStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._settings = context.settings
        self._repository = context.repository
        self._raw_dir = context.settings.paths.raw_dir

    async def run(self) -> RunSummary:
        options = self._context.options
        items = self._repository.pending_items(
            Stage.FETCH,
            kinds=options.effective_kinds(self._settings),
            limit=options.limit,
        )
        async with self._context.new_http_client() as client:
            locators = build_locators(self._settings.sources.locators, client, self._settings)
            hosts = sorted(
                {host for locator in locators for host in locator.hosts}
                | ({text_host(item) for item in items} - {UNLOCATED})
            )
            self._context.reporter.stage_started(STAGE, len(items), hosts)
            summary = await run_concurrently(
                STAGE,
                interleave(items, text_host),
                lambda item: self._acquire(client, locators, item),
                self._record,
                concurrency=client.limiters.total_concurrency(hosts),
                shutdown=self._context.shutdown,
                reporter=self._context.reporter,
            )
        write_source_map_report(self._repository, self._settings.paths.reports_dir)
        self._context.reporter.stage_finished(STAGE, summary)
        return summary

    def raw_path_for(self, norm_id: str, source: TextSource) -> Path:
        return self._raw_dir / source.source_name / f"{safe_file_stem(norm_id)}.html.gz"

    async def _acquire(
        self, client: HttpClient, locators: Sequence[Locator], item: WorkItem
    ) -> Outcome:
        location = await self._locate(locators, item)
        if location.legal_force is LegalForce.NOT_IN_FORCE:
            return Outcome(Status.SKIPPED, "not_in_force")
        source = location.text_source
        if source is None:
            # A failure, so that `retry-failed` tries again, e.g. after new URL patterns
            # are added to a locator.
            return Outcome(Status.FAILED, "no_text_source")
        page = location.page or await self._download(client, item, source)
        if page is None:
            return Outcome(Status.DONE, "not_modified")
        if len(page.content) < MIN_PAGE_BYTES:
            raise PermanentError(source.url, f"page too small ({len(page.content)} B)")
        path = self.raw_path_for(item.norm.norm_id, source)
        write_atomically(path, gzip.compress(page.content, compresslevel=6))
        self._repository.save_download(item.norm.norm_id, str(path), page.etag, page.last_modified)
        return Outcome(Status.DONE, source.source_name)

    async def _locate(self, locators: Sequence[Locator], item: WorkItem) -> Location:
        if item.text_source is not None:
            return Location(text_source=item.text_source)  # located on an earlier run
        location = await locate_norm(item.norm, locators)
        self._repository.save_location(item.norm.norm_id, location)
        return location

    async def _download(
        self, client: HttpClient, item: WorkItem, source: TextSource
    ) -> RawPage | None:
        """GET the text; ``None`` if the copy downloaded earlier is still current."""
        already_downloaded = (
            item.raw_path is not None and self.raw_path_for(item.norm.norm_id, source).exists()
        )
        result = await client.get(
            source.url,
            etag=item.etag if already_downloaded else None,
            last_modified=item.last_modified if already_downloaded else None,
        )
        if result.not_modified:
            return None
        return RawPage(result.content, result.etag, result.last_modified)

    def _record(self, item: WorkItem, outcome: Outcome) -> None:
        self._repository.mark(
            item.norm.norm_id,
            Stage.FETCH,
            outcome.status,
            reason=outcome.reason,
            message=outcome.message,
        )
