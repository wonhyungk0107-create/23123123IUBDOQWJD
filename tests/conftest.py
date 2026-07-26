"""Shared test configuration and fixtures.

Determinism is a requirement, not a convenience: acceptance gate G12 says
``make verify`` and ``make demo`` must produce identical results across runs. So
Hypothesis is registered in ``derandomize`` mode -- the same examples are generated
every time, and a failure found in CI reproduces locally without a database file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from tradeup.clock import FixedClock
from tradeup.config import Settings
from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule
from tradeup.domain.money import BalanceType, Currency

settings.register_profile(
    "deterministic",
    derandomize=True,
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    print_blob=True,
)
settings.load_profile("deterministic")


#: Fixed instant used across every test and the offline demo.
FROZEN_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

FEE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def frozen_now() -> datetime:
    return FROZEN_NOW


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FROZEN_NOW)


@pytest.fixture
def settings_obj(tmp_path: Path) -> Settings:
    """Settings with the shipped defaults and an isolated artifacts directory."""
    return Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}",
        artifacts_dir=tmp_path / "artifacts",
    )


def _rule(
    rule_id: str,
    venue: str,
    operation: FeeOperation,
    percentage: str,
    fixed_minor: int = 0,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue=venue,
        operation=operation,
        balance_type=BalanceType.CASH_WITHDRAWABLE,
        currency=Currency.USD,
        percentage=Decimal(percentage),
        fixed_minor=fixed_minor,
        effective_from=FEE_EPOCH,
        effective_until=None,
        source="tests/conftest.py synthetic schedule -- NOT a real venue fee",
        last_verified=None,
    )


@pytest.fixture
def fee_schedule() -> FeeSchedule:
    """A synthetic, clearly-labelled fee schedule.

    These percentages are invented for testing. They must never be mistaken for
    real venue fees; a real schedule carries a source URL and a verification date.
    """
    return FeeSchedule(
        [
            _rule("test:csfloat:purchase", "csfloat", FeeOperation.PURCHASE, "0.02"),
            _rule("test:csfloat:sale", "csfloat", FeeOperation.SALE, "0.02"),
            _rule("test:csfloat:withdrawal", "csfloat", FeeOperation.WITHDRAWAL, "0.01"),
            _rule("test:csfloat:deposit", "csfloat", FeeOperation.DEPOSIT, "0.00"),
            _rule("test:dmarket:purchase", "dmarket", FeeOperation.PURCHASE, "0.03"),
            _rule("test:dmarket:sale", "dmarket", FeeOperation.SALE, "0.05"),
            _rule("test:dmarket:withdrawal", "dmarket", FeeOperation.WITHDRAWAL, "0.02", 50),
            _rule("test:dmarket:deposit", "dmarket", FeeOperation.DEPOSIT, "0.00"),
        ]
    )
