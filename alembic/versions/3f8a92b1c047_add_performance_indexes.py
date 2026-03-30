"""add performance indexes

Revision ID: 3f8a92b1c047
Revises: 815267ccee89
Create Date: 2026-03-18 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3f8a92b1c047'
down_revision: Union[str, Sequence[str], None] = '815267ccee89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(op.f('ix_products_store'), 'products', ['store'])
    op.create_index(op.f('ix_products_is_hidden'), 'products', ['is_hidden'])
    op.create_index(op.f('ix_product_matches_product_b_id'), 'product_matches', ['product_b_id'])
    op.create_index(op.f('ix_product_matches_is_rejected'), 'product_matches', ['is_rejected'])
    op.create_index(op.f('ix_price_history_product_id'), 'price_history', ['product_id'])
    op.create_index(op.f('ix_orders_store'), 'orders', ['store'])
    op.create_index(op.f('ix_order_items_order_id'), 'order_items', ['order_id'])
    op.create_index(op.f('ix_order_items_product_id'), 'order_items', ['product_id'])
    op.create_index(op.f('ix_shopping_list_items_shopping_list_id'), 'shopping_list_items', ['shopping_list_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_shopping_list_items_shopping_list_id'), table_name='shopping_list_items')
    op.drop_index(op.f('ix_order_items_product_id'), table_name='order_items')
    op.drop_index(op.f('ix_order_items_order_id'), table_name='order_items')
    op.drop_index(op.f('ix_orders_store'), table_name='orders')
    op.drop_index(op.f('ix_price_history_product_id'), table_name='price_history')
    op.drop_index(op.f('ix_product_matches_is_rejected'), table_name='product_matches')
    op.drop_index(op.f('ix_product_matches_product_b_id'), table_name='product_matches')
    op.drop_index(op.f('ix_products_is_hidden'), table_name='products')
    op.drop_index(op.f('ix_products_store'), table_name='products')
