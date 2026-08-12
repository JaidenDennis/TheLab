"""Comparing what the agent believes it holds against what the broker holds.

PositionTracker opens a position because a signal was dispatched, not because
a fill was confirmed. Every one of these cases is a way those two can drift
apart, and the expensive one is BROKER_ONLY: the agent believes it is flat, so
nothing will ever flatten the position the broker actually holds, and it runs
past the cutoff and overnight.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from nq_agent.models import BrokerPosition, Direction, DivergenceKind, Position
from nq_agent.reconcile import Reconciler, StaticPositionSource

ENTRY_TIME = datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)
ACCOUNTS = ["tradeify", "mff", "fundednext"]


def agent_long(quantity: int = 1, entry: str = "20100") -> Position:
    return Position(
        symbol="NQ",
        direction=Direction.LONG,
        quantity=quantity,
        entry_price=Decimal(entry),
        entry_time=ENTRY_TIME,
        stop_price=Decimal("20090"),
        target_price=Decimal("20120"),
    )


def broker_long(
    account: str = "tradeify", quantity: int = 1, price: str = "20100"
) -> BrokerPosition:
    return BrokerPosition(
        account_id=account,
        symbol="NQ",
        direction=Direction.LONG,
        quantity=quantity,
        average_price=Decimal(price),
    )


def broker_short(account: str = "tradeify", quantity: int = 1) -> BrokerPosition:
    return BrokerPosition(
        account_id=account,
        symbol="NQ",
        direction=Direction.SHORT,
        quantity=quantity,
        average_price=Decimal("20100"),
    )


def reconciler() -> Reconciler:
    return Reconciler(symbol="NQ", accounts=ACCOUNTS)


def report(agent: Position | None, positions: list[BrokerPosition]):
    # `at` comes from the caller, never a wall-clock read: an adopted position
    # needs an entry_time and the clock is the engine's to supply.
    return reconciler().reconcile(agent, positions, at=ENTRY_TIME)


# --- agreement --------------------------------------------------------------


def test_matching_positions_on_every_account_agree() -> None:
    result = report(agent_long(), [broker_long(a) for a in ACCOUNTS])

    assert all(d.kind is DivergenceKind.AGREED for d in result.divergences)
    assert result.is_blocking is False


def test_flat_everywhere_agrees() -> None:
    result = report(None, [])

    assert result.is_blocking is False
    assert all(d.kind is DivergenceKind.AGREED for d in result.divergences)


def test_one_divergence_is_reported_per_account() -> None:
    """Three prop accounts receive the same signal, so each is checked
    separately -- one broker rejecting while two filled is a real state."""
    result = report(agent_long(), [broker_long(a) for a in ACCOUNTS])

    assert [d.account_id for d in result.divergences] == ACCOUNTS


# --- the dangerous cases ----------------------------------------------------


def test_a_position_the_agent_does_not_know_about_is_blocking() -> None:
    """The expensive one. Nothing will flatten it at the cutoff, because
    nothing believes it exists."""
    result = report(None, [broker_long("tradeify")])

    kinds = {d.account_id: d.kind for d in result.divergences}
    assert kinds["tradeify"] is DivergenceKind.BROKER_ONLY
    assert result.is_blocking is True


def test_a_phantom_position_the_broker_does_not_have_is_blocking() -> None:
    """The order never filled, or something closed it externally. The agent
    would otherwise send a flatten for a position that does not exist."""
    result = report(agent_long(), [])

    assert {d.kind for d in result.divergences} == {DivergenceKind.AGENT_ONLY}
    assert result.is_blocking is True


def test_a_direction_mismatch_is_blocking() -> None:
    result = report(agent_long(), [broker_short("tradeify")])

    kinds = {d.account_id: d.kind for d in result.divergences}
    assert kinds["tradeify"] is DivergenceKind.DIRECTION_MISMATCH
    assert result.is_blocking is True


def test_a_quantity_mismatch_is_blocking() -> None:
    """A partial fill. Half the position is real and the agent thinks all of
    it is."""
    result = report(agent_long(quantity=3), [broker_long("tradeify", quantity=1)])

    kinds = {d.account_id: d.kind for d in result.divergences}
    assert kinds["tradeify"] is DivergenceKind.QUANTITY_MISMATCH
    assert result.is_blocking is True


def test_one_bad_account_blocks_even_when_the_others_agree() -> None:
    """Two accounts filled and one did not. That is still a divergence and
    still needs a human."""
    positions = [broker_long("tradeify"), broker_long("mff")]

    result = report(agent_long(), positions)

    assert result.is_blocking is True
    assert result.blocking_accounts == ["fundednext"]


# --- slippage, which is not a structural problem ----------------------------


def test_a_different_fill_price_is_reported_but_not_blocking() -> None:
    """Slippage is expected. The position is real, the direction and size are
    right, and halting on every tick of slippage would halt on every trade."""
    result = report(agent_long(entry="20100"), [broker_long(a, price="20101.25") for a in ACCOUNTS])

    assert {d.kind for d in result.divergences} == {DivergenceKind.PRICE_MISMATCH}
    assert result.is_blocking is False


def test_the_brokers_fill_price_is_offered_for_adoption() -> None:
    """P&L must be computed against what was actually paid, not what the
    strategy asked for."""
    result = report(agent_long(entry="20100"), [broker_long(a, price="20101.25") for a in ACCOUNTS])

    assert result.consensus_price == Decimal("20101.25")


def test_no_consensus_price_when_accounts_filled_differently() -> None:
    """Three different fills is not one number to adopt. Report it and let a
    human decide rather than silently picking one."""
    positions = [
        broker_long("tradeify", price="20100"),
        broker_long("mff", price="20105"),
        broker_long("fundednext", price="20110"),
    ]

    assert report(agent_long(), positions).consensus_price is None


# --- adoption ---------------------------------------------------------------


def test_a_broker_only_position_is_offered_for_adoption() -> None:
    """So the cutoff flatten can actually close it. Adopting is not approving
    -- entries stay blocked either way."""
    result = report(None, [broker_long(a) for a in ACCOUNTS])

    adopted = result.adoptable_position
    assert adopted is not None
    assert adopted.direction is Direction.LONG
    assert adopted.quantity == 1


def test_an_adopted_position_carries_no_invented_stop() -> None:
    """The broker does not tell us what the stop was meant to be. Making one
    up would put a fabricated exit into the position tracker."""
    result = report(None, [broker_long(a) for a in ACCOUNTS])

    adopted = result.adoptable_position
    assert adopted is not None
    assert adopted.stop_price is None
    assert adopted.target_price is None


def test_nothing_is_adoptable_when_the_accounts_disagree_with_each_other() -> None:
    positions = [broker_long("tradeify"), broker_short("mff")]

    assert report(None, positions).adoptable_position is None


def test_nothing_is_adoptable_when_only_one_account_holds_it() -> None:
    """Adoption requires unanimity, not merely that the accounts reporting a
    position agree. One account out of three holding something is not a
    position to adopt into a tracker that stands for all three -- it is a
    fault for a human to look at, and the other two accounts are the ones
    that would then be traded on a false belief."""
    result = report(None, [broker_long("tradeify")])

    assert result.adoptable_position is None
    assert result.is_blocking is True


def test_nothing_is_adoptable_when_the_agent_already_has_a_position() -> None:
    """Adoption is for the flat-agent case. When both hold something and they
    disagree, a human resolves it."""
    result = report(agent_long(quantity=3), [broker_long(a, quantity=1) for a in ACCOUNTS])

    assert result.adoptable_position is None


# --- other symbols ----------------------------------------------------------


def test_positions_in_other_symbols_are_ignored() -> None:
    """The agent trades one instrument. A position in something else is not
    its business and must not read as a divergence."""
    other = BrokerPosition(
        account_id="tradeify",
        symbol="ES",
        direction=Direction.LONG,
        quantity=1,
        average_price=Decimal("5000"),
    )

    result = report(None, [other])

    assert result.is_blocking is False


# --- the source seam --------------------------------------------------------


async def test_a_static_source_returns_what_it_was_given() -> None:
    source = StaticPositionSource([broker_long("tradeify")])

    assert await source.fetch_positions() == [broker_long("tradeify")]


async def test_a_static_source_can_be_updated_between_calls() -> None:
    """Stands in for a broker whose state changes underneath the agent."""
    source = StaticPositionSource([])
    assert await source.fetch_positions() == []

    source.set([broker_long("mff")])

    assert len(await source.fetch_positions()) == 1


def test_the_summary_names_what_is_wrong() -> None:
    """This is what lands in the journal and in an operator's alert at 3pm."""
    summary = report(None, [broker_long("tradeify")]).summary()

    assert "tradeify" in summary
    assert "BROKER_ONLY" in summary


@pytest.mark.parametrize(
    "quantity,expected",
    [(1, DivergenceKind.AGREED), (2, DivergenceKind.QUANTITY_MISMATCH)],
)
def test_quantity_comparison_is_exact(quantity: int, expected: DivergenceKind) -> None:
    result = report(agent_long(quantity=1), [broker_long(a, quantity=quantity) for a in ACCOUNTS])

    assert {d.kind for d in result.divergences} == {expected}
