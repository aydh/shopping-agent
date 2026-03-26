"""add user_id columns and enable RLS

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-03-26 00:00:00.000000

"""
from typing import Sequence, Union
import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add user_id columns (nullable for backfill) ───────────────────────

    # orders
    op.add_column('orders', sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_orders_user_id', 'orders', ['user_id'])
    op.drop_constraint('orders_store_order_id_key', 'orders', type_='unique')
    op.create_unique_constraint('uq_user_store_order', 'orders', ['user_id', 'store_order_id'])

    # shopping_lists
    op.add_column('shopping_lists', sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_shopping_lists_user_id', 'shopping_lists', ['user_id'])

    # consumption_predictions
    op.add_column('consumption_predictions', sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_consumption_predictions_user_id', 'consumption_predictions', ['user_id'])
    op.drop_constraint('consumption_predictions_product_id_key', 'consumption_predictions', type_='unique')
    op.create_unique_constraint('uq_user_product_prediction', 'consumption_predictions', ['user_id', 'product_id'])

    # store_cookies
    op.add_column('store_cookies', sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.drop_index('ix_store_cookies_store', table_name='store_cookies')
    op.create_index('ix_store_cookies_store', 'store_cookies', ['store'], unique=False)
    op.create_unique_constraint('uq_user_store_cookies', 'store_cookies', ['user_id', 'store'])

    # ── 2. Backfill existing rows with MIGRATION_USER_ID if provided ─────────
    migration_user_id = os.environ.get('MIGRATION_USER_ID')
    if migration_user_id:
        conn = op.get_bind()
        for table in ('orders', 'shopping_lists', 'consumption_predictions', 'store_cookies'):
            conn.execute(
                sa.text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"),
                {'uid': migration_user_id},
            )

    # ── 3. Enable RLS on all tables ──────────────────────────────────────────
    personal_tables = [
        'orders', 'order_items', 'shopping_lists', 'shopping_list_items',
        'consumption_predictions', 'store_cookies',
    ]
    catalog_tables = ['products', 'product_matches', 'price_history']

    for table in personal_tables + catalog_tables:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')

    # ── 4. RLS policies — personal tables ───────────────────────────────────
    op.execute("""
        CREATE POLICY orders_isolation ON orders
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY shopping_lists_isolation ON shopping_lists
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY predictions_isolation ON consumption_predictions
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY store_cookies_isolation ON store_cookies
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)

    # Child tables — RLS joins through parent
    op.execute("""
        CREATE POLICY order_items_isolation ON order_items
            USING (order_id IN (SELECT id FROM orders WHERE user_id = auth.uid()))
            WITH CHECK (order_id IN (SELECT id FROM orders WHERE user_id = auth.uid()))
    """)
    op.execute("""
        CREATE POLICY shopping_list_items_isolation ON shopping_list_items
            USING (shopping_list_id IN (SELECT id FROM shopping_lists WHERE user_id = auth.uid()))
            WITH CHECK (shopping_list_id IN (SELECT id FROM shopping_lists WHERE user_id = auth.uid()))
    """)

    # ── 5. RLS policies — shared catalog (open for all authenticated) ────────
    for table in catalog_tables:
        op.execute(f"""
            CREATE POLICY {table}_open ON {table}
                FOR ALL TO authenticated
                USING (true)
                WITH CHECK (true)
        """)


def downgrade() -> None:
    # Drop RLS policies
    personal_tables = [
        'orders', 'order_items', 'shopping_lists', 'shopping_list_items',
        'consumption_predictions', 'store_cookies',
    ]
    catalog_tables = ['products', 'product_matches', 'price_history']

    policy_names = {
        'orders': 'orders_isolation',
        'order_items': 'order_items_isolation',
        'shopping_lists': 'shopping_lists_isolation',
        'shopping_list_items': 'shopping_list_items_isolation',
        'consumption_predictions': 'predictions_isolation',
        'store_cookies': 'store_cookies_isolation',
        'products': 'products_open',
        'product_matches': 'product_matches_open',
        'price_history': 'price_history_open',
    }
    for table, policy in policy_names.items():
        op.execute(f'DROP POLICY IF EXISTS {policy} ON {table}')

    for table in personal_tables + catalog_tables:
        op.execute(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY')

    # Restore constraints and remove columns
    op.drop_constraint('uq_user_store_order', 'orders', type_='unique')
    op.create_unique_constraint('orders_store_order_id_key', 'orders', ['store_order_id'])
    op.drop_index('ix_orders_user_id', table_name='orders')
    op.drop_column('orders', 'user_id')

    op.drop_index('ix_shopping_lists_user_id', table_name='shopping_lists')
    op.drop_column('shopping_lists', 'user_id')

    op.drop_constraint('uq_user_product_prediction', 'consumption_predictions', type_='unique')
    op.create_unique_constraint('consumption_predictions_product_id_key', 'consumption_predictions', ['product_id'])
    op.drop_index('ix_consumption_predictions_user_id', table_name='consumption_predictions')
    op.drop_column('consumption_predictions', 'user_id')

    op.drop_constraint('uq_user_store_cookies', 'store_cookies', type_='unique')
    op.drop_index('ix_store_cookies_store', table_name='store_cookies')
    op.create_index(op.f('ix_store_cookies_store'), 'store_cookies', ['store'], unique=True)
    op.drop_column('store_cookies', 'user_id')
