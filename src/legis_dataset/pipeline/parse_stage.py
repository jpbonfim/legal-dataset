"""Stage 3 — turn raw pages into clean document records (CPU-bound, process pool)."""

from __future__ import annotations

import asyncio
import gzip
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from legis_dataset.models import ParsedDocument
from legis_dataset.parsing.base import ParseError, get_parser
from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.records import JsonlPartWriter, build_record
from legis_dataset.pipeline.runner import Outcome, RunSummary, run_concurrently
from legis_dataset.state.repository import Stage, Status, WorkItem

STAGE = "parse"


def parse_raw_file(
    source_name: str, raw_path: str, norm_id: str, fallback_title: str
) -> ParsedDocument | str:
    """Runs in a worker process. Returns the document, or an error message."""
    try:
        raw = gzip.decompress(Path(raw_path).read_bytes())
        return get_parser(source_name).parse(raw, norm_id=norm_id, fallback_title=fallback_title)
    except ParseError as error:
        return f"parse_error: {error}"
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        return f"raw_file_error: {error}"


class ParseStage:
    def __init__(self, context: PipelineContext) -> None:
        self._context = context
        self._repository = context.repository
        self._settings = context.settings

    async def run(self) -> RunSummary:
        options = self._context.options
        items = [
            item
            for item in self._repository.pending_items(
                Stage.PARSE,
                kinds=options.effective_kinds(self._settings),
                limit=options.limit,
            )
            if item.text_source is not None and item.raw_path
        ]
        workers = self._settings.processing.parse_workers or os.cpu_count() or 1
        self._context.reporter.stage_started(STAGE, len(items), ["local CPU"])
        loop = asyncio.get_running_loop()
        with (
            # "spawn": forking a process that already runs threads (asyncio, httpx) is unsafe.
            ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")
            ) as pool,
            JsonlPartWriter(self._settings.paths.parsed_dir) as writer,
        ):

            async def parse_one(item: WorkItem) -> Outcome:
                assert item.text_source is not None and item.raw_path is not None
                result = await loop.run_in_executor(
                    pool,
                    parse_raw_file,
                    item.text_source.source_name,
                    item.raw_path,
                    item.norm.norm_id,
                    item.norm.title,
                )
                if isinstance(result, str):
                    reason, _, message = result.partition(": ")
                    return Outcome(Status.FAILED, reason, message)
                writer.write(item.subset, build_record(item, result))
                return Outcome(Status.DONE)

            summary = await run_concurrently(
                STAGE,
                items,
                parse_one,
                self._record,
                concurrency=workers * 2,  # keep the pool busy while results are written
                shutdown=self._context.shutdown,
                reporter=self._context.reporter,
            )
        self._context.reporter.stage_finished(STAGE, summary)
        return summary

    def _record(self, item: WorkItem, outcome: Outcome) -> None:
        self._repository.mark(
            item.norm.norm_id,
            Stage.PARSE,
            outcome.status,
            reason=outcome.reason,
            message=outcome.message,
        )
