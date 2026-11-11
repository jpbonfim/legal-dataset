from __future__ import annotations

import httpx
import pytest
import respx

from legis_dataset.config import CircuitBreakerConfig, HostPolicy, HttpConfig
from legis_dataset.http.client import HttpClient
from legis_dataset.http.errors import (
    BlockedError,
    HostHaltedError,
    NotFoundError,
    TransientError,
)
from legis_dataset.http.rate_limit import HostLimiter, HostState

URL = "https://example.gov.br/page"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _config(**overrides: object) -> HttpConfig:
    values: dict[str, object] = {
        "user_agent": "test",
        "max_attempts": 3,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.02,
        "default_host": HostPolicy(rate_per_second=1000, max_concurrency=4),
    }
    return HttpConfig.model_validate(values | overrides)


async def _no_sleep(_: float) -> None:
    return None


# ------------------------------------------------------------- rate limiter


async def test_limiter_spaces_requests_by_configured_rate() -> None:
    clock = FakeClock()
    limiter = HostLimiter(
        "h",
        HostPolicy(rate_per_second=2, max_concurrency=1),
        CircuitBreakerConfig(),
        clock=clock,
        sleep=clock.sleep,
    )
    for _ in range(3):
        async with limiter.slot():
            pass
    assert clock.now == pytest.approx(1.0)  # 3 requests at 2 req/s -> 0.5 s gaps


async def test_limiter_halves_rate_when_throttled_and_recovers() -> None:
    limiter = HostLimiter(
        "h",
        HostPolicy(rate_per_second=2, max_concurrency=1),
        CircuitBreakerConfig(failure_threshold=100),
    )
    limiter.record_throttled()
    assert limiter.snapshot().current_rate == pytest.approx(1.0)
    for _ in range(100):
        limiter.record_success()
    assert limiter.snapshot().current_rate == pytest.approx(2.0)


async def test_circuit_breaker_opens_after_consecutive_failures() -> None:
    clock = FakeClock()
    limiter = HostLimiter(
        "h",
        HostPolicy(rate_per_second=100, max_concurrency=1),
        CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=60),
        clock=clock,
        sleep=clock.sleep,
    )
    limiter.record_failure()
    limiter.record_failure()
    assert limiter.state is HostState.COOLING_DOWN
    async with limiter.slot():
        pass
    assert clock.now >= 60  # the next request waited for the cooldown


# --------------------------------------------------------------- HTTP client


@respx.mock
async def test_retries_transient_errors_then_succeeds() -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    async with HttpClient(_config(), sleep=_no_sleep) as client:
        result = await client.get(URL)
    assert result.content == b"ok"
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_attempts() -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    async with HttpClient(_config(), sleep=_no_sleep) as client:
        with pytest.raises(TransientError):
            await client.get(URL)


@respx.mock
async def test_not_found_is_permanent_and_not_retried() -> None:
    route = respx.route(url=URL).mock(return_value=httpx.Response(404))
    async with HttpClient(_config(), sleep=_no_sleep) as client:
        with pytest.raises(NotFoundError):
            await client.get(URL)
    assert route.call_count == 1


@respx.mock
async def test_challenge_page_halts_the_host() -> None:
    challenge = "<html><title>Verificação de segurança</title><form action='/_challenge'>"
    route = respx.get(URL).mock(return_value=httpx.Response(200, html=challenge))
    async with HttpClient(_config(), sleep=_no_sleep) as client:
        with pytest.raises(BlockedError):
            await client.get(URL)
        with pytest.raises(HostHaltedError):
            await client.get(URL)
    assert route.call_count == 1  # no further requests to a halted host


@respx.mock
async def test_invalid_json_is_transient() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, html="<html>erro</html>"))
    async with HttpClient(_config(max_attempts=1), sleep=_no_sleep) as client:
        with pytest.raises(TransientError):
            await client.get_json(URL)


@respx.mock
async def test_conditional_get_sends_validators() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304))
    async with HttpClient(_config(), sleep=_no_sleep) as client:
        result = await client.get(URL, etag='"abc"')
    assert result.not_modified
    assert route.calls.last.request.headers["If-None-Match"] == '"abc"'
