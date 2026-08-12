from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalIntent(str, Enum):
    ENTRY = "ENTRY"
    FLATTEN = "FLATTEN"


class OrderOutcome(str, Enum):
    """What is actually known about an order after dispatch.

    A boolean cannot carry this. `success=False` asserts the order did not
    fill, but a timeout, a connection reset mid-POST, or a cancellation
    between sending and reading the response all leave that genuinely open --
    and the recovery for "rejected" (retry, or route elsewhere) is the
    opposite of the recovery for "unknown" (reconcile against the broker
    before doing anything at all). Collapsing the two is how an agent ends up
    holding a position it believes it does not have.
    """

    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class VetoReason(str, Enum):
    MAX_TRADES = "MAX_TRADES"
    PAST_CUTOFF = "PAST_CUTOFF"
    KILL_SWITCH = "KILL_SWITCH"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    SESSION_CLOSED = "SESSION_CLOSED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    TRAILING_DRAWDOWN = "TRAILING_DRAWDOWN"


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(require_utc)]
"""A datetime that must arrive timezone-aware and is normalised to UTC.

Declared once and reused so a new datetime field cannot silently opt out of
the UTC contract by forgetting its validator.
"""


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Tick(Frozen):
    symbol: str
    ts: UtcDatetime
    price: Decimal
    size: int


class Bar(Frozen):
    symbol: str
    timeframe: str
    open_time: UtcDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    closed: bool = True

    @field_validator("timeframe")
    @classmethod
    def _known_timeframe(cls, value: str) -> str:
        if value not in TIMEFRAME_SECONDS:
            raise ValueError(f"unsupported timeframe: {value}")
        return value

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])


class Signal(Frozen):
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: UtcDatetime
    symbol: str
    intent: SignalIntent = SignalIntent.ENTRY
    direction: Direction
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    quantity: int = Field(ge=1)
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_intent_prices(self) -> Signal:
        prices = (self.entry_price, self.stop_price, self.target_price)
        if self.intent is SignalIntent.FLATTEN:
            if any(p is not None for p in prices):
                raise ValueError("FLATTEN must not carry price fields")
            return self

        if any(p is None for p in prices):
            raise ValueError("ENTRY requires entry_price, stop_price, target_price")

        entry, stop, target = self.entry_price, self.stop_price, self.target_price
        assert entry is not None and stop is not None and target is not None
        if self.direction is Direction.LONG and not (stop < entry < target):
            raise ValueError("LONG ENTRY requires stop_price < entry_price < target_price")
        if self.direction is Direction.SHORT and not (target < entry < stop):
            raise ValueError("SHORT ENTRY requires target_price < entry_price < stop_price")
        return self


class OrderResult(Frozen):
    signal_id: str
    executor_name: str
    # Required, with no default: every executor has to state what it actually
    # knows. A default would let a new one inherit an assumption.
    outcome: OrderOutcome
    account_id: str | None = None
    latency_ms: int = 0
    error: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Confirmed filled. UNKNOWN is deliberately not success -- nothing
        should treat "maybe" as a fill."""
        return self.outcome is OrderOutcome.FILLED

    @property
    def needs_reconciliation(self) -> bool:
        """The agent's belief about this order may not match the broker's.

        The only honest response is to ask the broker what it holds before
        sending anything else for this symbol.
        """
        return self.outcome is OrderOutcome.UNKNOWN


class Position(Frozen):
    symbol: str
    direction: Direction
    quantity: int
    entry_price: Decimal
    entry_time: UtcDatetime
    stop_price: Decimal
    target_price: Decimal


class PositionClose(Frozen):
    position: Position
    exit_price: Decimal
    exit_time: UtcDatetime
    exit_reason: str


class SessionState(Frozen):
    session_date: date
    trades_taken: int = 0
    is_halted: bool = False
    strategy_state: dict[str, Any] = Field(default_factory=dict)
    last_bar_time: UtcDatetime | None = None
    position: Position | None = None
    # RiskManager.snapshot(). Persisted for the same reason trades_taken is:
    # a restart that forgets the day's realised losses resumes trading
    # straight through the limit that had just stopped it.
    risk_state: dict[str, str] = Field(default_factory=dict)


class RiskVeto(Frozen):
    signal_id: str
    reason: VetoReason
    detail: str
