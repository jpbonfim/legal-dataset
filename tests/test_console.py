"""The plain progress display must work where output only passes through line by line."""

from __future__ import annotations

import io

from rich.console import Console

from legis_dataset.pipeline.runner import Outcome, RunSummary
from legis_dataset.state.repository import Status
from legis_dataset.ui.console import (
    LiveConsoleUI,
    PlainConsoleUI,
    ProgressMode,
    create_console_ui,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _console(*, terminal: bool) -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return Console(file=output, width=80, force_terminal=terminal, color_system=None), output


def test_plain_ui_prints_periodic_snapshots_as_complete_lines() -> None:
    console, output = _console(terminal=False)
    clock = FakeClock()
    ui = PlainConsoleUI(console, interval_seconds=15, clock=clock)
    ui.stage_started("fetch", 4, ["www.planalto.gov.br"])
    ui.on_request("www.planalto.gov.br", "GET", "u")
    ui.on_response("www.planalto.gov.br", 404, 0.1)
    ui.item_finished("fetch", Outcome(Status.DONE))
    assert "[fetch]" not in output.getvalue()  # nothing before the interval elapses

    clock.now = 20
    ui.item_finished("fetch", Outcome(Status.DONE))
    text = output.getvalue()
    assert "[fetch] 2/4 (50.0%)  done=2  elapsed 0:00:20  eta 0:00:20" in text
    assert "planalto" in text and "4xx 1" in text
    assert all(len(line) <= 80 for line in text.splitlines())
    assert text.endswith("\n")
    assert "\x1b" not in text  # no cursor movement or other control sequences

    ui.stage_finished("fetch", RunSummary())
    assert "✔ fetch" in output.getvalue()


def test_auto_mode_picks_live_only_in_a_real_terminal() -> None:
    assert isinstance(
        create_console_ui(_console(terminal=True)[0], ProgressMode.AUTO), LiveConsoleUI
    )
    assert isinstance(
        create_console_ui(_console(terminal=False)[0], ProgressMode.AUTO), PlainConsoleUI
    )


def test_live_panel_fits_in_80_columns() -> None:
    console, output = _console(terminal=True)
    ui = LiveConsoleUI(console)
    for stage, total in (("map:catalog", 73), ("map:recheck", 34), ("fetch", 165969)):
        ui.stage_started(stage, total, [])
    for host in ("legis.senado.leg.br", "normas.leg.br", "www.planalto.gov.br"):
        for _ in range(3):
            ui.on_request(host, "GET", "u")
            ui.on_response(host, 200, 0.1)
    console.print(ui._render())
    lines = output.getvalue().splitlines()
    assert all(len(line) <= 80 for line in lines)
    assert not any("…" in line for line in lines if "Sources" in line or "planalto" in line)
    stage_lines = [line for line in lines if line.startswith(("map:", "fetch "))]
    assert len(stage_lines) == 3
    # Fixed widths: bars, counts and times line up although stage names and totals differ.
    assert len({line.index("0:00:00") for line in stage_lines}) == 1
