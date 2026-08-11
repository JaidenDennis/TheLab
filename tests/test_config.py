from datetime import time
from pathlib import Path

import pytest

from nq_agent.config import load_settings


def test_base_config_loads_defaults(tmp_path: Path) -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.symbol == "NQ"
    assert settings.timeframes == ["1m", "5m"]
    assert settings.session.cutoff == time(16, 30)
    assert settings.risk.max_trades_per_day == 2
    assert settings.router.partial_fan == "continue"


def test_environment_overlay_merges_over_base() -> None:
    settings = load_settings(Path("config/paper.yaml"))
    assert settings.symbol == "NQ"
    assert len(settings.executors) == 2
    assert settings.executors[0].type == "dryrun"
    assert settings.executors[0].accounts == ["tradeify", "mff", "fundednext"]


def test_null_kill_switch_path_resolves_under_data_dir() -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.risk.kill_switch_path == Path("var/nq-agent.halt")


def test_derived_paths_hang_off_data_dir() -> None:
    settings = load_settings(Path("config/base.yaml"))
    assert settings.journal_dir == Path("var/journal")
    assert settings.state_db_path == Path("var/state.db")


# --- Required addition: Minor 2 -- a misspelled key must not silently fall
# back to its field's default. Settings uses extra="ignore" so
# pydantic-settings can layer environment variables over YAML (flipping it
# would break that, see load_settings's docstring); these pin that the
# separate, explicit key walk in load_settings still catches a YAML typo
# extra="ignore" would otherwise swallow with no error at all -- exactly
# the "risk limit silently reverts to its default" scenario measured
# against max_trades_per_day.


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("data_dir: ./var\nsymbool: NQ\n")
    with pytest.raises(ValueError, match=r"symbool.*<top level>"):
        load_settings(config)


def test_unknown_nested_key_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("risk:\n  max_trades_perday: 1\n")
    with pytest.raises(ValueError, match=r"max_trades_perday.*'risk'"):
        load_settings(config)


def test_unknown_key_in_an_executor_list_item_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enalbed: true\n"
    )
    with pytest.raises(ValueError, match=r"enalbed.*executors\[\]"):
        load_settings(config)


def test_a_correctly_spelled_risk_limit_still_loads(tmp_path: Path) -> None:
    """Guards against a validator that is simply too strict."""
    config = tmp_path / "good.yaml"
    config.write_text("risk:\n  max_trades_per_day: 5\n")
    settings = load_settings(config)
    assert settings.risk.max_trades_per_day == 5
