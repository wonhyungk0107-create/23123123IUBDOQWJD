"""Configuration and risk gates.

Every threshold that can reject a candidate lives here, in one place, as a typed
value. Scattering ``0.08`` through the codebase makes the effective policy
unknowable; a single settings object makes it auditable and lets the demo pin an
exact configuration.

Two safety properties are enforced at construction rather than at use:

* ``final_min_net_roi`` may not exceed ``discovery_min_net_roi``. The discovery
  threshold exists to absorb staleness between scan and revalidation, so a discovery
  bar *below* the final bar would surface candidates that can only ever be rejected.
* Live execution and a non-zero daily spend must be enabled together and explicitly.
  Defaults are off and zero.

Secrets are :class:`~pydantic.SecretStr`. They are never logged, never rendered into
an operator card, and never written to an evidence artifact.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradeup.domain.money import BalanceType, Currency, Money

__all__ = ["Settings", "load_settings"]


class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="TRADEUP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- execution safety ----------------------------------------------------
    live_execution_enabled: bool = Field(
        default=False,
        description="Master switch. False means no code path may place an order.",
    )
    max_daily_spend_minor: int = Field(
        default=0,
        ge=0,
        description="Hard daily spend ceiling in minor units. Zero during groundwork.",
    )

    # -- profitability gates -------------------------------------------------
    discovery_min_net_roi: Decimal = Field(
        default=Decimal("0.12"),
        description="Net ROI a candidate must show at discovery, before revalidation.",
    )
    final_min_net_roi: Decimal = Field(
        default=Decimal("0.08"),
        description="Net ROI a candidate must still show after direct revalidation.",
    )
    min_absolute_expected_profit_minor: int = Field(
        default=300,
        ge=0,
        description="Floor on absolute EV, so trivial-dollar contracts are ignored.",
    )

    # -- risk limits ---------------------------------------------------------
    max_contract_cost_minor: int = Field(default=50_000, gt=0)
    max_worst_case_loss_minor: int = Field(default=40_000, gt=0)
    max_partial_fill_exposure_minor: int = Field(default=15_000, gt=0)
    max_quote_age_seconds: int = Field(default=300, gt=0)
    max_expected_capital_days: int = Field(default=21, gt=0)
    max_unvaluable_probability_mass: Decimal = Field(
        default=Decimal("0.10"),
        description="Reject if more than this share of outcomes could not be priced.",
    )
    min_output_liquidity_evidence: int = Field(
        default=1,
        ge=0,
        description="Minimum independent price observations behind the modelled exit.",
    )

    # -- economic assumptions ------------------------------------------------
    base_currency: Currency = Currency.USD
    annual_capital_cost_rate: Decimal = Field(
        default=Decimal("0.12"),
        ge=Decimal(0),
        description="Opportunity cost of committed capital, used for carry charges.",
    )
    default_bundle_completion_probability: Decimal = Field(
        default=Decimal("0.85"),
        gt=Decimal(0),
        le=Decimal(1),
        description=(
            "Prior probability that a complete bundle can actually be filled. "
            "Calibrate from observed shadow-run fill rates; the default is a "
            "deliberately pessimistic placeholder, not a measurement."
        ),
    )
    operational_cost_per_contract_minor: int = Field(default=0, ge=0)

    # -- persistence and artifacts -------------------------------------------
    database_url: str = "sqlite+pysqlite:///./tradeup.db"
    artifacts_dir: Path = Path("artifacts")

    # -- credentials (all optional; absence degrades to a typed refusal) ------
    csfloat_api_key: SecretStr | None = None
    dmarket_public_key: SecretStr | None = None
    dmarket_secret_key: SecretStr | None = None
    skinsnipe_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    # -- network -------------------------------------------------------------
    http_timeout_seconds: Decimal = Field(default=Decimal("15"), gt=Decimal(0))
    http_max_retries: int = Field(default=3, ge=0, le=10)
    http_user_agent: str = "tradeup-agent/0.1 (shadow mode; contact: operator)"

    # -- validation ----------------------------------------------------------

    @model_validator(mode="after")
    def _check_gate_ordering(self) -> Self:
        if self.final_min_net_roi > self.discovery_min_net_roi:
            raise ValueError(
                f"final_min_net_roi ({self.final_min_net_roi}) exceeds "
                f"discovery_min_net_roi ({self.discovery_min_net_roi}); the discovery "
                "threshold must be the looser of the two or no candidate can survive"
            )
        if self.discovery_min_net_roi < 0 or self.final_min_net_roi < 0:
            raise ValueError("ROI gates cannot be negative")
        if not (Decimal(0) <= self.max_unvaluable_probability_mass <= Decimal(1)):
            raise ValueError("max_unvaluable_probability_mass must be within [0, 1]")
        return self

    @model_validator(mode="after")
    def _check_execution_safety(self) -> Self:
        if self.max_daily_spend_minor > 0 and not self.live_execution_enabled:
            raise ValueError(
                "max_daily_spend_minor is non-zero but live_execution_enabled is false; "
                "a spend budget without an execution switch is a misconfiguration"
            )
        return self

    # -- money-typed accessors ------------------------------------------------
    # Thresholds are stored as integers so they round-trip through the environment,
    # and exposed as Money so comparisons go through the balance-type guard.

    def _cash(self, minor: int) -> Money:
        return Money(minor, self.base_currency, BalanceType.CASH_WITHDRAWABLE)

    @property
    def min_absolute_expected_profit(self) -> Money:
        return self._cash(self.min_absolute_expected_profit_minor)

    @property
    def max_contract_cost(self) -> Money:
        return self._cash(self.max_contract_cost_minor)

    @property
    def max_worst_case_loss(self) -> Money:
        return self._cash(self.max_worst_case_loss_minor)

    @property
    def max_partial_fill_exposure(self) -> Money:
        return self._cash(self.max_partial_fill_exposure_minor)

    @property
    def max_daily_spend(self) -> Money:
        return self._cash(self.max_daily_spend_minor)

    @property
    def operational_cost_per_contract(self) -> Money:
        return self._cash(self.operational_cost_per_contract_minor)

    @property
    def evidence_dir(self) -> Path:
        return self.artifacts_dir / "evidence"

    @property
    def reports_dir(self) -> Path:
        return self.artifacts_dir / "reports"

    def available_credentials(self) -> dict[str, bool]:
        """Which credentials are present. Values are booleans, never the secrets."""
        return {
            "csfloat_api_key": self.csfloat_api_key is not None,
            "dmarket_public_key": self.dmarket_public_key is not None,
            "dmarket_secret_key": self.dmarket_secret_key is not None,
            "skinsnipe_api_key": self.skinsnipe_api_key is not None,
            "telegram_bot_token": self.telegram_bot_token is not None,
        }

    def gate_summary(self) -> dict[str, str]:
        """Human-readable snapshot of the effective policy, for evidence artifacts."""
        return {
            "live_execution_enabled": str(self.live_execution_enabled),
            "max_daily_spend_minor": str(self.max_daily_spend_minor),
            "discovery_min_net_roi": str(self.discovery_min_net_roi),
            "final_min_net_roi": str(self.final_min_net_roi),
            "min_absolute_expected_profit_minor": str(self.min_absolute_expected_profit_minor),
            "max_contract_cost_minor": str(self.max_contract_cost_minor),
            "max_worst_case_loss_minor": str(self.max_worst_case_loss_minor),
            "max_partial_fill_exposure_minor": str(self.max_partial_fill_exposure_minor),
            "max_quote_age_seconds": str(self.max_quote_age_seconds),
            "max_expected_capital_days": str(self.max_expected_capital_days),
            "max_unvaluable_probability_mass": str(self.max_unvaluable_probability_mass),
            "base_currency": self.base_currency.value,
        }


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, with explicit overrides for tests."""
    return Settings(**overrides)  # type: ignore[arg-type]
