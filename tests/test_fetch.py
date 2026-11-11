"""Fetch locates and downloads in one pass, reusing what earlier runs already learned."""

from __future__ import annotations

import datetime as dt

import httpx
import respx
from conftest import make_norm

from legis_dataset.config import Settings
from legis_dataset.models import Location, NormKind, TextSource, TextVersion
from legis_dataset.pipeline.context import PipelineContext, RunOptions
from legis_dataset.pipeline.orchestrator import StageName, run_stages
from legis_dataset.state.db import connect
from legis_dataset.state.repository import Stage, StateRepository, Status

PLANALTO_URL = "https://www.planalto.gov.br/ccivil_03/leis/l9249.htm"
PAGE = b"<html><body>" + b"Art. 1o Texto da lei. " * 20 + b"</body></html>"


def _fast(settings: Settings) -> Settings:
    for policy in settings.http.hosts.values():
        policy.rate_per_second = 1000
    return settings


def _legacy_map_row(
    repository: StateRepository, norm_id: str, status: Status, reason: str | None
) -> None:
    """A per-norm row written by the former separate locate pass (stage ``map``)."""
    repository._db.execute(
        "INSERT INTO stage_status (norm_id, stage, status, reason, updated_at) "
        "VALUES (?, 'map', ?, ?, ?)",
        (norm_id, status, reason, dt.datetime.now(dt.UTC).isoformat()),
    )


def test_state_from_the_former_locate_pass_is_kept(
    settings: Settings, repository: StateRepository
) -> None:
    located, revoked, unlocated = (make_norm(n) for n in ("1", "2", "3"))
    repository.upsert_norms((norm, "leis") for norm in (located, revoked, unlocated))
    source = TextSource("planalto", PLANALTO_URL, TextVersion.ORIGINAL)
    repository.save_location(located.norm_id, Location(text_source=source))
    _legacy_map_row(repository, located.norm_id, Status.DONE, "planalto")
    _legacy_map_row(repository, revoked.norm_id, Status.SKIPPED, "not_in_force")
    _legacy_map_row(repository, unlocated.norm_id, Status.FAILED, "no_text_source")

    migrated = StateRepository(connect(settings.paths.state_db))

    counts = {(r["stage"], r["status"], r["reason"]) for r in migrated.status_counts()}
    assert counts == {("fetch", "skipped", "not_in_force")}
    pending = {item.norm.norm_id: item for item in migrated.pending_items(Stage.FETCH)}
    assert set(pending) == {located.norm_id, unlocated.norm_id}
    assert pending[located.norm_id].text_source == source


async def test_known_text_source_is_downloaded_without_locating_again(
    settings: Settings, repository: StateRepository
) -> None:
    norm = make_norm("9249")
    repository.upsert_norms([(norm, "leis")])
    source = TextSource("planalto", PLANALTO_URL, TextVersion.ORIGINAL)
    repository.save_location(norm.norm_id, Location(text_source=source))
    context = PipelineContext(_fast(settings), repository, RunOptions(kinds=[NormKind.LEI]))

    with respx.mock as router:  # any request other than the text page fails the test
        page = router.get(PLANALTO_URL).mock(return_value=httpx.Response(200, content=PAGE))
        assert await run_stages(context, [StageName.FETCH])

    assert page.call_count == 1
    counts = {(r["stage"], r["status"], r["reason"]) for r in repository.status_counts()}
    assert counts == {("fetch", "done", "planalto")}
