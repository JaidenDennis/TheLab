"""The seams between the engine and the outside world.

Everything here is about what has to be true before a live feed or a live
executor can be wired in: that teardown happens even when the feed dies, that
the feed and clock are chosen at wiring time rather than hardcoded, and that a
strategy asking for data the config does not produce is rejected loudly rather
than silently never firing.
"""

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from nq_agent.clock import SimClock
from nq_agent.config import load_settings
from nq_agent.feed.base import DataFeed
from nq_agent.feed.replay import ReplayFeed
from nq_agent.main import FeedBinding, live_binding, replay_binding, run_from_config
from nq_agent.models import Bar, SessionState, Signal
from nq_agent.state import StateStore
from nq_agent.strategy.base import Strategy

FIXTURE = Path("tests/fixtures/2026-07-15.jsonl")
SESSION = date(2026, 7, 15)
START = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)


def config_for(tmp_path: Path, *, timeframes: str = "[1m, 5m]") -> Path:
    config = tmp_path / "test.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        f"timeframes: {timeframes}\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )
    return config


def journal_events(tmp_path: Path, session_date: date = SESSION) -> list[dict[str, Any]]:
    path = tmp_path / "journal" / f"{session_date.isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


class QuietStrategy(Strategy):
    """Never trades. Keeps these tests about wiring, not about signals."""

    name = "quiet"
    required_timeframes = ["1m"]

    def on_bar(self, bar: Bar, context: Any) -> Signal | None:
        return None

    def on_session_start(self, session_date: date) -> None:
        return None

    def on_session_end(self, session_date: date) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return {}

    def restore_state(self, state: dict[str, Any]) -> None:
        return None


class GreedyStrategy(QuietStrategy):
    """Asks for a timeframe the test config does not produce."""

    name = "greedy"
    required_timeframes = ["15m"]


class ExplodingFeed(DataFeed):
    """Yields real bars, then fails the way a dropped connection does."""

    def __init__(self, fixture: Path, symbol: str, clock: SimClock, fail_after: int) -> None:
        self._inner = ReplayFeed(fixture, symbol, clock=clock)
        self._fail_after = fail_after
        self.closed = 0

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        return await self._inner.get_bars(symbol, timeframe, start, end)

    async def stream(
        self, symbol: str, timeframes: list[str], resume_from: datetime | None = None
    ) -> AsyncIterator[Bar]:
        emitted = 0
        async for bar in self._inner.stream(symbol, timeframes, resume_from):
            if emitted >= self._fail_after:
                raise ConnectionError("feed dropped")
            emitted += 1
            yield bar

    async def close(self) -> None:
        self.closed += 1


def exploding_binding(fail_after: int) -> tuple[FeedBinding, ExplodingFeed]:
    clock = SimClock(START)
    feed = ExplodingFeed(FIXTURE, "NQ", clock, fail_after=fail_after)
    return FeedBinding(feed=feed, clock=clock, anchor=START), feed


async def test_a_feed_that_dies_mid_stream_still_closes_the_feed(tmp_path: Path) -> None:
    """Teardown outside a finally means a feed error skips feed.close()
    entirely, leaking whatever the feed held open."""
    binding, feed = exploding_binding(fail_after=20)

    with pytest.raises(ConnectionError):
        await run_from_config(
            config_for(tmp_path),
            None,
            "stub",
            None,
            strategy_override=QuietStrategy(),
            binding=binding,
        )

    assert feed.closed == 1


async def test_a_feed_that_dies_mid_stream_still_ends_the_session(tmp_path: Path) -> None:
    binding, _ = exploding_binding(fail_after=20)

    with pytest.raises(ConnectionError):
        await run_from_config(
            config_for(tmp_path),
            None,
            "stub",
            None,
            strategy_override=QuietStrategy(),
            binding=binding,
        )

    events = [record["event"] for record in journal_events(tmp_path)]
    assert "session_end" in events


async def test_a_feed_that_dies_mid_stream_journals_feed_error(tmp_path: Path) -> None:
    """The design names a feed_error event. Nothing wrote one, so the single
    most likely live failure left no trace in the debugging record."""
    binding, _ = exploding_binding(fail_after=20)

    with pytest.raises(ConnectionError):
        await run_from_config(
            config_for(tmp_path),
            None,
            "stub",
            None,
            strategy_override=QuietStrategy(),
            binding=binding,
        )

    errors = [r for r in journal_events(tmp_path) if r["event"] == "feed_error"]
    assert len(errors) == 1
    assert "ConnectionError" in str(errors[0]["error"])


