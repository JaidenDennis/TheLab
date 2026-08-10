from datetime import time
from pathlib import Path

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
