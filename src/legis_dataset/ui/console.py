"""Terminal progress: stage progress and what every source host is doing.

Two displays share the same counters:

- :class:`LiveConsoleUI` redraws a panel in place. It needs a real terminal.
- :class:`PlainConsoleUI` prints a snapshot as ordinary lines every few seconds. It is for
  outputs that only pass lines through, such as ``docker compose up``, CI logs or a file:
  there a redrawn panel turns into repeated, half-drawn copies.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column, Table

from legis_dataset.http.errors import HttpFetchError
from legis_dataset.http.rate_limit import HostLimiterRegistry, HostSnapshot, HostState
from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.state.repository import Status

logger = logging.getLogger(__name__)

# Short names keep the panel within the 80 columns of a default terminal (and of containers).
SOURCE_NAMES = {
    "legis.senado.leg.br": "senado",
    "normas.leg.br": "normas.leg",
    "www.planalto.gov.br": "planalto",
}
_STATE_STYLE = {HostState.OK: "green", HostState.COOLING_DOWN: "yellow", HostState.HALTED: "red"}
_STATE_LABEL = {HostState.OK: "ok", HostState.COOLING_DOWN: "cooldown", HostState.HALTED: "halted"}
# Fixed widths so that bars stay aligned while stages are added to the panel.
STAGE_NAME_WIDTH = 12
COUNT_WIDTH = 15  # "165969/165969"
PLAIN_SNAPSHOT_SECONDS = 15.0


class ProgressMode(StrEnum):
    AUTO = "auto"  # live in a real terminal, plain otherwise
    LIVE = "live"
    PLAIN = "plain"


def source_name(host: str) -> str:
    return SOURCE_NAMES.get(host, host)


@dataclass
class HostCounters:
    requests: int = 0
    ok: int = 0
    client_errors: int = 0
    server_errors: int = 0
    retries: int = 0
    failures: int = 0


@dataclass
class StageProgress:
    total: int
    started_at: float
    counts: dict[Status, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def completed(self) -> int:
        return sum(self.counts.values())

    @property
    def detail(self) -> str:
        return "  ".join(f"{status.value}={n:,}" for status, n in self.counts.items() if n)


@dataclass(frozen=True)
class HostRow:
    name: str
    state: HostState
    rate: str
    connections: str
    counters: HostCounters


class ProgressTracker:
    """Counts what stages and hosts do; subclasses decide how to show it.

    Implements both :class:`~legis_dataset.pipeline.runner.StageReporter` and
    :class:`~legis_dataset.http.client.HttpObserver`.
    """

    def __init__(self, console: Console, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.console = console
        self._clock = clock
        self._stages: dict[str, StageProgress] = {}
        self._hosts: dict[str, HostCounters] = defaultdict(HostCounters)
        self._registries: list[HostLimiterRegistry] = []

    def __enter__(self) -> ProgressTracker:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    def watch_limiters(self, registry: HostLimiterRegistry) -> None:
        self._registries.append(registry)

    # -------------------------------------------------------- StageReporter

    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None:
        sources = ", ".join(source_name(host) for host in hosts) or "none"
        self.console.print(f"[bold cyan]▶ {stage}[/]: {total:,} items — sources: {sources}")
        self._stages[stage] = StageProgress(total, self._clock())

    def item_finished(self, stage: str, outcome: Outcome) -> None:
        self._stages[stage].counts[outcome.status] += 1

    def stage_finished(self, stage: str, summary: RunSummary) -> None:
        parts = [
            f"{status.value}{'/' + reason if reason else ''}={total:,}"
            for (status, reason), total in sorted(summary.outcomes.items())
        ]
        if summary.deferred:
            parts.append(f"deferred (host halted)={summary.deferred:,}")
        note = " [yellow](interrupted — run again to resume)" if summary.interrupted else ""
        self.console.print(f"[bold]✔ {stage}[/]: " + (", ".join(parts) or "no items") + note)

    def info(self, message: str) -> None:
        self.console.print(message)

    # --------------------------------------------------------- HttpObserver

    def on_request(self, host: str, method: str, url: str) -> None:
        self._hosts[host].requests += 1

    def on_response(self, host: str, status_code: int, elapsed: float) -> None:
        counters = self._hosts[host]
        if status_code < 400:
            counters.ok += 1
        elif status_code < 500:
            counters.client_errors += 1
        else:
            counters.server_errors += 1

    def on_retry(self, host: str, reason: str, wait_seconds: float) -> None:
        self._hosts[host].retries += 1
        logger.info("retrying %s in %.1fs: %s", host, wait_seconds, reason)

    def on_failure(self, host: str, error: HttpFetchError) -> None:
        self._hosts[host].failures += 1
        logger.warning("request failed (%s): %s", error.reason, error)
        if error.reason == "blocked":
            self.console.print(
                f"[bold red]Host {host} answered with an anti-bot page and was halted for "
                "this run. Remaining items stay pending; try again later.[/]"
            )

    # ------------------------------------------------------------- helpers

    def host_rows(self) -> list[HostRow]:
        snapshots: dict[str, HostSnapshot] = {
            s.host: s for registry in self._registries for s in registry.snapshots()
        }
        rows: list[HostRow] = []
        for host, counters in sorted(self._hosts.items()):
            snapshot = snapshots.get(host)
            rows.append(
                HostRow(
                    name=source_name(host),
                    state=snapshot.state if snapshot else HostState.OK,
                    rate=(
                        f"{snapshot.current_rate:.1f}/{snapshot.configured_rate:.1f}"
                        if snapshot
                        else "-"
                    ),
                    connections=(
                        f"{snapshot.in_flight}/{snapshot.max_concurrency}" if snapshot else "-"
                    ),
                    counters=counters,
                )
            )
        return rows


class LiveConsoleUI(ProgressTracker):
    """Panel redrawn in place: one bar per stage and a table of hosts."""

    def __init__(self, console: Console) -> None:
        super().__init__(console)
        self._progress = Progress(
            TextColumn(
                "[bold]{task.description}",
                table_column=Column(width=STAGE_NAME_WIDTH, no_wrap=True),
            ),
            BarColumn(bar_width=24),
            MofNCompleteColumn(table_column=Column(width=COUNT_WIDTH, justify="right")),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            # Last, so that its changing length never shifts the other columns.
            TextColumn("{task.fields[detail]}", table_column=Column(no_wrap=True)),
            console=console,
        )
        self._tasks: dict[str, TaskID] = {}
        self._live = Live(console=console, refresh_per_second=4, get_renderable=self._render)

    def __enter__(self) -> LiveConsoleUI:
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._live.stop()

    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None:
        super().stage_started(stage, total, hosts)
        self._tasks[stage] = self._progress.add_task(stage, total=total or 1, detail="")
        if total == 0:
            self._progress.update(self._tasks[stage], completed=1, detail="nothing to do")

    def item_finished(self, stage: str, outcome: Outcome) -> None:
        super().item_finished(stage, outcome)
        detail = self._stages[stage].detail
        self._progress.update(self._tasks[stage], advance=1, detail=detail)

    def _render(self) -> Group:
        return Group(self._progress, self._hosts_table())

    def _hosts_table(self) -> Table:
        table = Table(
            title="Sources",
            title_justify="left",
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
            collapse_padding=True,
        )
        for column in ("source", "state", "req/s", "conn", "requests", "ok", "4xx", "5xx"):
            table.add_column(column, justify="left" if column in {"source", "state"} else "right")
        table.add_column("retry", justify="right")
        table.add_column("fail", justify="right")
        for row in self.host_rows():
            counters = row.counters
            table.add_row(
                row.name,
                f"[{_STATE_STYLE[row.state]}]{_STATE_LABEL[row.state]}",
                row.rate,
                row.connections,
                f"{counters.requests:,}",
                f"{counters.ok:,}",
                f"{counters.client_errors:,}",
                f"{counters.server_errors:,}",
                f"{counters.retries:,}",
                f"{counters.failures:,}",
            )
        return table


class PlainConsoleUI(ProgressTracker):
    """Prints the progress of running stages and every host as plain lines, periodically."""

    def __init__(
        self,
        console: Console,
        *,
        interval_seconds: float = PLAIN_SNAPSHOT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(console, clock=clock)
        self._interval = interval_seconds
        self._last_snapshot = clock()
        self._running: set[str] = set()

    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None:
        super().stage_started(stage, total, hosts)
        self._running.add(stage)

    def item_finished(self, stage: str, outcome: Outcome) -> None:
        super().item_finished(stage, outcome)
        self._maybe_snapshot()

    def on_response(self, host: str, status_code: int, elapsed: float) -> None:
        super().on_response(host, status_code, elapsed)
        self._maybe_snapshot()

    def stage_finished(self, stage: str, summary: RunSummary) -> None:
        self._running.discard(stage)
        self.print_snapshot(stages=[stage])
        super().stage_finished(stage, summary)

    def _maybe_snapshot(self) -> None:
        if self._clock() - self._last_snapshot >= self._interval:
            self.print_snapshot(stages=sorted(self._running))

    def print_snapshot(self, *, stages: Iterable[str]) -> None:
        self._last_snapshot = self._clock()
        lines = [self._stage_line(stage) for stage in stages]
        for row in self.host_rows():
            c = row.counters
            lines.append(
                f"  {row.name:<10} {_STATE_LABEL[row.state]} rate {row.rate} "
                f"conn {row.connections} | req {c.requests:,} ok {c.ok:,} "
                f"4xx {c.client_errors:,} 5xx {c.server_errors:,} "
                f"retry {c.retries:,} fail {c.failures:,}"
            )
        for line in lines:
            # Plain text, one line each: no markup parsing and no hard wrapping at the
            # console width, so log processors receive every line whole.
            self.console.print(line, markup=False, highlight=False, soft_wrap=True)

    def _stage_line(self, stage: str) -> str:
        progress = self._stages[stage]
        done, total = progress.completed, progress.total
        elapsed = self._clock() - progress.started_at
        percent = f"{100 * done / total:.1f}%" if total else "100%"
        eta = "-"
        if 0 < done < total:
            eta = _duration(elapsed / done * (total - done))
        detail = f"  {progress.detail}" if progress.detail else ""
        return (
            f"[{stage}] {done:,}/{total:,} ({percent}){detail}  "
            f"elapsed {_duration(elapsed)}  eta {eta}"
        )


def _duration(seconds: float) -> str:
    return str(dt.timedelta(seconds=round(seconds)))


def create_console_ui(console: Console, mode: ProgressMode) -> ProgressTracker:
    if mode is ProgressMode.AUTO:
        interactive = console.is_terminal and not console.is_dumb_terminal
        mode = ProgressMode.LIVE if interactive else ProgressMode.PLAIN
    if mode is ProgressMode.LIVE:
        return LiveConsoleUI(console)
    return PlainConsoleUI(console)
