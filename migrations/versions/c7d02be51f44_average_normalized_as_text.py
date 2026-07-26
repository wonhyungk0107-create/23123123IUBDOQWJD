"""store the exact average normalised float as text

Live 17-digit floats averaged over ten inputs produce rationals whose numerator
and denominator overflow a 64-bit integer column. Text is lossless at any size,
consistent with the rule that exact values persist as text.

Revision ID: c7d02be51f44
Revises: a41c9f27e3b8
Create Date: 2026-07-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d02be51f44"
down_revision: str | None = "a41c9f27e3b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("average_normalized", sa.Text(), nullable=False, server_default="0")
        )
    op.execute(
        "UPDATE candidates SET average_normalized = "
        "CAST(average_normalized_numerator AS TEXT) || '/' || "
        "CAST(average_normalized_denominator AS TEXT)"
    )
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.drop_column("average_normalized_numerator")
        batch_op.drop_column("average_normalized_denominator")


def downgrade() -> None:
    # Structural only: the integer pair cannot faithfully hold every value the
    # text form can, which is the reason this migration exists. Downgraded rows
    # get 0/1 and the exact value survives only in the upgraded schema.
    with op.batch_alter_table("candidates", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "average_normalized_numerator",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "average_normalized_denominator",
                sa.BigInteger(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.drop_column("average_normalized")
