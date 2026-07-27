"""Turning confirmation pairs into a fitted ask haircut.

The sweep values outputs at ``ask * (1 - sale_fee - haircut)``; the confirmation
values the same lead from executable listings and completed-sale evidence. Each
persisted pair therefore implies the haircut that *would have* made the estimate
match reality:

    value_ratio  r = exact_value / estimated_value
    implied  h' = 1 - fee - r * (1 - fee - h_at_estimate_time)

The report gives the distribution of ``r`` and the implied haircuts, split by
quality (StatTrak and Normal markets behave differently), and recommends the
**median** implied haircut — with the even-count median taken from the *larger*
haircut of the middle pair, because overstating a cost is the survivable error.

Application policy: a recommendation backed by at least
:data:`MIN_RELIABLE_SAMPLES` pairs for its quality is applied automatically to
the next sweep via :func:`applied_haircuts` — the batch feeds its own history
back so estimates track measured reality without an operator in the loop. An
unreliable recommendation steers nothing: below the sample floor the
configured prior applies, and the report labels the anecdote as such. The
operator can still override explicitly via ``candidates prospects
--ask-haircut``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeup.domain.items import QualityType

__all__ = [
    "MIN_RELIABLE_SAMPLES",
    "CalibrationPair",
    "CalibrationReport",
    "QualityCalibration",
    "applied_haircuts",
    "build_calibration_report",
]

#: Below this many pairs a recommendation is an anecdote, and the report says so.
MIN_RELIABLE_SAMPLES = 10

#: The sale-fee assumption the sweep's net formula used. Matches
#: valuation/venue_fees.py; revisit both together if the sourced fee changes.
_SALE_FEE = Decimal("0.08")

_MAX_HAIRCUT = Decimal("0.95")


@dataclass(frozen=True, slots=True)
class CalibrationPair:
    """One estimate and its exact-listing confirmation."""

    quality: QualityType
    estimated_output_value_minor: int
    exact_output_value_minor: int
    estimated_roi: Decimal
    exact_roi: Decimal
    ask_haircut_at_estimate: Decimal

    def __post_init__(self) -> None:
        if self.estimated_output_value_minor <= 0:
            raise ValueError("a calibration pair needs a positive estimated value")
        if self.exact_output_value_minor < 0:
            raise ValueError("exact output value cannot be negative")

    @property
    def value_ratio(self) -> Decimal:
        return Decimal(self.exact_output_value_minor) / Decimal(self.estimated_output_value_minor)

    @property
    def implied_haircut(self) -> Decimal:
        """The haircut that would have made this estimate match this confirmation."""
        implied = (
            Decimal(1)
            - _SALE_FEE
            - self.value_ratio * (Decimal(1) - _SALE_FEE - self.ask_haircut_at_estimate)
        )
        return min(max(implied, Decimal(0)), _MAX_HAIRCUT)

    @property
    def roi_gap(self) -> Decimal:
        return self.estimated_roi - self.exact_roi


def _conservative_median(values: Sequence[Decimal]) -> Decimal:
    """Median; for even counts, the larger of the middle pair (bigger haircut)."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return max(ordered[middle - 1], ordered[middle])


@dataclass(frozen=True)
class QualityCalibration:
    """Gap distribution for one quality tier."""

    quality: QualityType
    sample_count: int
    median_value_ratio: Decimal
    median_roi_gap: Decimal
    recommended_ask_haircut: Decimal
    reliable: bool

    def summary(self) -> dict[str, str]:
        return {
            "quality": self.quality.value,
            "samples": str(self.sample_count),
            "median_value_ratio": str(self.median_value_ratio.quantize(Decimal("0.0001"))),
            "median_roi_gap": str(self.median_roi_gap.quantize(Decimal("0.0001"))),
            "recommended_ask_haircut": str(
                self.recommended_ask_haircut.quantize(Decimal("0.0001"))
            ),
            "reliable": str(self.reliable),
        }


@dataclass(frozen=True)
class CalibrationReport:
    """The whole dataset, overall and per quality."""

    overall: QualityCalibration | None
    by_quality: tuple[QualityCalibration, ...]

    @property
    def has_data(self) -> bool:
        return self.overall is not None


def _calibrate(quality: QualityType, pairs: Sequence[CalibrationPair]) -> QualityCalibration:
    ratios = [pair.value_ratio for pair in pairs]
    haircuts = [pair.implied_haircut for pair in pairs]
    gaps = [pair.roi_gap for pair in pairs]
    # For the ratio, the *smaller* middle value is the conservative read (less
    # value survives); for the haircut that is exactly the larger one, which
    # _conservative_median already picks. Ratio median mirrors it via negation.
    return QualityCalibration(
        quality=quality,
        sample_count=len(pairs),
        median_value_ratio=-_conservative_median([-ratio for ratio in ratios]),
        median_roi_gap=_conservative_median(gaps),
        recommended_ask_haircut=_conservative_median(haircuts),
        reliable=len(pairs) >= MIN_RELIABLE_SAMPLES,
    )


def applied_haircuts(
    report: CalibrationReport,
    *,
    default: Decimal,
) -> dict[QualityType, Decimal]:
    """The ask haircut each quality's next sweep runs under.

    A reliable per-quality recommendation (``>= MIN_RELIABLE_SAMPLES`` pairs)
    is applied automatically; below the floor the configured ``default``
    applies, because an anecdote must steer nothing. Every quality is present
    in the result so callers never fall back implicitly.
    """
    applied = {quality: default for quality in QualityType}
    for calibration in report.by_quality:
        if calibration.reliable:
            applied[calibration.quality] = calibration.recommended_ask_haircut
    return applied


def build_calibration_report(pairs: Sequence[CalibrationPair]) -> CalibrationReport:
    """Distribution and recommendations from every confirmed pair."""
    if not pairs:
        return CalibrationReport(overall=None, by_quality=())
    by_quality: list[QualityCalibration] = []
    for quality in sorted({pair.quality for pair in pairs}, key=lambda q: q.value):
        subset = [pair for pair in pairs if pair.quality is quality]
        by_quality.append(_calibrate(quality, subset))
    # The overall row reuses NORMAL as a label-free aggregate would need a new
    # type; it is presented as "ALL" by the caller.
    overall = _calibrate(pairs[0].quality, list(pairs))
    return CalibrationReport(overall=overall, by_quality=tuple(by_quality))
