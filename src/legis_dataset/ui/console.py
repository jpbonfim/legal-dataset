"""Live terminal view: stage progress and what every source host is doing."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

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
from rich.table import Table

from legis_dataset.http.errors import HttpFetchError
from legis_dataset.http.rate_limit import HostLimiterRegistry, HostState
from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.state.repository import Status

logger = logging.getLogger(__name__)

# Human-readable names for the hosts shown in the panel.
SOURCE_LABELS = {
    "legis.senado.leg.br": "Senado Federal — Dados Abertos (catalog, legal status)",
    "normas.leg.br": "normas.leg.br — Congresso Nacional (legal status, text in force)",
    "www.planalto.gov.br": "Planalto — Casa Civil (text fallback)",
    "local CPU": "local processing",
}
_STATE_STYLE = {HostState.OK: "green", HostState.COOLING_DOWN: "yellow", HostState.HALTED: "red"}


@dataclass
class HostCounters:
    requests: int = 0
    ok: int = 0
    client_errors: int = 0
    server_errors: int = 0
    retries: int = 0
    failures: int = 0
    last_url: str = ""


class ConsoleUI:
    """Implements both :class:`StageReporter` and :class:`HttpObserver`."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[detail]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self.console,
        )
        self._tasks: dict[str, TaskID] = {}
        self._stage_counts: dict[str, dict[Status, int]] = defaultdict(lambda: defaultdict(int))
        self._hosts: dict[str, HostCounters] = defaultdict(HostCounters)
        self._registries: list[HostLimiterRegistry] = []
        self._live = Live(
            self._render(), console=self.console, refresh_per_second=4, get_renderable=self._render
        )

    # ------------------------------------------------------------- lifecycle

    def __enter__(self) -> ConsoleUI:
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._live.stop()

    def watch_limiters(self, registry: HostLimiterRegistry) -> None:
        self._registries.append(registry)

    # -------------------------------------------------------- StageReporter

    def stage_started(self, stage: str, total: int, hosts: Iterable[str]) -> None:
        sources = [SOURCE_LABELS.get(host, host) for host in hosts]
        self.console.print(
            f"[bold cyan]▶ {stage}[/]: {total:,} items — sources: "
            + (", ".join(sources) if sources else "none")
        )
        self._tasks[stage] = self._progress.add_task(stage, total=total or 1, detail="")
        if total == 0:
            self._progress.update(self._tasks[stage], completed=1, detail="nothing to do")

    def item_finished(self, stage: str, outcome: Outcome) -> None:
        counts = self._stage_counts[stage]
        counts[outcome.status] += 1
        detail = "  ".join(f"{status.value}={total:,}" for status, total in counts.items() if total)
        self._progress.update(self._tasks[stage], advance=1, detail=detail)

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
        counters = self._hosts[host]
        counters.requests += 1
        counters.last_url = url

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

    # ------------------------------------------------------------ rendering

    def _render(self) -> Group:
        return Group(self._progress, self._hosts_table())

    def _hosts_table(self) -> Table:
        table = Table(title="Sources (per host)", title_justify="left", expand=False)
        for column in (
            "host",
            "state",
            "rate req/s",
            "in flight",
            "requests",
            "ok",
            "4xx",
            "5xx",
            "retries",
            "failed",
        ):
            table.add_column(column, justify="left" if column in {"host", "state"} else "right")
        snapshots = {s.host: s for registry in self._registries for s in registry.snapshots()}
        for host, counters in sorted(self._hosts.items()):
            snapshot = snapshots.get(host)
            state = snapshot.state if snapshot else HostState.OK
            rate = (
                f"{snapshot.current_rate:.2f}/{snapshot.configured_rate:.2f}" if snapshot else "-"
            )
            in_flight = f"{snapshot.in_flight}/{snapshot.max_concurrency}" if snapshot else "-"
            table.add_row(
                host,
                f"[{_STATE_STYLE[state]}]{state.value}",
                rate,
                in_flight,
                f"{counters.requests:,}",
                f"{counters.ok:,}",
                f"{counters.client_errors:,}",
                f"{counters.server_errors:,}",
                f"{counters.retries:,}",
                f"{counters.failures:,}",
            )
        return table
