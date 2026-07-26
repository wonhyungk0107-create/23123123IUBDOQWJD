"""Immutable economic ledger.

Every movement of value is an append-only event. Nothing is ever updated in place,
because the point of the ledger is to answer "what did we actually predict, and what
actually happened" months later, when the answer is inconvenient.

Sign convention: ``amount`` is signed from our perspective. Money leaving us is
negative, money arriving is positive. The sign is *validated* against the event type,
so a fee recorded as a positive number is a hard error rather than a quietly
profitable contract.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.domain.money import BalanceType, Currency, Money, money_sum

__all__ = [
    "LedgerEvent",
    "LedgerEventType",
    "LedgerSignError",
    "RealizedContractResult",
    "make_event_id",
    "settled_cash_pnl",
]


class LedgerSignError(Exception):
    """Raised when an amount's sign contradicts its event type."""


class _Direction(enum.Enum):
    INFLOW = enum.auto()
    OUTFLOW = enum.auto()
    EITHER = enum.auto()
    ZERO = enum.auto()


class LedgerEventType(enum.StrEnum):
    """Every economically meaningful thing that can happen."""

    CASH_DEPOSIT = "CASH_DEPOSIT"
    MARKET_BALANCE_CREDIT = "MARKET_BALANCE_CREDIT"
    PURCHASE = "PURCHASE"
    BUYER_FEE = "BUYER_FEE"
    DEPOSIT_FEE = "DEPOSIT_FEE"
    FX_COST = "FX_COST"
    INPUT_TRANSFER = "INPUT_TRANSFER"
    TRADEUP_CONSUMPTION = "TRADEUP_CONSUMPTION"
    OUTPUT_CREATION = "OUTPUT_CREATION"
    OUTPUT_LISTING = "OUTPUT_LISTING"
    SALE = "SALE"
    SELLER_FEE = "SELLER_FEE"
    WITHDRAWAL_FEE = "WITHDRAWAL_FEE"
    REFUND = "REFUND"
    REVERSAL = "REVERSAL"
    WRITE_OFF = "WRITE_OFF"
    SETTLEMENT = "SETTLEMENT"

    @property
    def direction(self) -> _Direction:
        return _DIRECTION[self]

    @property
    def is_cost(self) -> bool:
        return self.direction is _Direction.OUTFLOW

    @property
    def affects_settled_cash(self) -> bool:
        """Whether this event moves realised, withdrawable cash.

        Custody moves (transfers, consumption, output creation) are recorded for the
        audit trail but carry no cash effect, so they must not touch P&L.
        """
        return self.direction is not _Direction.ZERO


_DIRECTION: dict[LedgerEventType, _Direction] = {
    LedgerEventType.CASH_DEPOSIT: _Direction.INFLOW,
    LedgerEventType.MARKET_BALANCE_CREDIT: _Direction.INFLOW,
    LedgerEventType.PURCHASE: _Direction.OUTFLOW,
    LedgerEventType.BUYER_FEE: _Direction.OUTFLOW,
    LedgerEventType.DEPOSIT_FEE: _Direction.OUTFLOW,
    LedgerEventType.FX_COST: _Direction.OUTFLOW,
    LedgerEventType.INPUT_TRANSFER: _Direction.ZERO,
    LedgerEventType.TRADEUP_CONSUMPTION: _Direction.ZERO,
    LedgerEventType.OUTPUT_CREATION: _Direction.ZERO,
    LedgerEventType.OUTPUT_LISTING: _Direction.ZERO,
    LedgerEventType.SALE: _Direction.INFLOW,
    LedgerEventType.SELLER_FEE: _Direction.OUTFLOW,
    LedgerEventType.WITHDRAWAL_FEE: _Direction.OUTFLOW,
    LedgerEventType.REFUND: _Direction.INFLOW,
    LedgerEventType.REVERSAL: _Direction.EITHER,
    LedgerEventType.WRITE_OFF: _Direction.OUTFLOW,
    LedgerEventType.SETTLEMENT: _Direction.EITHER,
}