async def test_a_feed_error_reaches_the_caller(tmp_path: Path) -> None:
    """Journalling the error must not swallow it. A run that died has not
    succeeded, and the supervisor that restarts the process needs to know."""
    binding, _ = exploding_binding(fail_after=20)

    with pytest.raises(ConnectionError, match="feed dropped"):
        await run_from_config(
            config_for(tmp_path),
            None,
            "stub",
            None,
            strategy_override=QuietStrategy(),
            binding=binding,
        )


async def test_teardown_closes_the_executors(tmp_path: Path) -> None:
    """A real executor holds a network session. Nothing closed it."""
    engine = await run_from_config(
        config_for(tmp_path), FIXTURE, "stub", None, strategy_override=QuietStrategy()
    )

    assert engine.router.executors, "no executors were wired"
    assert all(e.closed for e in engine.router.executors)


async def test_a_custom_binding_replaces_the_replay_feed(tmp_path: Path) -> None:
    """The point of the seam: wiring a different feed must not mean editing
    run_from_config."""
    binding, feed = exploding_binding(fail_after=10_000)

    engine = await run_from_config(
        config_for(tmp_path),
        None,
        "stub",
        None,
        strategy_override=QuietStrategy(),
        binding=binding,
    )

    assert engine.feed is feed
    assert feed.closed == 1


def test_replay_binding_anchors_on_the_fixtures_first_tick() -> None:
    binding = replay_binding(FIXTURE, "NQ")

    assert binding.anchor == START
    assert isinstance(binding.clock, SimClock)
    assert binding.backfill_until is None, (
        "a replay restart is a deterministic continuation, not a catch-up: "
        "warmup window 2 must stay empty for it"
    )


def test_live_binding_without_credentials_says_which_key_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the env var, not failing with an AttributeError somewhere deep
    in the SDK on the first bar of a live session."""
    monkeypatch.delenv("NQ_DATABENTO_API_KEY", raising=False)
    settings = load_settings(config_for(tmp_path))

    with pytest.raises(ValueError, match="NQ_DATABENTO_API_KEY"):
        live_binding(settings)


def test_live_binding_builds_a_reconnecting_feed_on_a_real_clock(tmp_path: Path) -> None:
    """The live path must not hand the engine a bare provider feed: a single
    dropped connection would then end the session."""
    from nq_agent.clock import RealClock
    from nq_agent.feed.reconnecting import ReconnectingFeed

    settings = load_settings(config_for(tmp_path)).model_copy(
        update={"databento_api_key": "db-test-key"}
    )

    binding = live_binding(settings)

    assert isinstance(binding.feed, ReconnectingFeed)
    assert isinstance(binding.clock, RealClock)
    assert binding.backfill_until == binding.anchor, (
        "the gap between the last persisted bar and now is warmup window 2; "
        "without backfill_until it would be dispatched as live"
    )


async def test_running_without_a_replay_path_or_binding_needs_live_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NQ_DATABENTO_API_KEY", raising=False)

    with pytest.raises(ValueError, match="NQ_DATABENTO_API_KEY"):
        await run_from_config(config_for(tmp_path), None, "stub", None)


async def test_the_anchor_decides_which_session_state_is_resumed(tmp_path: Path) -> None:
    """State lookup used to key off the replay fixture's first tick, which is
    exactly why a live feed could not resume -- its 'first tick' is whenever
    the process happened to start. Same fixture, same bars, two anchors: only
    the one matching the persisted session resumes it."""
    config = config_for(tmp_path)
    settings = load_settings(config)
    store = StateStore(settings.state_db_path)
    await store.init_schema()
    await store.save(SessionState(session_date=SESSION, trades_taken=5))

    clock = SimClock(START)
    matching = await run_from_config(
        config,
        None,
        "stub",
        0,
        strategy_override=QuietStrategy(),
        binding=FeedBinding(
            feed=ReplayFeed(FIXTURE, "NQ", clock=clock), clock=clock, anchor=START
        ),
    )

    assert matching.trades_taken == 5, "the persisted session was not resumed"


async def test_an_anchor_on_another_day_does_not_resume_this_session(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)
    settings = load_settings(config)
    store = StateStore(settings.state_db_path)
    await store.init_schema()
    await store.save(SessionState(session_date=SESSION, trades_taken=5))

    clock = SimClock(START)
    other = await run_from_config(
        config,
        None,
        "stub",
        0,
        strategy_override=QuietStrategy(),
        binding=FeedBinding(
            feed=ReplayFeed(FIXTURE, "NQ", clock=clock),
            clock=clock,
            anchor=START + timedelta(days=1),
        ),
    )

    assert other.trades_taken == 0


async def test_an_explicit_binding_outranks_a_replay_path(tmp_path: Path) -> None:
    """Both supplied: the binding decides, including its anchor. Re-deriving
    the anchor from the fixture whenever a replay path happens to be present
    would quietly ignore the caller's binding -- and is invisible in every
    other test here, because they pass one or the other and never both."""
    config = config_for(tmp_path)
    settings = load_settings(config)
    store = StateStore(settings.state_db_path)
    await store.init_schema()
    await store.save(SessionState(session_date=SESSION, trades_taken=5))

    clock = SimClock(START)
    engine = await run_from_config(
        config,
        FIXTURE,
        "stub",
        0,
        strategy_override=QuietStrategy(),
        binding=FeedBinding(
            feed=ReplayFeed(FIXTURE, "NQ", clock=clock),
            clock=clock,
            anchor=START + timedelta(days=1),
        ),
    )

    assert engine.trades_taken == 0, (
        "the fixture's first tick was used as the anchor instead of the "
        "binding's, so the 2026-07-15 session was resumed against a binding "
        "anchored on 2026-07-16"
    )


async def test_a_strategy_requiring_an_unproduced_timeframe_is_rejected(
    tmp_path: Path,
) -> None:
    """required_timeframes is declarative and nothing checked it. Declare 15m
    against a [1m, 5m] config and the strategy simply never fires -- a silent
    no-op indistinguishable from a strategy that found no setups."""
    config = config_for(tmp_path, timeframes="[1m, 5m]")

    with pytest.raises(ValueError, match="15m"):
        await run_from_config(
            config, FIXTURE, "stub", None, strategy_override=GreedyStrategy()
        )


async def test_a_strategy_requiring_a_produced_timeframe_runs(tmp_path: Path) -> None:
    config = config_for(tmp_path, timeframes="[1m, 5m]")

    engine = await run_from_config(
        config, FIXTURE, "stub", None, strategy_override=QuietStrategy()
    )

    assert engine.strategy.name == "quiet"


# --- refusing to trade real money without money limits ---


def live_config(tmp_path: Path, risk: str = "") -> Path:
    config = tmp_path / "live.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        + risk
        + "executors:\n"
        "  - name: broker\n"
        "    type: webhook\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )
    return config


def test_a_webhook_executor_without_loss_limits_is_refused(tmp_path: Path) -> None:
    """The limits are opt-in, so a live config that simply omits them would
    otherwise start with no daily loss limit and no drawdown limit at all --
    invisibly, until the prop firm's own limit closes the account."""
    from nq_agent.main import check_live_safety

    settings = load_settings(live_config(tmp_path))

    with pytest.raises(ValueError, match="max_daily_loss"):
        check_live_safety(settings)


