"""A replay killed mid-way through a multi-day fixture resumes the session it
was in, not the fixture's first.

The anchor of a replay binding is the fixture's first tick, and the anchor
used to name the session whose state to load. On a one-day fixture those are
the same session, which is why every existing resume test passed. On a
multi-day fixture they are not: a run killed in day two would load day one's
completed state, replay day two from the top, and re-fire entries that had
already been dispatched -- the exact double-order a resume exists to prevent.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

from nq_agent.main import run_from_config

DAY_ONE = date(2026, 7, 15)
DAY_TWO = date(2026, 7, 16)


def two_day_fixture(tmp_path: Path) -> Path:
    """Two short sessions. Day one's trade closes at its target; day two's
    entry is still open when the test kills the run, and a pair of ticks past
    the cutoff lets the session flatten close it cleanly on the resume."""

    def tick(day: str, hhmmss: str, price: str) -> str:
        return json.dumps({"ts": f"2026-07-{day}T{hhmmss}+00:00", "price": price, "size": 1})

    ticks = [
        # Day one: enter long at 20000 on the first bar, rally through the
        # +20 target, then drift flat so the day ends with no position.
        tick("15", "13:30:30", "20000.00"),
        tick("15", "13:31:30", "20000.00"),
        tick("15", "13:32:30", "20026.00"),  # target 20020 hit intra-bar
        tick("15", "13:33:30", "20010.00"),
        tick("15", "13:34:30", "20010.00"),
        tick("15", "13:35:30", "20010.00"),
        tick("15", "13:36:30", "20010.00"),
        # Day two: enter long at 20000 again, drift slightly down -- the
        # position is open when the first run is killed.
        tick("16", "13:30:30", "20000.00"),
        tick("16", "13:31:30", "20000.00"),
        tick("16", "13:32:30", "19998.00"),
        tick("16", "13:33:30", "19998.00"),
        tick("16", "13:34:30", "19998.00"),
        tick("16", "13:35:30", "19998.00"),
        # Past the cutoff, so the resumed run's session flatten has a bar
        # to fire on and the fixture ends with day two properly closed.
        tick("16", "20:30:30", "19998.00"),
        tick("16", "20:31:30", "19998.00"),
    ]
    fixture = tmp_path / "two-days.jsonl"
    fixture.write_text("".join(line + "\n" for line in ticks))
    return fixture


def config_for(tmp_path: Path) -> Path:
    config = tmp_path / "md.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )
    return config


def day_events(tmp_path: Path, session_date: date) -> list[dict[str, Any]]:
    path = tmp_path / "journal" / f"{session_date.isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines()]


def kinds(tmp_path: Path, session_date: date) -> list[str]:
    return [e["event"] for e in day_events(tmp_path, session_date)]


# Bar order in the first run: seven day-one 1m/5m bars, the two day-one
# buckets the first day-two tick rolls closed, then day two's own bars.
# Twelve bars puts the kill three bars into day two, entry dispatched,
# position open.
KILL_AT = 12


async def test_a_multiday_replay_resumes_the_session_it_was_killed_in(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)
    fixture = two_day_fixture(tmp_path)

    first = await run_from_config(config, fixture, "always", max_bars=KILL_AT)
    assert first.trades_taken == 1, "the kill point must land inside day two's open trade"
    opened = [e for e in day_events(tmp_path, DAY_TWO) if e["event"] == "position_opened"]
    assert opened, "day two's entry never happened before the kill"

    second = await run_from_config(config, fixture, "always", max_bars=None)

    # Day two was adopted, not restarted: adoption preserves the restored
    # strategy state, so the entry that already fired must not fire again.
    day_two = day_events(tmp_path, DAY_TWO)
    entries = [
        e for e in day_two if e["event"] == "signal_emitted" and e.get("intent") == "ENTRY"
    ]
    assert len(entries) == 1, "day two's entry fired twice -- the session was restarted"
    assert "session_resumed" in kinds(tmp_path, DAY_TWO)
    starts = [e for e in day_two if e["event"] == "session_start"]
    assert len(starts) == 1, "the resumed run started day two over"
    assert second.trades_taken == 1

    # Day one is wholly in the past: the resumed run must not have re-run,
    # re-adopted or re-ended it.
    day_one = kinds(tmp_path, DAY_ONE)
    assert day_one.count("session_start") == 1
    assert "session_resumed" not in day_one
    assert day_one.count("session_end") == 1

    # And the position the kill left open was closed by day two's cutoff.
    closes = [e for e in day_two if e["event"] == "position_closed"]
    assert [e["exit_reason"] for e in closes].count("FLATTEN") == 1


async def test_resuming_into_a_new_session_starts_fresh_day_counters(
    tmp_path: Path,
) -> None:
    """Each fixture after the first in a multi-fixture backtest resumes the
    previous session's state on a NEW day. Yesterday's trades_taken, halt
    flag and daily loss must not gate today: one halted day would silently
    poison every later day of the backtest."""

    def tick(day: str, hhmmss: str, price: str) -> str:
        return json.dumps({"ts": f"2026-07-{day}T{hhmmss}+00:00", "price": price, "size": 1})

    # Day one: enter long at 20000, decline through the $20 daily limit so
    # the risk flatten fires and halts the day, then close past the cutoff.
    day_one = tmp_path / "day1.jsonl"
    day_one.write_text(
        "\n".join(
            [
                tick("15", "13:30:30", "20000.00"),
                tick("15", "13:31:30", "20000.00"),
                tick("15", "13:32:30", "19998.00"),  # -$40, past the limit
                tick("15", "13:33:30", "19998.00"),
                tick("15", "20:30:30", "19998.00"),
                tick("15", "20:31:30", "19998.00"),
            ]
        )
        + "\n"
    )
    # Day two: a clean session with an entry on the first bar.
    day_two = tmp_path / "day2.jsonl"
    day_two.write_text(
        "\n".join(
            [
                tick("16", "13:30:30", "20000.00"),
                tick("16", "13:31:30", "20000.00"),
                tick("16", "13:32:30", "20001.00"),
                tick("16", "20:30:30", "20001.00"),
                tick("16", "20:31:30", "20001.00"),
            ]
        )
        + "\n"
    )
    config = tmp_path / "fresh.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "risk:\n"
        "  max_trades_per_day: 10\n"
        "  duplicate_window_seconds: 0\n"
        "  max_daily_loss: 20\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )

    first = await run_from_config(config, day_one, "always", None)
    assert first.is_halted, "day one must end halted for this test to mean anything"
    assert first.trades_taken == 1

    second = await run_from_config(config, day_two, "always", None)

    assert second.trades_taken == 1, "yesterday's trade count leaked into today"
    assert not second.is_halted, "yesterday's halt leaked into today"
    opened = [e for e in day_events(tmp_path, DAY_TWO) if e["event"] == "position_opened"]
    assert opened, "day two never traded -- yesterday's state gated it"


async def test_what_a_strategy_learns_at_session_end_is_persisted(tmp_path: Path) -> None:
    """Strategy.on_session_end banks cross-session statistics (SME's rolling
    daily range, its OFI distribution), and it runs after the last bar's
    persist. The teardown must persist again, or every multi-fixture backtest
    resumes each morning without the memory it built overnight -- this
    surfaced as a 20-session warmup that never completed across 947 real
    sessions."""
    from nq_agent.state import StateStore

    def tick(hhmmss: str, price: str) -> str:
        return json.dumps({"ts": f"2026-07-15T{hhmmss}+00:00", "price": price, "size": 1})

    fixture = tmp_path / "one-day.jsonl"
    fixture.write_text(
        "\n".join(
            [
                tick("13:30:30", "20000.00"),
                tick("13:31:30", "20010.00"),
                tick("13:32:30", "19990.00"),
                tick("20:30:30", "20000.00"),
                tick("20:31:30", "20000.00"),
            ]
        )
        + "\n"
    )
    config = tmp_path / "sme.yaml"
    config.write_text(
        f"data_dir: {tmp_path.as_posix()}\n"
        "timeframes: [1m, 5m]\n"
        "executors:\n"
        "  - name: dryrun_broker\n"
        "    type: dryrun\n"
        "    enabled: true\n"
        "    accounts: [tradeify]\n"
    )

    await run_from_config(config, fixture, "sme", None)

    state = await StateStore(tmp_path / "state.db").load_latest()
    assert state is not None
    assert state.strategy_state.get("daily_ranges"), (
        "the daily range banked at session end never reached the state store"
    )


async def test_a_completed_multiday_replay_rerun_replays_nothing(tmp_path: Path) -> None:
    """Running the fixture to completion and then again must not re-dispatch
    anything: the latest persisted state already reflects the final bar, so
    the second run is one long skip window."""
    config = config_for(tmp_path)
    fixture = two_day_fixture(tmp_path)

    await run_from_config(config, fixture, "always", max_bars=None)
    orders_before = len(
        [e for e in day_events(tmp_path, DAY_ONE) if e["event"] == "order_result"]
    ) + len([e for e in day_events(tmp_path, DAY_TWO) if e["event"] == "order_result"])

    await run_from_config(config, fixture, "always", max_bars=None)
    orders_after = len(
        [e for e in day_events(tmp_path, DAY_ONE) if e["event"] == "order_result"]
    ) + len([e for e in day_events(tmp_path, DAY_TWO) if e["event"] == "order_result"])

    assert orders_after == orders_before, "a completed replay re-sent orders when re-run"
