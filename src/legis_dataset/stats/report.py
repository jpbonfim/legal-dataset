"""Terminal rendering of :class:`DatasetStats`."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table

from legis_dataset.stats.dataset_stats import DatasetStats, GroupStats, SubsetStats


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def _overview(stats: DatasetStats) -> Table:
    table = Table(title="Dataset overview", title_justify="left")
    for column in (
        "subset",
        "documents",
        "tokens",
        "words",
        "chars",
        "tokens/doc mean",
        "median",
        "p95",
        "shards",
        "size",
    ):
        table.add_column(column, justify="left" if column == "subset" else "right")
    for subset in stats.subsets:
        table.add_row(
            subset.name,
            f"{subset.documents:,}",
            f"{subset.tokens:,}",
            f"{subset.words:,}",
            f"{subset.chars:,}",
            f"{subset.tokens_mean:,.0f}",
            f"{subset.tokens_median:,.0f}",
            f"{subset.tokens_p95:,.0f}",
            str(subset.shards),
            _human_bytes(subset.disk_bytes),
        )
    table.add_row(
        "[bold]total",
        f"[bold]{stats.total_documents:,}",
        f"[bold]{stats.total_tokens:,}",
        f"{sum(s.words for s in stats.subsets):,}",
        f"{sum(s.chars for s in stats.subsets):,}",
        "",
        "",
        "",
        str(sum(s.shards for s in stats.subsets)),
        _human_bytes(sum(s.disk_bytes for s in stats.subsets)),
    )
    return table


def _grouped(title: str, groups: dict[str, GroupStats]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("value")
    table.add_column("documents", justify="right")
    table.add_column("tokens", justify="right")
    for key, group in sorted(groups.items(), key=lambda kv: -kv[1].tokens):
        table.add_row(key, f"{group.documents:,}", f"{group.tokens:,}")
    return table


def _subset_detail(subset: SubsetStats) -> Panel:
    return Panel(
        Group(
            _grouped("by kind of norm", subset.by_kind),
            _grouped("by text source", subset.by_source),
            _grouped("by decade signed", subset.by_decade),
        ),
        title=f"subset: {subset.name}",
    )


def render_stats(stats: DatasetStats, console: Console) -> None:
    if not stats.subsets:
        console.print("[yellow]No Parquet files found. Run the pipeline (or `export`) first.")
        return
    estimate = " [red](ESTIMATE — tokenizer unavailable)" if stats.tokens_are_estimate else ""
    console.print(f"Tokenizer: [bold]{stats.tokenizer}[/]{estimate}")
    console.print(_overview(stats))
    for subset in stats.subsets:
        console.print(_subset_detail(subset))
    if stats.removed:
        table = Table(title="Documents removed by filters/dedup", title_justify="left")
        table.add_column("reason")
        table.add_column("documents", justify="right")
        for reason, total in sorted(stats.removed.items(), key=lambda kv: -kv[1]):
            table.add_row(reason, f"{total:,}")
        console.print(table)
