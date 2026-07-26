"""Capital timing and carry cost.

Money committed to a contract is unavailable for the next one. A 12% return over
thirty days is worse than 8% over five, and a system that ranks on ROI alone will
systematically prefer the slower trade.

Capital-days are therefore modelled explicitly, from the actual mechanical delays:
the trade lock on each input, the transfer to Steam, the time to sell the output,
and the venue's settlement hold. None of these are same-day.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from tradeup.domain.money import Money

__all__ = ["CapitalModel", "CapitalTimeline"]

DAYS_PER_YEAR = Decimal(365)


@dataclass(frozen=True, slots=True)
class CapitalTimeline:
    """The stages between committing cash and getting it back.

    Stages are sequential, not overlapping: an input cannot be transferred before
    its trade lock expires, and the output cannot be sold before it exists.
    """

    max_input_trade_lock_days: int
    transfer_days: int
    contract_execution_days: int
    expected_days_to_sale: int
    settlement_days: int

    def __post_init__(self) -> None:
        for name in (
            "max_input_trade_lock_days",
            "transfer_days",
            "contract_execution_days",
            "expected_days_to_sale",
            "settlement_days",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def total_days(self) -> int:
        return (
            self.max_input_trade_lock_days
            + self.transfer_days
            + self.contract_execution_days
            + self.expected_days_to_sale
            + self.settlement_days
        )

    def breakdown(self) -> dict[str, int]:
        return {
            "input_trade_lock": self.max_input_trade_lock_days,
            "transfer_to_steam": self.transfer_days,
            "contract_execution": self.contract_execution_days,
            "time_to_sale": self.expected_days_to_sale,
            "settlement": self.settlement_days,
            "total": self.total_days,
        }


@dataclass(frozen=True, slots=True)
class CapitalModel:
    """Converts committed principal and elapsed days into a carry charge.

    ``annual_rate`` is an opportunity cost, not an interest payment. It exists so
    that a contract which ties up capital for a month is charged for the trades it
    prevented. The default in :class:`~tradeup.config.Settings` is a stated
    assumption to be calibrated, not a measurement.
    """

    annual_rate: Decimal
    #: Days between purchase and the asset becoming transferable, when the venue
    #: imposes a hold beyond the per-listing trade lock.
    default_transfer_days: int = 1
    default_contract_execution_days: int = 1
    default_settlement_days: int = 2

    def __post_init__(self) -> None:
        if self.annual_rate < 0:
            raise ValueError("annual capital cost rate cannot be negative")

    def carry_cost(self, principal: Money, days: int | Decimal) -> Money:
        """Opportunity cost of holding ``principal`` for ``days``.

        Rounds up: understating the cost of time is exactly the bias that makes
        slow contracts look competitive.
        """
        if isinstance(days, float):
            raise TypeError("days must be int or Decimal, not float")
        elapsed = Decimal(days)
        if elapsed < 0:
            raise ValueError("carry days cannot be negative")
        if principal.is_negative:
            raise ValueError("carry cost requires non-negative principal")
        factor = (self.annual_rate * elapsed / DAYS_PER_YEAR).quantize(
            Decimal("0.00000001"), rounding=ROUND_CEILING
        )
        return principal.scaled_up(factor)

    def timeline(
        self,
        *,
        max_input_trade_lock_days: int,
        expected_days_to_sale: int,
        transfer_days: int | None = None,
        contract_execution_days: int | None = None,
        settlement_days: int | None = None,
    ) -> CapitalTimeline:
        return CapitalTimeline(
            max_input_trade_lock_days=max_input_trade_lock_days,
            transfer_days=self.default_transfer_days if transfer_days is None else transfer_days,
            contract_execution_days=(
                self.default_contract_execution_days
                if contract_execution_days is None
                else contract_execution_days
            ),
            expected_days_to_sale=expected_days_to_sale,
            settlement_days=(
                self.default_settlement_days if settlement_days is None else settlement_days
            ),
        )

    def profit_per_capital_day(self, profit: Money, days: int | Decimal) -> Money:
        """Velocity metric. A zero-day contract is charged one day, never zero.

        Treating an instantaneous round trip as infinite velocity would let a
        rounding artefact dominate the ranking.
        """
        elapsed = max(Decimal(days), Decimal(1))
        return profit.scaled_down(Decimal(1) / elapsed)
