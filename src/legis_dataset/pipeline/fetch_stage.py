"""Stage 2 — download the raw text pages chosen by the map stage."""

from __future__ import annotations

import gzip
import os
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit

from legis_dataset.http.client import HttpClient
from legis_dataset.http.errors import PermanentError
from legis_dataset.models import safe_file_stem
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.runner import Outcome, RunSummary, run_concurrently
from legis_dataset.state.repository import Stage, Status, WorkItem

STAGE = "fetch"
MIN_PAGE_BYTES = 200


def _host_of(item: WorkItem) -> str:
    assert item.text_source is not None
    return urlsplit(item.text_source.url).hostname or ""


def interleave_by_host(items: list[WorkItem]) -> list[WorkItem]:
    """Round-robin items across hosts so no worker idles waiting on one busy host."""
    queues: dict[str, list[WorkItem]] = {}
    for item in items:
        queues.setdefault(_host_of(item), []).append(item)
    ordered: list[WorkItem] = []
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
        self._repository = context.repository
        self._raw_dir = context.settings.paths.raw_dir

    async def run(self) -> RunSummary:
        options = self._context.options
        items = [
            item
            for item in self._repository.pending_items(
                Stage.FETCH,
                kinds=options.effective_kinds(self._context.settings),
                limit=options.limit,
            )
            if item.text_source is not None
        ]
        hosts = sorted({_host_of(item) for item in items})
        self._context.reporter.stage_started(STAGE, len(items), hosts)
        async with self._context.new_http_client() as client:
            summary = await run_concurrently(
                STAGE,
                interleave_by_host(items),
                lambda item: self._fetch(client, item),
                self._record,
                concurrency=client.limiters.total_concurrency(hosts),
                shutdown=self._context.shutdown,
                reporter=self._context.reporter,
            )
        self._context.reporter.stage_finished(STAGE, summary)
        return summary

    def raw_path_for(self, item: WorkItem) -> Path:
        assert item.text_source is not None
        name = f"{safe_file_stem(item.norm.norm_id)}.html.gz"
        return self._raw_dir / item.text_source.source_name / name

    async def _fetch(self, client: HttpClient, item: WorkItem) -> Outcome:
        assert item.text_source is not None
        path = self.raw_path_for(item)
        already_downloaded = item.raw_path is not None and path.exists()
        result = await client.get(
            item.text_source.url,
            etag=item.etag if already_downloaded else None,
            last_modified=item.last_modified if already_downloaded else None,
        )
        if result.not_modified:
            return Outcome(Status.DONE, "not_modified")
        if len(result.content) < MIN_PAGE_BYTES:
            raise PermanentError(item.text_source.url, f"page too small ({len(result.content)} B)")
        write_atomically(path, gzip.compress(result.content, compresslevel=6))
        self._repository.save_download(
            item.norm.norm_id, str(path), result.etag, result.last_modified
        )
        return Outcome(Status.DONE)

    def _record(self, item: WorkItem, outcome: Outcome) -> None:
        self._repository.mark(
            item.norm.norm_id,
            Stage.FETCH,
            outcome.status,
            reason=outcome.reason,
            message=outcome.message,
        )
