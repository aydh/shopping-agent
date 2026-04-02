"""add user_product_preferences table and drop is_hidden from products

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-04-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Create user_product_preferences table ─────────────────────────────
    op.create_table(
        'user_product_preferences',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('exclude_from_predictions', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['product_id'], ['products.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'product_id', name='uq_user_product_pref'),
    )
    op.create_index('ix_user_product_preferences_user_id', 'user_product_preferences', ['user_id'])
    op.create_index('ix_user_product_preferences_product_id', 'user_product_preferences', ['product_id'])

    # ── 2. Enable RLS on the new table ───────────────────────────────────────
    op.execute('ALTER TABLE user_product_preferences ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE user_product_preferences FORCE ROW LEVEL SECURITY')
    op.execute("""
        CREATE POLICY user_product_preferences_isolation ON user_product_preferences
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)

    # ── 3. Drop is_hidden from products ──────────────────────────────────────
    op.drop_index('ix_products_is_hidden', table_name='products')
    op.drop_column('products', 'is_hidden')


def downgrade() -> None:
    # Restore is_hidden on products (all set to False — original per-user data is lost)
    op.add_column('products', sa.Column('is_hidden', sa.Boolean(), nullable=False, server_default='false'))
    op.create_index('ix_products_is_hidden', 'products', ['is_hidden'])

    # Drop RLS policy and table
    op.execute('DROP POLICY IF EXISTS user_product_preferences_isolation ON user_product_preferences')
    op.execute('ALTER TABLE user_product_preferences DISABLE ROW LEVEL SECURITY')
    op.drop_index('ix_user_product_preferences_product_id', table_name='user_product_preferences')
    op.drop_index('ix_user_product_preferences_user_id', table_name='user_product_preferences')
    op.drop_table('user_product_preferences')