def test_naming_only_one_limit_is_still_refused(tmp_path: Path) -> None:
    from nq_agent.main import check_live_safety

    settings = load_settings(live_config(tmp_path, "risk:\n  max_daily_loss: 400\n"))

    with pytest.raises(ValueError, match="max_trailing_drawdown"):
        check_live_safety(settings)


def test_both_limits_set_is_accepted(tmp_path: Path) -> None:
    from nq_agent.main import check_live_safety

    settings = load_settings(
        live_config(tmp_path, "risk:\n  max_daily_loss: 400\n  max_trailing_drawdown: 1500\n")
    )

    check_live_safety(settings)


async def test_a_run_is_refused_end_to_end_not_just_by_the_helper(
    tmp_path: Path,
) -> None:
    """The check being correct is worth nothing if run_from_config never calls
    it. Deleting the call leaves every direct test of check_live_safety green.

    QuietStrategy, not "stub": live_config declares timeframes [1m] and the
    stub requires 5m, so the timeframe check would fire first and this would
    pass on the wrong exception.
    """
    with pytest.raises(ValueError, match="max_daily_loss"):
        await run_from_config(
            live_config(tmp_path), FIXTURE, "stub", None, strategy_override=QuietStrategy()
        )


def test_a_dry_run_without_limits_is_fine(tmp_path: Path) -> None:
    """Only real orders trigger the requirement. A replay with no limits is
    not risking anything."""
    from nq_agent.main import check_live_safety

    check_live_safety(load_settings(config_for(tmp_path)))


def test_a_disabled_webhook_executor_does_not_trigger_the_requirement(
    tmp_path: Path,
) -> None:
    from nq_agent.main import check_live_safety

    config = tmp_path / "off.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m]\n"
        "executors:\n"
        "  - name: broker\n"
        "    type: webhook\n"
        "    enabled: false\n"
        "    accounts: [tradeify]\n"
    )

    check_live_safety(load_settings(config))
