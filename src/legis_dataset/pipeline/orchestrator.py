"""Runs a sequence of stages, stopping cleanly when the user interrupts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from legis_dataset.pipeline.context import PipelineContext
from legis_dataset.pipeline.dedup_stage import DedupStage
from legis_dataset.pipeline.export_stage import ExportStage
from legis_dataset.pipeline.fetch_stage import FetchStage
from legis_dataset.pipeline.map_stage import MapStage
from legis_dataset.pipeline.parse_stage import ParseStage


class StageName(StrEnum):
    MAP = "map"
    FETCH = "fetch"
    PARSE = "parse"
    DEDUP = "dedup"
    EXPORT = "export"


ALL_STAGES = list(StageName)


class _Stage(Protocol):
    async def run(self) -> object: ...


_STAGE_CLASSES: dict[StageName, type] = {
    StageName.MAP: MapStage,
    StageName.FETCH: FetchStage,
    StageName.PARSE: ParseStage,
    StageName.DEDUP: DedupStage,
    StageName.EXPORT: ExportStage,
}


async def run_stages(context: PipelineContext, stages: Sequence[StageName]) -> bool:
    """Run ``stages`` in order. Returns ``False`` if the run was interrupted."""
    shutdown = context.shutdown
    shutdown.install()
    shutdown.on_request = lambda: context.reporter.info(
        "[yellow]Interrupt received: finishing in-flight items, then stopping. "
        "Press Ctrl+C again to stop immediately.[/]"
    )
    try:
        for name in stages:
            stage: _Stage = _STAGE_CLASSES[name](context)
            await stage.run()
            if shutdown.requested:
                return False
        return True
    except asyncio.CancelledError:
        return False
    finally:
        shutdown.uninstall()
