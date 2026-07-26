"""TradeUpSpy parity validator -- offline comparison only. Never fetches.

TradeUpSpy publishes no API, and its ``robots.txt`` explicitly disallows exactly the
paths a parity check would want:

    Disallow: /calculator/custom/*
    Disallow: /calculator/shared/*
    Disallow: /calculator/share/*
    Disallow: /calculator/weekly/*

So this module contains no HTTP client. It does two things: compares our computed
contract against a manually exported fixture, and *composes a URL for a human to
open*. The URL is returned as text; nothing in this codebase requests it.

The comparison is decomposed rather than boolean. "We disagree" is useless; "we
disagree on the output float but agree on probabilities" points straight at the
float formula. Each disagreement carries a category so parity failures can be
counted by cause across a shadow run.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from tradeup.domain.contracts import TradeupCandidate

__all__ = [
    "DisagreementCategory",
    "ParityComparison",
    "ParityDisagreement",
    "TradeUpSpyValidator",
    "compose_manual_check_url",
]


class DisagreementCategory(enum.StrEnum):
    """Why our numbers and theirs differ. The categorisation is the diagnostic."""

    METADATA = "METADATA"
    """Different float caps, collection membership, or output universe."""

    RULE_VERSION = "RULE_VERSION"
    """Different assumptions about input count or quality eligibility."""

    FLOAT_FORMULA = "FLOAT_FORMULA"
    """Same inputs, different projected output float."""

    PROBABILITY = "PROBABILITY"
    """Same outputs, different odds."""

    PRICE_TIMESTAMP = "PRICE_TIMESTAMP"
    """Prices observed at different moments."""

    FEE = "FEE"
    """Same gross figures, different net."""


@dataclass(frozen=True, slots=True)
class ParityDisagreement:
    category: DisagreementCategory
    field: str
    ours: str
    theirs: str

    def __str__(self) -> str:
        return f"[{self.category.value}] {self.field}: ours={self.ours} theirs={self.theirs}"


@dataclass(frozen=True)
class ParityComparison:
    """Outcome of comparing one candidate against one exported reference."""

    candidate_id: str
    fixture_id: str
    disagreements: tuple[ParityDisagreement, ...] = field(default_factory=tuple)

    @property
    def agrees(self) -> bool:
        return not self.disagreements

    @property
    def categories(self) -> tuple[DisagreementCategory, ...]:
        return tuple(dict.fromkeys(d.category for d in self.disagreements))

    def summary(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "fixture_id": self.fixture_id,
            "agrees": str(self.agrees),
            "categories": ",".join(c.value for c in self.categories) or "none",
            "disagreement_count": str(len(self.disagreements)),
        }


def compose_manual_check_url(candidate: TradeupCandidate) -> str:
    """Build a URL for a *human* to open in a browser.

    Returned as a string and never requested. The calculator paths are
    robots-disallowed, so an automated fetch would be a deliberate violation of the
    site's stated access rules.
    """
    return (
        "https://www.tradeupspy.com/calculator/custom"
        f"?contract={candidate.candidate_id}&inputs={candidate.input_count}"
    )


class TradeUpSpyValidator:
    """Compares our contract maths against manually exported reference data.

    Fixtures are JSON exported by an operator from the site by hand. The expected
    shape is::

        {
          "fixture_id": "...",
          "input_count": 10,
          "output_float": "0.245",
          "outcomes": {"<skin_id>": "0.35", ...},
          "notes": "..."
        }
    """

    def __init__(self, *, float_tolerance: Decimal = Decimal("0.0001")) -> None:
        if float_tolerance < 0:
            raise ValueError("float_tolerance cannot be negative")
        self._tolerance = float_tolerance

    def load_fixture(self, path: Path) -> Mapping[str, object]:
        document = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        if not isinstance(document, Mapping):
            raise ValueError(f"parity fixture {path} is not an object")
        return document

    def compare(
        self,
        candidate: TradeupCandidate,
        reference: Mapping[str, object],
    ) -> ParityComparison:
        """Compare one candidate against one reference export."""
        disagreements: list[ParityDisagreement] = []
        fixture_id = str(reference.get("fixture_id", "<unnamed>"))

        expected_inputs = reference.get("input_count")
        if isinstance(expected_inputs, int) and expected_inputs != candidate.input_count:
            disagreements.append(
                ParityDisagreement(
                    category=DisagreementCategory.RULE_VERSION,
                    field="input_count",
                    ours=str(candidate.input_count),
                    theirs=str(expected_inputs),
                )
            )

        expected_float = reference.get("output_float")
        if expected_float is not None:
            # Every outcome shares the same average normalised float, so any single
            # outcome's projected float is a valid comparison point.
            ours = candidate.outcomes[0].output_float
            theirs = Decimal(str(expected_float))
            if abs(ours - theirs) > self._tolerance:
                disagreements.append(
                    ParityDisagreement(
                        category=DisagreementCategory.FLOAT_FORMULA,
                        field="output_float",
                        ours=str(ours),
                        theirs=str(theirs),
                    )
                )

        expected_outcomes = reference.get("outcomes")
        if isinstance(expected_outcomes, Mapping):
            ours_by_skin: dict[str, Fraction] = {}
            for outcome in candidate.outcomes:
                ours_by_skin[outcome.skin_id] = (
                    ours_by_skin.get(outcome.skin_id, Fraction(0)) + outcome.probability
                )
            for skin_id, expected in expected_outcomes.items():
                theirs_probability = Fraction(Decimal(str(expected)))
                ours_probability = ours_by_skin.get(str(skin_id))
                if ours_probability is None:
                    disagreements.append(
                        ParityDisagreement(
                            category=DisagreementCategory.METADATA,
                            field=f"outcome:{skin_id}",
                            ours="absent",
                            theirs=str(theirs_probability),
                        )
                    )
                elif abs(ours_probability - theirs_probability) > Fraction(self._tolerance):
                    disagreements.append(
                        ParityDisagreement(
                            category=DisagreementCategory.PROBABILITY,
                            field=f"outcome:{skin_id}",
                            ours=str(ours_probability),
                            theirs=str(theirs_probability),
                        )
                    )
            for skin_id in ours_by_skin:
                if skin_id not in expected_outcomes:
                    disagreements.append(
                        ParityDisagreement(
                            category=DisagreementCategory.METADATA,
                            field=f"outcome:{skin_id}",
                            ours=str(ours_by_skin[skin_id]),
                            theirs="absent",
                        )
                    )

        return ParityComparison(
            candidate_id=candidate.candidate_id,
            fixture_id=fixture_id,
            disagreements=tuple(disagreements),
        )

    def compare_all(
        self,
        candidates: Sequence[TradeupCandidate],
        references: Mapping[str, Mapping[str, object]],
    ) -> tuple[ParityComparison, ...]:
        """Compare every candidate that has a reference. Unmatched ones are skipped."""
        return tuple(
            self.compare(candidate, references[candidate.candidate_id])
            for candidate in candidates
            if candidate.candidate_id in references
        )
