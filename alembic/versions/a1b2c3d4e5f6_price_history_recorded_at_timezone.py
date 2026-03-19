"""price_history recorded_at timezone-aware

Revision ID: a1b2c3d4e5f6
Revises: 3f8a92b1c047
Create Date: 2026-03-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3f8a92b1c047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert price_history.recorded_at to TIMESTAMP WITH TIME ZONE.

    Existing naive values are interpreted as UTC (the server timezone on
    Supabase), so AT TIME ZONE 'UTC' is the correct cast.
    """
    op.alter_column(
        "price_history",
        "recorded_at",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(timezone=False),
        existing_nullable=False,
        postgresql_using="recorded_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    op.alter_column(
        "price_history",
        "recorded_at",
        type_=sa.DateTime(timezone=False),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="recorded_at AT TIME ZONE 'UTC'",
    )
