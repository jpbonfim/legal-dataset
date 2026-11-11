"""Error taxonomy used to decide between retrying, failing and stopping a host."""

from __future__ import annotations

from typing import ClassVar


class HttpFetchError(Exception):
    """Base class for every failure while talking to a remote source."""

    #: Short machine-readable label stored in the state database.
    reason: ClassVar[str] = "http_error"

    def __init__(self, url: str, message: str) -> None:
        super().__init__(f"{message} [{url}]")
        self.url: str = url


class TransientError(HttpFetchError):
    """Timeouts, connection problems, 5xx, 429. Worth retrying later."""

    reason: ClassVar[str] = "transient"

    def __init__(self, url: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(url, message)
        self.retry_after: float | None = retry_after


class PermanentError(HttpFetchError):
    """The request will not succeed by repeating it (e.g. 404, 410, malformed payload)."""

    reason: ClassVar[str] = "permanent"

    def __init__(self, url: str, message: str, status_code: int | None = None) -> None:
        super().__init__(url, message)
        self.status_code: int | None = status_code


class NotFoundError(PermanentError):
    reason: ClassVar[str] = "not_found"


class BlockedError(HttpFetchError):
    """The host answered with an anti-bot challenge or an access-denied page.

    We never try to get around it: the host is halted for the rest of the run.
    """

    reason: ClassVar[str] = "blocked"


class HostHaltedError(HttpFetchError):
    """Raised for requests to a host that was halted earlier in this run."""

    reason: ClassVar[str] = "host_halted"
