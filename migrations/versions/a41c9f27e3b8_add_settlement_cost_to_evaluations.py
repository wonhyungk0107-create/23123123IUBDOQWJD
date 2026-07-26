"""add settlement cost to candidate evaluations

Revision ID: a41c9f27e3b8
Revises: db761825af69
Create Date: 2026-07-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a41c9f27e3b8"
down_revision: str | None = "db761825af69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows predate the crypto settlement rail, so their settlement share
    # really was zero; the server default records that fact rather than guessing.
    with op.batch_alter_table("candidate_evaluations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "settlement_cost_minor",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("candidate_evaluations", schema=None) as batch_op:
        batch_op.drop_column("settlement_cost_minor")
