"""prospect confirmations: the estimate-versus-executable calibration dataset

Revision ID: e19f4a86d2c7
Revises: c7d02be51f44
Create Date: 2026-07-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e19f4a86d2c7"
down_revision: str | None = "c7d02be51f44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prospect_confirmations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("collection_id", sa.String(length=256), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("input_rarity", sa.String(length=32), nullable=False),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("input_wear", sa.String(length=32), nullable=False),
        sa.Column("estimated_cost_minor", sa.BigInteger(), nullable=False),
        sa.Column("estimated_output_value_minor", sa.BigInteger(), nullable=False),
        sa.Column("estimated_ev_minor", sa.BigInteger(), nullable=False),
        sa.Column("estimated_roi", sa.String(length=64), nullable=False),
        sa.Column("estimated_ask_haircut", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("listings_found", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(length=32), nullable=True),
        sa.Column("exact_cost_minor", sa.BigInteger(), nullable=True),
        sa.Column("exact_output_value_minor", sa.BigInteger(), nullable=True),
        sa.Column("exact_ev_minor", sa.BigInteger(), nullable=True),
        sa.Column("exact_roi", sa.String(length=64), nullable=True),
        sa.Column("rejection_reasons", sa.String(length=512), nullable=True),
        sa.Column("detail", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("prospect_confirmations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_prospect_confirmations_confirmed_at"), ["confirmed_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_prospect_confirmations_collection_id"),
            ["collection_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_prospect_confirmations_quality"), ["quality"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_prospect_confirmations_status"), ["status"], unique=False
        )


def downgrade() -> None:
    op.drop_table("prospect_confirmations")
