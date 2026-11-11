"""Pipeline configuration loaded from a TOML file."""

from __future__ import annotations

import datetime as dt
import tomllib
from functools import cached_property
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from legis_dataset.models import NormKind

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"


class PathsConfig(BaseModel):
    data_dir: Path = Path("data")

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def parsed_dir(self) -> Path:
        return self.data_dir / "interim" / "parsed"

    @property
    def clean_dir(self) -> Path:
        return self.data_dir / "interim" / "clean"

    @property
    def final_dir(self) -> Path:
        return self.data_dir / "final"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


class CatalogConfig(BaseModel):
    norm_kinds: list[NormKind]
    first_year: int = 1889
    last_year: int = 0

    @property
    def effective_last_year(self) -> int:
        return self.last_year or dt.date.today().year


class SourcesConfig(BaseModel):
    catalog: str = "senado"
    locators: list[str] = Field(default_factory=lambda: ["normas_leg", "senado_status", "planalto"])


class HostPolicy(BaseModel):
    #: Average request rate. The actual gap between two requests is randomized around it.
    rate_per_second: float = Field(gt=0)
    max_concurrency: int = Field(ge=1)
    #: How much each gap varies: 0.5 spreads gaps uniformly over 50%–150% of the average.
    #: Evenly spaced requests are a typical bot signature; the average rate is unchanged.
    jitter: float = Field(default=0.5, ge=0, lt=1)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    cooldown_seconds: float = 300


class HttpConfig(BaseModel):
    user_agent: str
    contact: str = ""
    timeout_seconds: float = 60
    max_attempts: int = 5
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 120.0
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    default_host: HostPolicy = HostPolicy(rate_per_second=1.0, max_concurrency=1)
    hosts: dict[str, HostPolicy] = Field(default_factory=dict)

    @property
    def full_user_agent(self) -> str:
        return f"{self.user_agent} {self.contact}".strip()

    def policy_for(self, host: str) -> HostPolicy:
        return self.hosts.get(host, self.default_host)


class ProcessingConfig(BaseModel):
    parse_workers: int = 0
    min_chars: int = 200
    near_duplicate_threshold: float = Field(default=0.9, gt=0, le=1)
    minhash_permutations: int = 128


class ExportConfig(BaseModel):
    max_rows_per_shard: int = 20000
    compression: str = "zstd"


class StatsConfig(BaseModel):
    tokenizer: str = "Qwen/Qwen2.5-7B"


class Settings(BaseModel):
    paths: PathsConfig = PathsConfig()
    catalog: CatalogConfig
    subsets: dict[str, list[NormKind]]
    sources: SourcesConfig = SourcesConfig()
    http: HttpConfig
    processing: ProcessingConfig = ProcessingConfig()
    export: ExportConfig = ExportConfig()
    stats: StatsConfig = StatsConfig()

    @model_validator(mode="after")
    def _every_kind_has_a_subset(self) -> Settings:
        assigned = [kind for kinds in self.subsets.values() for kind in kinds]
        duplicated = {kind for kind in assigned if assigned.count(kind) > 1}
        if duplicated:
            raise ValueError(f"norm kinds assigned to more than one subset: {sorted(duplicated)}")
        missing = set(self.catalog.norm_kinds) - set(assigned)
        if missing:
            raise ValueError(f"norm kinds without a subset: {sorted(missing)}")
        return self

    @cached_property
    def subset_by_kind(self) -> dict[NormKind, str]:
        return {kind: subset for subset, kinds in self.subsets.items() for kind in kinds}


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (defaults to ``config/default.toml``)."""
    config_path = path or DEFAULT_CONFIG_PATH
    with config_path.open("rb") as file:
        raw = tomllib.load(file)
    return Settings.model_validate(raw)
