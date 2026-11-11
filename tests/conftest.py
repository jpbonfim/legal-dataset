from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from legis_dataset.config import Settings, load_settings
from legis_dataset.models import FEDERAL, Norm, NormKind, build_urn
from legis_dataset.state.db import connect
from legis_dataset.state.repository import StateRepository

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    loaded = load_settings()
    loaded.paths.data_dir = tmp_path / "data"
    return loaded


@pytest.fixture
def repository(settings: Settings) -> StateRepository:
    return StateRepository(connect(settings.paths.state_db))


def make_norm(number: str = "9249", kind: NormKind = NormKind.LEI, year: int = 1995) -> Norm:
    signed_on = dt.date(year, 12, 26)
    return Norm(
        norm_id=build_urn(FEDERAL, kind, signed_on, number),
        kind=kind,
        number=number,
        signed_on=signed_on,
        jurisdiction=FEDERAL,
        title=f"Lei nº {number}",
        summary="Ementa.",
        catalog_source="senado",
        catalog_ref="551125",
    )
