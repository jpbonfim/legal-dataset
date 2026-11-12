"""Both progress displays draw the same aligned panel; snapshot mode prints it as new lines."""

from __future__ import annotations

import io
import re

from rich.console import Console

from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.state.repository import Status
from legis_dataset.ui.console import (
    LiveConsoleUI,
    ProgressMode,
    ProgressTracker,
    SnapshotConsoleUI,
    create_console_ui,
)

HOSTS = ("legis.senado.leg.br", "normas.leg.br", "www.planalto.gov.br")
ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _console(*, terminal: bool) -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=terminal, color_system="standard")
    return console, output


def _feed(ui: ProgressTracker) -> None:
    for stage, total in (("map:catalog", 73), ("map:recheck", 34), ("fetch", 165969)):
        ui.stage_started(stage, total, [])
    ui.item_finished("fetch", Outcome(Status.DONE))
    for host in HOSTS:
        ui.on_request(host, "GET", "u")
        ui.on_response(host, 200, 0.1)


def test_panel_fits_in_80_columns_with_aligned_bars() -> None:
    console, output = _console(terminal=False)
    ui = ProgressTracker(console)
    _feed(ui)
    console.print(ui.render())
    lines = ANSI_STYLE.sub("", output.getvalue()).splitlines()
    assert all(len(line) <= 80 for line in lines)
    stage_lines = [line for line in lines if line.startswith(("map:", "fetch "))]
    assert len(stage_lines) == 3
    assert all("━" in line for line in stage_lines)  # bars are drawn, not only text
    # Fixed widths: bars and counts line up although stage names and totals differ.
    assert len({line.index("━") for line in stage_lines}) == 1
    assert len({line.index("0:00:00") for line in stage_lines}) == 1
    assert any(line.startswith("planalto") for line in lines)
    assert not any("…" in line for line in lines)


def test_snapshot_prints_the_whole_panel_as_complete_lines() -> None:
    console, output = _console(terminal=False)
    clock = FakeClock()
    ui = SnapshotConsoleUI(console, interval_seconds=15, clock=clock)
    _feed(ui)
    assert "Sources" not in output.getvalue()  # nothing before the interval elapses

    clock.now = 20
    ui.item_finished("fetch", Outcome(Status.DONE))
    text = ANSI_STYLE.sub("", output.getvalue())
    assert text.count("Sources") == 1 and "━" in text and "2/165969" in text
    assert text.endswith("\n")  # the last line is complete: nothing is held back
    assert "\x1b[1A" not in text and "\x1b[2K" not in text  # no redraw in place

    ui.stage_finished("fetch", RunSummary())  # a finished stage always gets a snapshot
    assert output.getvalue().count("Sources") == 2


def test_auto_mode_redraws_in_place_only_in_a_real_terminal() -> None:
    live = create_console_ui(_console(terminal=True)[0], ProgressMode.AUTO)
    snapshot = create_console_ui(_console(terminal=False)[0], ProgressMode.AUTO)
    assert isinstance(live, LiveConsoleUI)
    assert isinstance(snapshot, SnapshotConsoleUI)
