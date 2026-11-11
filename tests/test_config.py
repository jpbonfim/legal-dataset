from __future__ import annotations

from pathlib import Path

from legis_dataset.config import load_settings, merge_settings


def test_custom_file_only_overrides_the_values_it_sets(tmp_path: Path) -> None:
    custom = tmp_path / "custom.toml"
    custom.write_text(
        '[paths]\ndata_dir = "data/sample"\n\n'
        '[http.hosts."www.planalto.gov.br"]\nrate_per_second = 0.5\n'
    )
    defaults = load_settings()
    settings = load_settings(custom)

    assert settings.paths.data_dir == Path("data/sample")
    planalto = settings.http.policy_for("www.planalto.gov.br")
    assert planalto.rate_per_second == 0.5
    assert (
        planalto.max_concurrency == defaults.http.policy_for("www.planalto.gov.br").max_concurrency
    )
    assert settings.http.hosts.keys() == defaults.http.hosts.keys()
    assert settings.catalog == defaults.catalog


def test_lists_are_replaced_not_concatenated() -> None:
    merged = merge_settings(
        {"catalog": {"norm_kinds": ["lei", "decreto"]}}, {"catalog": {"norm_kinds": ["lei"]}}
    )
    assert merged == {"catalog": {"norm_kinds": ["lei"]}}
