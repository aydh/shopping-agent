"""add_price_history_composite_index

Revision ID: a1b2c3d4e5f6
Revises: 4054b9e11b62
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4054b9e11b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Composite index covering both the product_id filter and recorded_at sort/range.
    # Speeds up price chart queries (GROUP BY product_id, date(recorded_at))
    # and the daily upsert check (WHERE product_id = ? AND recorded_at BETWEEN ? AND ?).
    op.create_index(
        'ix_price_history_product_id_recorded_at',
        'price_history',
        ['product_id', 'recorded_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_price_history_product_id_recorded_at', table_name='price_history')
