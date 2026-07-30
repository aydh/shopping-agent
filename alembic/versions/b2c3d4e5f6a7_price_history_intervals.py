"""price_history_intervals

Add last_seen_at to price_history and collapse runs of consecutive
identical prices into a single interval row per price regime:
recorded_at = first observation, last_seen_at = last observation.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'price_history',
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE price_history SET last_seen_at = recorded_at")

    # Collapse each run of consecutive identical prices (per product) into a
    # single row keeping the run's first observation, with last_seen_at set to
    # the run's last observation. Gaps-and-islands via row_number difference.
    op.execute("""
        WITH runs AS (
            SELECT id, product_id, price, recorded_at,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY recorded_at, id)
                 - ROW_NUMBER() OVER (PARTITION BY product_id, price ORDER BY recorded_at, id) AS grp
            FROM price_history
        ),
        agg AS (
            SELECT product_id, price, grp,
                   MAX(recorded_at) AS last_at,
                   (ARRAY_AGG(id ORDER BY recorded_at, id))[1] AS keep_id
            FROM runs
            GROUP BY product_id, price, grp
        )
        UPDATE price_history ph
        SET last_seen_at = agg.last_at
        FROM agg
        WHERE ph.id = agg.keep_id
    """)
    op.execute("""
        WITH runs AS (
            SELECT id, product_id, price, recorded_at,
                   ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY recorded_at, id)
                 - ROW_NUMBER() OVER (PARTITION BY product_id, price ORDER BY recorded_at, id) AS grp
            FROM price_history
        ),
        agg AS (
            SELECT (ARRAY_AGG(id ORDER BY recorded_at, id))[1] AS keep_id
            FROM runs
            GROUP BY product_id, price, grp
        )
        DELETE FROM price_history ph
        WHERE ph.id NOT IN (SELECT keep_id FROM agg)
    """)

    op.alter_column(
        'price_history', 'last_seen_at',
        nullable=False,
        server_default=sa.text('now()'),
    )


def downgrade() -> None:
    # Lossy: the interior observations deleted by upgrade cannot be restored.
    op.drop_column('price_history', 'last_seen_at')
