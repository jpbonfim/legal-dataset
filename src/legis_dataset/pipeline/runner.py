"""Concurrent execution of independent work items with graceful interruption.

Every item ends in exactly one :class:`Outcome`, which the caller persists. A failing item
never stops the others; an interrupted run leaves unfinished items untouched so the next
run picks them up again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from legis_dataset.http.errors import HostHaltedError, HttpFetchError
from legis_dataset.state.repository import Status

logger = logging.getLogger(__name__)

T = TypeVar("T")

SIGNAL_DEBOUNCE_SECONDS = 1.0


@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str | None = None
    message: str | None = None


@dataclass
class RunSummary:
    outcomes: Counter[tuple[Status, str]] = field(default_factory=Counter)
    deferred: int = 0  # items not attempted because their host was halted
    interrupted: bool = False

    def add(self, outcome: Outcome) -> None:
        self.outcomes[(outcome.status, outcome.reason or "")] += 1

    def count(self, status: Status) -> int:
        return sum(
            total for (item_status, _), total in self.outcomes.items() if item_status == status
        )


class StageReporter(Protocol):
    """Progress callbacks implemented by the console UI (or a no-op in tests)."""

    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None: ...
    def item_finished(self, stage: str, outcome: Outcome) -> None: ...
    def stage_finished(self, stage: str, summary: RunSummary) -> None: ...
    def info(self, message: str) -> None: ...


class NullReporter:
    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None:
        pass

    def item_finished(self, stage: str, outcome: Outcome) -> None:
        pass

    def stage_finished(self, stage: str, summary: RunSummary) -> None:
        pass

    def info(self, message: str) -> None:
        pass


class ShutdownController:
    """First Ctrl+C: finish in-flight items and stop. Second Ctrl+C: cancel immediately."""

    def __init__(self) -> None:
        self._requested = False
        self._first_signal_at: float | None = None
        self._main_task: asyncio.Task[object] | None = None
        self.on_request: Callable[[], None] | None = None

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> None:
        self._requested = True

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Not available on Windows or outside the main thread; Ctrl+C then aborts.
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self._handle_signal)

    def uninstall(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)

    def _handle_signal(self) -> None:
        now = time.monotonic()
        if self._first_signal_at is None:
            self._first_signal_at = now
            self._requested = True
            if self.on_request is not None:
                self.on_request()
            return
        # Wrappers such as `uv run` forward Ctrl+C while the terminal also delivers it, so
        # one key press can arrive twice; only a later, separate press forces the stop.
        if now - self._first_signal_at > SIGNAL_DEBOUNCE_SECONDS and self._main_task:
            self._main_task.cancel()


async def run_concurrently[T](
    stage: str,
    items: Iterable[T],
    worker: Callable[[T], Awaitable[Outcome]],
    record: Callable[[T, Outcome], None],
    *,
    concurrency: int,
    shutdown: ShutdownController,
    reporter: StageReporter,
) -> RunSummary:
    summary = RunSummary()
    iterator = iter(items)

    async def consume() -> None:
        while not shutdown.requested:
            try:
                item = next(iterator)
            except StopIteration:
                return
            try:
                outcome = await worker(item)
            except HostHaltedError:
                summary.deferred += 1
                continue
            except HttpFetchError as error:
                outcome = Outcome(Status.FAILED, error.reason, str(error))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # a bug must not take the whole run down
                logger.exception("unexpected error in stage %s", stage)
                outcome = Outcome(Status.FAILED, "unexpected", f"{type(error).__name__}: {error}")
            record(item, outcome)
            summary.add(outcome)
            reporter.item_finished(stage, outcome)

    await asyncio.gather(*(consume() for _ in range(max(1, concurrency))))
    summary.interrupted = shutdown.requested
    return summary
