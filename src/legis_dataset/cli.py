"""Command-line interface: ``legis-dataset <command>``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from legis_dataset.config import Settings, load_settings
from legis_dataset.logging_setup import configure_logging
from legis_dataset.models import NormKind
from legis_dataset.pipeline.context import PipelineContext, RunOptions
from legis_dataset.pipeline.export_stage import current_stats
from legis_dataset.pipeline.orchestrator import ALL_STAGES, StageName, run_stages
from legis_dataset.state.db import connect
from legis_dataset.state.repository import Stage, StateRepository
from legis_dataset.stats.report import render_stats
from legis_dataset.ui.console import ConsoleUI

app = typer.Typer(
    help="Build a CPT dataset of Brazilian federal legislation in force from official sources.",
    no_args_is_help=True,
)
console = Console()

ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="TOML config (default: config/default.toml).")
]
KindsOption = Annotated[
    str | None,
    typer.Option(help="Comma-separated norm kinds, e.g. 'lei,decreto' (default: from config)."),
]
LimitOption = Annotated[
    int | None, typer.Option(help="Process at most N norms per stage (useful for testing).")
]
YearsOption = Annotated[
    str | None, typer.Option(help="Catalog year range, e.g. '2020-2024' (default: from config).")
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Show info logs.")]


def _parse_kinds(value: str | None) -> list[NormKind] | None:
    if not value:
        return None
    try:
        return [NormKind(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as error:
        valid = ", ".join(kind.value for kind in NormKind)
        raise typer.BadParameter(f"{error}. Valid kinds: {valid}") from None


def _apply_years(settings: Settings, years: str | None) -> None:
    if not years:
        return
    first, _, last = years.partition("-")
    try:
        settings.catalog.first_year = int(first)
        settings.catalog.last_year = int(last or first)
    except ValueError:
        raise typer.BadParameter("use the form 'YYYY' or 'YYYY-YYYY'") from None


def _open_repository(settings: Settings) -> StateRepository:
    return StateRepository(connect(settings.paths.state_db))


def _execute(
    stages: list[StageName],
    config: Path | None,
    kinds: str | None,
    limit: int | None,
    years: str | None,
    verbose: bool,
    *,
    show_stats: bool,
) -> None:
    settings = load_settings(config)
    _apply_years(settings, years)
    log_path = configure_logging(settings.paths.logs_dir, console, verbose=verbose)
    repository = _open_repository(settings)
    console.print(f"Logs: {log_path}   State: {settings.paths.state_db}")
    with ConsoleUI(console) as ui:
        context = PipelineContext(
            settings=settings,
            repository=repository,
            options=RunOptions(kinds=_parse_kinds(kinds), limit=limit),
            reporter=ui,
            http_observer=ui,
        )
        completed = asyncio.run(run_stages(context, stages))
    if not completed:
        console.print("[yellow]Stopped before finishing. Run the same command again to resume.")
        raise typer.Exit(code=130)
    if show_stats:
        render_stats(current_stats(repository, settings.paths.final_dir), console)


@app.command()
def run(
    config: ConfigOption = None,
    kinds: KindsOption = None,
    limit: LimitOption = None,
    years: YearsOption = None,
    stats_only: Annotated[
        bool, typer.Option("--stats-only", help="Only show statistics of the existing dataset.")
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """Run every stage (map → fetch → parse → dedup → export) and show statistics."""
    if stats_only:
        stats(config)
        return
    _execute(ALL_STAGES, config, kinds, limit, years, verbose, show_stats=True)


@app.command(name="map")
def map_sources(
    config: ConfigOption = None,
    kinds: KindsOption = None,
    limit: LimitOption = None,
    years: YearsOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Stage 1: discover norms and find their legal status and text source."""
    _execute([StageName.MAP], config, kinds, limit, years, verbose, show_stats=False)


@app.command()
def fetch(
    config: ConfigOption = None,
    kinds: KindsOption = None,
    limit: LimitOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Stage 2: download the raw texts."""
    _execute([StageName.FETCH], config, kinds, limit, None, verbose, show_stats=False)


@app.command()
def parse(
    config: ConfigOption = None,
    kinds: KindsOption = None,
    limit: LimitOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Stage 3: extract clean text from the raw pages."""
    _execute([StageName.PARSE], config, kinds, limit, None, verbose, show_stats=False)


@app.command()
def dedup(config: ConfigOption = None, verbose: VerboseOption = False) -> None:
    """Stage 4: quality filters and deduplication."""
    _execute([StageName.DEDUP], config, None, None, None, verbose, show_stats=False)


@app.command()
def export(config: ConfigOption = None, verbose: VerboseOption = False) -> None:
    """Stage 5: write the Parquet datasets and show statistics."""
    _execute([StageName.EXPORT], config, None, None, None, verbose, show_stats=True)


@app.command()
def stats(config: ConfigOption = None) -> None:
    """Show statistics of the existing dataset (offline; nothing is run)."""
    settings = load_settings(config)
    repository = _open_repository(settings) if settings.paths.state_db.exists() else None
    render_stats(current_stats(repository, settings.paths.final_dir), console)


@app.command()
def status(config: ConfigOption = None) -> None:
    """Show how many norms are in each state of each stage."""
    settings = load_settings(config)
    if not settings.paths.state_db.exists():
        console.print("No state yet. Run `legis-dataset map` first.")
        return
    repository = _open_repository(settings)
    console.print(f"Norms in catalog: {repository.norm_count():,}")
    table = Table(title="Progress by stage")
    for column in ("stage", "status", "reason", "norms"):
        table.add_column(column, justify="right" if column == "norms" else "left")
    for row in repository.status_counts():
        table.add_row(row["stage"], row["status"], row["reason"], f"{row['total']:,}")
    console.print(table)


@app.command(name="retry-failed")
def retry_failed(
    config: ConfigOption = None,
    stage: Annotated[
        Stage | None, typer.Option(help="Only this stage (default: all stages).")
    ] = None,
) -> None:
    """Mark failed items as pending so the next run tries them again."""
    settings = load_settings(config)
    reopened = _open_repository(settings).reopen_failed(stage)
    console.print(f"{reopened:,} failed items will be retried on the next run.")


if __name__ == "__main__":
    app()
