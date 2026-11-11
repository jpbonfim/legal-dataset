"""Contracts every data source implements, and the registry that builds them by name.

A *catalog source* enumerates norms. A *locator* tells, for one norm, whether it is in
force and/or where its text can be downloaded. New jurisdictions (state or municipal
legislation) plug in by registering new catalog sources and locators; the pipeline stages
only depend on these protocols.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Flag, auto
from typing import Protocol

from legis_dataset.config import Settings
from legis_dataset.http.client import HttpClient
from legis_dataset.models import Location, Norm, NormKind


@dataclass(frozen=True)
class CatalogPartition:
    """A unit of catalog discovery that is checkpointed on its own (e.g. one kind × year)."""

    kind: NormKind
    year: int

    @property
    def key(self) -> str:
        return f"{self.kind.value}/{self.year}"


class CatalogSource(Protocol):
    name: str
    #: Hosts this source talks to (shown in the progress panel).
    hosts: tuple[str, ...]

    def partitions(
        self, kinds: list[NormKind], first_year: int, last_year: int
    ) -> list[CatalogPartition]: ...

    async def list_partition(self, partition: CatalogPartition) -> list[Norm]: ...


class Capability(Flag):
    LEGAL_STATUS = auto()
    TEXT = auto()


class Locator(Protocol):
    name: str
    hosts: tuple[str, ...]
    #: What this locator can find out. The map stage only asks a locator for
    #: information that is still unknown.
    provides: Capability

    def supports(self, norm: Norm) -> bool: ...

    async def locate(self, norm: Norm) -> Location: ...


CatalogFactory = Callable[[HttpClient, Settings], CatalogSource]
LocatorFactory = Callable[[HttpClient, Settings], Locator]

_CATALOGS: dict[str, CatalogFactory] = {}
_LOCATORS: dict[str, LocatorFactory] = {}


def register_catalog(name: str) -> Callable[[CatalogFactory], CatalogFactory]:
    def decorator(factory: CatalogFactory) -> CatalogFactory:
        _CATALOGS[name] = factory
        return factory

    return decorator


def register_locator(name: str) -> Callable[[LocatorFactory], LocatorFactory]:
    def decorator(factory: LocatorFactory) -> LocatorFactory:
        _LOCATORS[name] = factory
        return factory

    return decorator


def build_catalog(name: str, client: HttpClient, settings: Settings) -> CatalogSource:
    _load_builtin_sources()
    try:
        return _CATALOGS[name](client, settings)
    except KeyError:
        raise ValueError(f"unknown catalog source {name!r}; known: {sorted(_CATALOGS)}") from None


def build_locators(names: list[str], client: HttpClient, settings: Settings) -> list[Locator]:
    _load_builtin_sources()
    unknown = [name for name in names if name not in _LOCATORS]
    if unknown:
        raise ValueError(f"unknown locators {unknown}; known: {sorted(_LOCATORS)}")
    return [_LOCATORS[name](client, settings) for name in names]


def _load_builtin_sources() -> None:
    # Importing the modules runs their @register_* decorators.
    from legis_dataset.sources import normas_leg, planalto, senado  # noqa: F401
