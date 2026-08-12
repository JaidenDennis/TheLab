"""Compare what the agent believes it holds against what the broker holds.

PositionTracker is a simulation. It opens a position because a signal was
dispatched, not because a fill was confirmed, so the agent's belief and the
broker's reality can drift apart in six ways -- see DivergenceKind. Two of
them are expensive:

  AGENT_ONLY   the agent will send a flatten for a position that does not
               exist, and its P&L is counting a trade that never happened.
  BROKER_ONLY  nothing will flatten the position at the cutoff, because
               nothing believes it exists. It runs overnight.

The policy this module implements is deliberately conservative: the broker is
the source of truth, structural disagreement blocks new entries until it is
resolved, and nothing is ever silently rewritten to make the numbers agree.
Slippage is the one exception -- a different fill price is expected, is not a
structural problem, and halting on it would halt on every trade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from nq_agent.models import (
    BrokerPosition,
    Divergence,
    DivergenceKind,
    Position,
)


class PositionSource(ABC):
    """Somewhere to ask what is actually held.

    Separate from Executor on purpose. An executor sends orders; this reads
    state, and a broker that can do one may not expose the other. Keeping
    them apart also means reconciliation can be wired against a read-only
    credential.
    """

    @abstractmethod
    async def fetch_positions(self) -> list[BrokerPosition]:
        """Every open position the broker reports, across all accounts."""

    async def close(self) -> None:
        return None


class StaticPositionSource(PositionSource):
    """A fixed answer, changeable between calls. For tests and dry runs."""

    def __init__(self, positions: list[BrokerPosition] | None = None) -> None:
        self._positions = list(positions or [])

    def set(self, positions: list[BrokerPosition]) -> None:
        self._positions = list(positions)

    async def fetch_positions(self) -> list[BrokerPosition]:
        return list(self._positions)


class ReconciliationReport:
    def __init__(
        self,
        divergences: list[Divergence],
        consensus: BrokerPosition | None,
        agent_position: Position | None,
        at: datetime,
    ) -> None:
        self.divergences = divergences
        self._consensus = consensus
        self._agent_position = agent_position
        self._at = at

    @property
    def is_blocking(self) -> bool:
        return any(d.is_blocking for d in self.divergences)

    @property
    def blocking_accounts(self) -> list[str]:
        return [d.account_id for d in self.divergences if d.is_blocking]

    @property
    def consensus_price(self) -> Decimal | None:
        """The price every account filled at, when they all agree on one.

        None when they differ: three different fills is not one number to
        adopt, and picking one silently would put a made-up entry price into
        the P&L that the risk limits are measured in.
        """
        return self._consensus.average_price if self._consensus else None

    @property
    def adoptable_position(self) -> Position | None:
        """A position the agent could take ownership of, or None.

        Offered only when the agent is flat and every account agrees on the
        same position -- so the tracker can at least flatten it at the
        cutoff. Adoption is not approval: entries stay blocked either way,
        because a position appearing out of nowhere is a fault whether or not
        it can be cleaned up automatically.

        The result carries no stop or target. The broker reports what is
        held, never what the exit was meant to be.
        """
        if self._agent_position is not None or self._consensus is None:
            return None
        return Position(
            symbol=self._consensus.symbol,
            direction=self._consensus.direction,
            quantity=self._consensus.quantity,
            entry_price=self._consensus.average_price,
            entry_time=self._at,
        )

    def summary(self) -> str:
        problems = [d for d in self.divergences if d.kind is not DivergenceKind.AGREED]
        if not problems:
            return "agent and broker agree on every account"
        return "; ".join(
            f"{d.account_id}: {d.kind.value}" + (f" ({d.detail})" if d.detail else "")
            for d in problems
        )


class Reconciler:
    def __init__(self, symbol: str, accounts: list[str]) -> None:
        self._symbol = symbol
        self._accounts = list(accounts)

    def _compare(
        self, account: str, agent: Position | None, broker: BrokerPosition | None
    ) -> Divergence:
        def result(kind: DivergenceKind, detail: str = "") -> Divergence:
            return Divergence(account_id=account, kind=kind, detail=detail)

        if agent is None and broker is None:
            return result(DivergenceKind.AGREED)
        if agent is None and broker is not None:
            return result(
                DivergenceKind.BROKER_ONLY,
                f"broker holds {broker.quantity} {broker.direction.value} "
                f"at {broker.average_price}, agent believes it is flat",
            )
        if agent is not None and broker is None:
            return result(
                DivergenceKind.AGENT_ONLY,
                f"agent believes it holds {agent.quantity} {agent.direction.value}, "
                "broker reports flat",
            )

        assert agent is not None and broker is not None
        # Direction before quantity: if both differ, the direction is the
        # more fundamental disagreement and the more alarming thing to read.
        if agent.direction is not broker.direction:
            return result(
                DivergenceKind.DIRECTION_MISMATCH,
                f"agent {agent.direction.value}, broker {broker.direction.value}",
            )
        if agent.quantity != broker.quantity:
            return result(
                DivergenceKind.QUANTITY_MISMATCH,
                f"agent {agent.quantity}, broker {broker.quantity}",
            )
        if agent.entry_price != broker.average_price:
            return result(
                DivergenceKind.PRICE_MISMATCH,
                f"agent {agent.entry_price}, broker filled {broker.average_price}",
            )
        return result(DivergenceKind.AGREED)

    def _consensus(self, by_account: dict[str, BrokerPosition]) -> BrokerPosition | None:
        """The single position every account holds, when there is one.

        Unanimity is required. A position on one account out of three is not
        something to adopt into a tracker that stands for all three -- it is
        a fault for a human to look at.
        """
        if len(by_account) != len(self._accounts):
            return None
        positions = list(by_account.values())
        first = positions[0]
        for other in positions[1:]:
            if (
                other.direction is not first.direction
                or other.quantity != first.quantity
                or other.average_price != first.average_price
            ):
                return None
        return first

    def reconcile(
        self,
        agent_position: Position | None,
        broker_positions: list[BrokerPosition],
        at: datetime,
    ) -> ReconciliationReport:
        # Other instruments are not this agent's business and must not read
        # as a divergence -- the operator may well trade something else in
        # the same account by hand.
        relevant = [p for p in broker_positions if p.symbol == self._symbol]
        by_account = {p.account_id: p for p in relevant if p.account_id in self._accounts}

        divergences = [
            self._compare(account, agent_position, by_account.get(account))
            for account in self._accounts
        ]
        return ReconciliationReport(
            divergences=divergences,
            consensus=self._consensus(by_account),
            agent_position=agent_position,
            at=at,
        )
