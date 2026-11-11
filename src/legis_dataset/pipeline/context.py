"""Objects shared by all stages of one pipeline execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from legis_dataset.config import Settings
from legis_dataset.http.client import HttpClient, HttpObserver, NullObserver
from legis_dataset.models import NormKind
from legis_dataset.pipeline.runner import NullReporter, ShutdownController, StageReporter
from legis_dataset.state.repository import StateRepository


@dataclass
class RunOptions:
    """Command-line restrictions applied to one execution."""

    kinds: list[NormKind] | None = None
    limit: int | None = None

    def effective_kinds(self, settings: Settings) -> list[NormKind]:
        return self.kinds or list(settings.catalog.norm_kinds)


@dataclass
class PipelineContext:
    settings: Settings
    repository: StateRepository
    options: RunOptions = field(default_factory=RunOptions)
    reporter: StageReporter = field(default_factory=NullReporter)
    http_observer: HttpObserver = field(default_factory=NullObserver)
    shutdown: ShutdownController = field(default_factory=ShutdownController)

    def new_http_client(self) -> HttpClient:
        return HttpClient(self.settings.http, observer=self.http_observer)
