"""Per-host request pacing, adaptive slow-down and circuit breaking.

Each host gets its own :class:`HostLimiter`, so different hosts are crawled in parallel
while every single host only sees a small, steady request rate.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from legis_dataset.config import CircuitBreakerConfig, HostPolicy
from legis_dataset.http.errors import HostHaltedError

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Uniform = Callable[[float, float], float]

# After a throttling signal the rate is halved, never going below this fraction of the
# configured rate; each success then recovers this fraction of the configured rate.
MIN_RATE_FRACTION = 0.05
RECOVERY_FRACTION = 0.02


class HostState(StrEnum):
    OK = "ok"
    COOLING_DOWN = "cooling_down"  # circuit breaker open
    HALTED = "halted"  # blocked by the host; no more requests this run


@dataclass(frozen=True)
class HostSnapshot:
    host: str
    state: HostState
    current_rate: float
    configured_rate: float
    in_flight: int
    max_concurrency: int


class HostLimiter:
    def __init__(
        self,
        host: str,
        policy: HostPolicy,
        breaker: CircuitBreakerConfig,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        uniform: Uniform = random.uniform,
    ) -> None:
        self.host = host
        self._policy = policy
        self._breaker = breaker
        self._clock = clock
        self._sleep = sleep
        self._uniform = uniform
        self._concurrency = asyncio.Semaphore(policy.max_concurrency)
        self._pacing_lock = asyncio.Lock()
        self._current_rate = policy.rate_per_second
        self._next_slot = 0.0
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._halted_reason: str | None = None
        self._in_flight = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Wait for permission to send one request to this host."""
        self._raise_if_halted()
        async with self._concurrency:
            await self._wait_for_turn()
            self._raise_if_halted()
            self._in_flight += 1
            try:
                yield
            finally:
                self._in_flight -= 1

    async def _wait_for_turn(self) -> None:
        async with self._pacing_lock:
            while True:
                now = self._clock()
                ready_at = max(self._next_slot, self._open_until)
                if now >= ready_at:
                    break
                await self._sleep(ready_at - now)
            self._next_slot = self._clock() + self._next_gap()

    def _next_gap(self) -> float:
        """Seconds until the next request: the average gap, randomized by ``jitter``."""
        average = 1.0 / self._current_rate
        jitter = self._policy.jitter
        return average * self._uniform(1.0 - jitter, 1.0 + jitter)

    def _raise_if_halted(self) -> None:
        if self._halted_reason is not None:
            raise HostHaltedError(self.host, f"host halted: {self._halted_reason}")

    # ------------------------------------------------------------- feedback

    def record_success(self) -> None:
        self._consecutive_failures = 0
        configured = self._policy.rate_per_second
        self._current_rate = min(configured, self._current_rate + configured * RECOVERY_FRACTION)

    def record_throttled(self, retry_after: float | None = None) -> None:
        """The host asked us to slow down (429/503) or is struggling (5xx/timeouts)."""
        configured = self._policy.rate_per_second
        self._current_rate = max(configured * MIN_RATE_FRACTION, self._current_rate / 2)
        if retry_after:
            self._next_slot = max(self._next_slot, self._clock() + retry_after)
        self._record_failure()

    def record_failure(self) -> None:
        self._record_failure()

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker.failure_threshold:
            self._open_until = self._clock() + self._breaker.cooldown_seconds
            # After the cooldown a single success closes the breaker again; another failure
            # re-opens it immediately.
            self._consecutive_failures = self._breaker.failure_threshold - 1

    def halt(self, reason: str) -> None:
        self._halted_reason = reason

    # ------------------------------------------------------------- reporting

    @property
    def state(self) -> HostState:
        if self._halted_reason is not None:
            return HostState.HALTED
        if self._clock() < self._open_until:
            return HostState.COOLING_DOWN
        return HostState.OK

    def snapshot(self) -> HostSnapshot:
        return HostSnapshot(
            host=self.host,
            state=self.state,
            current_rate=self._current_rate,
            configured_rate=self._policy.rate_per_second,
            in_flight=self._in_flight,
            max_concurrency=self._policy.max_concurrency,
        )


class HostLimiterRegistry:
    """Creates one limiter per host on first use."""

    def __init__(
        self,
        policy_for: Callable[[str], HostPolicy],
        breaker: CircuitBreakerConfig,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._policy_for = policy_for
        self._breaker = breaker
        self._clock = clock
        self._sleep = sleep
        self._limiters: dict[str, HostLimiter] = {}

    def for_host(self, host: str) -> HostLimiter:
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = HostLimiter(
                host, self._policy_for(host), self._breaker, clock=self._clock, sleep=self._sleep
            )
            self._limiters[host] = limiter
        return limiter

    def snapshots(self) -> list[HostSnapshot]:
        return [limiter.snapshot() for limiter in self._limiters.values()]

    def total_concurrency(self, hosts: list[str]) -> int:
        return sum(self._policy_for(host).max_concurrency for host in hosts)
