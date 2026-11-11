"""Interrupting a stage and running it again must neither redo nor lose work."""

from __future__ import annotations

from conftest import make_norm

from legis_dataset.http.errors import HostHaltedError, TransientError
from legis_dataset.pipeline.runner import (
    NullReporter,
    Outcome,
    ShutdownController,
    run_concurrently,
)
from legis_dataset.state.repository import Stage, StateRepository, Status, WorkItem


def _seed(repository: StateRepository, count: int) -> None:
    repository.upsert_norms((make_norm(str(1000 + i)), "leis") for i in range(count))


async def _run(repository: StateRepository, worker, shutdown: ShutdownController) -> None:
    def record(item: WorkItem, outcome: Outcome) -> None:
        repository.mark(item.norm.norm_id, Stage.FETCH, outcome.status, reason=outcome.reason)

    await run_concurrently(
        "fetch",
        repository.pending_items(Stage.FETCH),
        worker,
        record,
        concurrency=1,
        shutdown=shutdown,
        reporter=NullReporter(),
    )


async def test_interrupted_stage_resumes_where_it_stopped(repository: StateRepository) -> None:
    _seed(repository, 10)
    processed: list[str] = []
    shutdown = ShutdownController()

    async def worker(item: WorkItem) -> Outcome:
        processed.append(item.norm.norm_id)
        if len(processed) == 4:
            shutdown.request()  # like Ctrl+C while the 4th item is in flight
        return Outcome(Status.DONE)

    await _run(repository, worker, shutdown)
    assert len(processed) == 4  # the in-flight item finished, nothing new started
    assert len(repository.pending_items(Stage.FETCH)) == 6

    await _run(repository, worker, ShutdownController())
    assert len(processed) == 10
    assert len(set(processed)) == 10  # no item was processed twice
    assert repository.pending_items(Stage.FETCH) == []


async def test_failures_and_halted_hosts(repository: StateRepository) -> None:
    _seed(repository, 3)
    numbers = iter(range(3))

    async def worker(item: WorkItem) -> Outcome:
        index = next(numbers)
        if index == 0:
            raise TransientError("u", "timeout")
        if index == 1:
            raise HostHaltedError("u", "halted")
        raise RuntimeError("bug")  # unexpected errors are contained too

    await _run(repository, worker, ShutdownController())
    counts = {(row["status"], row["reason"]): row["total"] for row in repository.status_counts()}
    assert counts == {("failed", "transient"): 1, ("failed", "unexpected"): 1}
    # Transient failures and never-attempted (halted) items are retried automatically;
    # unexpected ones only after `retry-failed`.
    assert len(repository.pending_items(Stage.FETCH)) == 2
    assert repository.reopen_failed(Stage.FETCH) == 2
    assert len(repository.pending_items(Stage.FETCH)) == 3
