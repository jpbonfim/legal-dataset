"""Async HTTP client that is polite to the host and classifies every failure."""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
import orjson

from legis_dataset.config import HttpConfig
from legis_dataset.http.errors import (
    BlockedError,
    HostHaltedError,
    HttpFetchError,
    NotFoundError,
    PermanentError,
    TransientError,
)
from legis_dataset.http.rate_limit import HostLimiter, HostLimiterRegistry

# Markers of anti-bot / challenge pages. Seeing one halts the host: we never work around it.
CHALLENGE_MARKERS = (
    b"verifica\xc3\xa7\xc3\xa3o de seguran\xc3\xa7a",  # "verificação de segurança" (UTF-8)
    b"/_challenge",
    b"g-recaptcha",
    b"hcaptcha",
    b"cf-challenge",
    b"captcha",
)
THROTTLE_STATUSES = {429, 503}
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
NOT_FOUND_STATUSES = {404, 410}


class HttpObserver(Protocol):
    """Receives events so the UI and logs can show what each host is doing."""

    def on_request(self, host: str, method: str, url: str) -> None: ...
    def on_response(self, host: str, status_code: int, elapsed: float) -> None: ...
    def on_retry(self, host: str, reason: str, wait_seconds: float) -> None: ...
    def on_failure(self, host: str, error: HttpFetchError) -> None: ...
    def watch_limiters(self, registry: HostLimiterRegistry) -> None: ...


class NullObserver:
    def on_request(self, host: str, method: str, url: str) -> None:
        pass

    def on_response(self, host: str, status_code: int, elapsed: float) -> None:
        pass

    def on_retry(self, host: str, reason: str, wait_seconds: float) -> None:
        pass

    def on_failure(self, host: str, error: HttpFetchError) -> None:
        pass

    def watch_limiters(self, registry: HostLimiterRegistry) -> None:
        pass


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    content: bytes
    headers: httpx.Headers

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


class HttpClient:
    """Wraps :class:`httpx.AsyncClient` with per-host limits and retries.

    Only transient failures are retried here; permanent ones and blocks are raised at once.
    """

    def __init__(
        self,
        config: HttpConfig,
        *,
        limiters: HostLimiterRegistry | None = None,
        observer: HttpObserver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self.limiters = limiters or HostLimiterRegistry(config.policy_for, config.circuit_breaker)
        self._observer: HttpObserver = observer or NullObserver()
        self._observer.watch_limiters(self.limiters)
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            headers={"User-Agent": config.full_user_agent, "Accept-Language": "pt-BR,pt;q=0.9"},
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ public API

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        conditional = dict(headers or {})
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        return await self._request("GET", url, params=params, headers=conditional)

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> dict[str, object] | list[object]:
        result = await self._request(
            "GET", url, params=params, headers={"Accept": "application/json"}
        )
        try:
            return orjson.loads(result.content)
        except orjson.JSONDecodeError as error:
            # Overloaded APIs sometimes answer 200 with an HTML error page.
            raise TransientError(url, f"response is not valid JSON: {error}") from error

    async def exists(self, url: str) -> bool:
        """HEAD request; ``True`` for 2xx, ``False`` for 404/410."""
        try:
            await self._request("HEAD", url)
        except NotFoundError:
            return False
        return True

    # -------------------------------------------------------------- internals

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        host = urlsplit(url).hostname or ""
        limiter = self.limiters.for_host(host)
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._attempt(limiter, method, url, params, headers)
            except TransientError as error:
                if attempt >= self._config.max_attempts:
                    self._observer.on_failure(host, error)
                    raise
                wait = self._backoff(attempt, error.retry_after)
                self._observer.on_retry(host, str(error), wait)
                await self._sleep(wait)
            except BlockedError as error:
                limiter.halt(str(error))
                self._observer.on_failure(host, error)
                raise
            except (HostHaltedError, NotFoundError):
                # Not failures: a halted host was already reported, and 404s are expected
                # answers (e.g. probing candidate URLs); both show up in the counters.
                raise
            except HttpFetchError as error:
                self._observer.on_failure(host, error)
                raise

    async def _attempt(
        self,
        limiter: HostLimiter,
        method: str,
        url: str,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
    ) -> FetchResult:
        async with limiter.slot():
            self._observer.on_request(limiter.host, method, url)
            started = time.monotonic()
            try:
                response = await self._client.request(method, url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                limiter.record_throttled()
                raise TransientError(url, f"{type(error).__name__}: {error}") from error
            except httpx.HTTPError as error:
                limiter.record_failure()
                raise PermanentError(url, f"{type(error).__name__}: {error}") from error
            self._observer.on_response(
                limiter.host, response.status_code, time.monotonic() - started
            )
            return self._classify(limiter, url, response)

    def _classify(self, limiter: HostLimiter, url: str, response: httpx.Response) -> FetchResult:
        status = response.status_code
        result = FetchResult(str(response.url), status, response.content, response.headers)
        if _looks_like_challenge(response):
            raise BlockedError(url, f"anti-bot challenge page (HTTP {status})")
        if status < 400:
            limiter.record_success()
            return result
        if status in RETRYABLE_STATUSES:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if status in THROTTLE_STATUSES or status >= 500:
                limiter.record_throttled(retry_after)
            raise TransientError(url, f"HTTP {status}", retry_after=retry_after)
        # A definitive answer from a healthy host: does not count against the breaker.
        limiter.record_success()
        if status in NOT_FOUND_STATUSES:
            raise NotFoundError(url, f"HTTP {status}", status_code=status)
        raise PermanentError(url, f"HTTP {status}", status_code=status)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        exponential = self._config.backoff_base_seconds * 2 ** (attempt - 1)
        wait = min(self._config.backoff_max_seconds, exponential)
        wait = random.uniform(wait / 2, wait)  # full jitter keeps workers from syncing up
        if retry_after is not None:
            wait = max(wait, min(retry_after, self._config.backoff_max_seconds * 5))
        return wait


def _looks_like_challenge(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        return False
    # Challenge pages are small; legislation pages can be megabytes and may legitimately
    # mention words like "captcha" deep in their text.
    head = response.content[:20_000].lower()
    if len(response.content) > 200_000:
        return False
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return None
        return max(0.0, parsed.timestamp() - time.time())