def make_event_id(
    event_type: LedgerEventType,
    occurred_at: datetime,
    sequence: int,
    reference: str,
) -> str:
    """Deterministic event identifier.

    ``sequence`` disambiguates genuinely identical events (two identical fees on the
    same contract at the same instant) without making the id random, so replaying a
    fixture produces byte-identical ledger rows.
    """
    payload = f"{event_type.value}|{occurred_at.isoformat()}|{sequence}|{reference}"
    return f"LE-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One immutable economic fact."""

    event_id: str
    event_type: LedgerEventType
    amount: Money
    occurred_at: datetime
    recorded_at: datetime
    sequence: int
    #: Free-form venue-side identifier (order id, trade id, transfer id).
    reference: str = ""
    contract_id: str | None = None
    item_id: str | None = None
    venue: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("ledger timestamps must be timezone-aware UTC")
        if self.sequence < 0:
            raise ValueError("ledger sequence cannot be negative")
        direction = self.event_type.direction
        if direction is _Direction.OUTFLOW and self.amount.minor_units > 0:
            raise LedgerSignError(
                f"{self.event_type.value} is an outflow but amount is positive: {self.amount}"
            )
        if direction is _Direction.INFLOW and self.amount.minor_units < 0:
            raise LedgerSignError(
                f"{self.event_type.value} is an inflow but amount is negative: {self.amount}"
            )
        if direction is _Direction.ZERO and not self.amount.is_zero:
            raise LedgerSignError(
                f"{self.event_type.value} is a custody move and must carry a zero amount, "
                f"got {self.amount}"
            )

    @property
    def affects_settled_cash(self) -> bool:
        return (
            self.event_type.affects_settled_cash and self.amount.balance_type.is_withdrawable_cash
        )


def settled_cash_pnl(
    events: Sequence[LedgerEvent],
    *,
    currency: Currency,
    contract_id: str | None = None,
) -> Money:
    """Net movement of withdrawable cash.

    Deliberately ignores venue balances, Steam Wallet value and inventory marks. An
    unsold output contributes nothing here, which is the entire point: a contract is
    not profitable until proceeds settle and are attributed to it.
    """
    relevant = [
        e
        for e in events
        if e.affects_settled_cash and (contract_id is None or e.contract_id == contract_id)
    ]
    return money_sum(
        (e.amount for e in relevant),
        currency=currency,
        balance_type=BalanceType.CASH_WITHDRAWABLE,
    )


@dataclass(frozen=True)
class RealizedContractResult:
    """Predicted versus actual, for one completed contract.

    Stored so calibration can ask specific questions -- did we misprice the output,
    misjudge the fees, or misjudge how long the capital would be locked -- instead of
    only knowing that the total was wrong.
    """

    contract_id: str
    rule_version: str

    # -- what we predicted ---------------------------------------------------
    predicted_ev: Money
    predicted_roi: Decimal
    predicted_outcome_distribution: Mapping[str, Fraction]
    predicted_sale_value: Money
    predicted_capital_days: Decimal

    # -- what happened -------------------------------------------------------
    actual_output_skin_id: str | None
    actual_output_float: Decimal | None
    actual_gross_sale: Money | None
    actual_fees: Money | None
    actual_settled_proceeds: Money | None
    actual_capital_days: Decimal | None

    completed_at: datetime | None
    settled_at: datetime | None

    def __post_init__(self) -> None:
        for name in ("completed_at", "settled_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware UTC")

    @property
    def is_settled(self) -> bool:
        return self.settled_at is not None and self.actual_settled_proceeds is not None

    @property
    def realized_pnl(self) -> Money | None:
        """Settled proceeds minus what we spent, or ``None`` until settlement."""
        if self.actual_settled_proceeds is None:
            return None
        return self.actual_settled_proceeds

    @property
    def prediction_error(self) -> Money | None:
        """Actual settled P&L minus predicted EV. Negative means we were optimistic."""
        realized = self.realized_pnl
        if realized is None:
            return None
        return realized - self.predicted_ev

    @property
    def outcome_was_predicted(self) -> bool:
        """Whether the realised output was in the modelled distribution at all.

        A ``False`` here means the rule registry or metadata is wrong, which is far
        more serious than an unlucky draw.
        """
        if self.actual_output_skin_id is None:
            return False
        return self.actual_output_skin_id in self.predicted_outcome_distribution
