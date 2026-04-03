"""consolidated schema

Revision ID: d0e1f2a3b4c5
Revises:
Create Date: 2026-04-03 00:00:00.000000

Consolidates all previous migrations into a single baseline:
  815267ccee89 initial_schema
  3f8a92b1c047 add_performance_indexes
  a1b2c3d4e5f6 price_history_recorded_at_timezone
  b1c2d3e4f5a6 add_user_id_and_rls
  c1d2e3f4a5b6 add_user_product_preferences
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Tables ───────────────────────────────────────────────────────────────

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=False),
        sa.Column("store_order_id", sa.String(length=64), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("total_amount", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("store_name", sa.String(length=256), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "store_order_id", name="uq_user_store_order"),
    )
    op.create_index("ix_orders_store", "orders", ["store"])
    op.create_index("ix_orders_user_id", "orders", ["user_id"])

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=False),
        sa.Column("store_product_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("brand", sa.String(length=256), nullable=True),
        sa.Column("category", sa.String(length=256), nullable=True),
        sa.Column("unit_size", sa.String(length=64), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("unit_price_measure", sa.String(length=32), nullable=True),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("product_url", sa.String(length=1024), nullable=True),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store", "store_product_id", name="uq_store_product"),
    )
    op.create_index("ix_products_store", "products", ["store"])

    op.create_table(
        "shopping_lists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Enum("DRAFT", "CONFIRMED", "ORDERED", name="liststatus"), nullable=False),
        sa.Column("preferred_store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=True),
        sa.Column("estimated_total", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shopping_lists_user_id", "shopping_lists", ["user_id"])

    op.create_table(
        "store_cookies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=False),
        sa.Column("cookies_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "store", name="uq_user_store_cookies"),
    )
    op.create_index("ix_store_cookies_store", "store_cookies", ["store"])

    op.create_table(
        "consumption_predictions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("avg_purchase_interval_days", sa.Float(), nullable=False),
        sa.Column("avg_quantity_per_purchase", sa.Float(), nullable=False),
        sa.Column("estimated_daily_consumption", sa.Float(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("last_purchased_date", sa.Date(), nullable=False),
        sa.Column("predicted_runout_date", sa.Date(), nullable=False),
        sa.Column("next_purchase_date", sa.Date(), nullable=False),
        sa.Column("purchase_count", sa.Integer(), nullable=False),
        sa.Column("last_purchase_quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_purchase_store", sa.String(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "product_id", name="uq_user_product_prediction"),
    )
    op.create_index("ix_consumption_predictions_user_id", "consumption_predictions", ["user_id"])

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_paid", sa.Float(), nullable=False),
        sa.Column("was_substituted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_product_id", "order_items", ["product_id"])

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_history_product_id", "price_history", ["product_id"])

    op.create_table(
        "product_matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_a_id", sa.Integer(), nullable=False),
        sa.Column("product_b_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False),
        sa.Column("is_rejected", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_a_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["product_b_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_a_id", "product_b_id", name="uq_product_match"),
    )
    op.create_index("ix_product_matches_product_b_id", "product_matches", ["product_b_id"])
    op.create_index("ix_product_matches_is_rejected", "product_matches", ["is_rejected"])

    op.create_table(
        "shopping_list_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shopping_list_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("coles_price", sa.Float(), nullable=True),
        sa.Column("woolworths_price", sa.Float(), nullable=True),
        sa.Column("chosen_store", sa.Enum("COLES", "WOOLWORTHS", name="store"), nullable=True),
        sa.Column("is_user_added", sa.Boolean(), nullable=False),
        sa.Column("is_removed", sa.Boolean(), nullable=False),
        sa.Column("is_ordered", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["shopping_list_id"], ["shopping_lists.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shopping_list_items_shopping_list_id", "shopping_list_items", ["shopping_list_id"])

    op.create_table(
        "user_product_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("exclude_from_predictions", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "product_id", name="uq_user_product_pref"),
    )
    op.create_index("ix_user_product_preferences_user_id", "user_product_preferences", ["user_id"])
    op.create_index("ix_user_product_preferences_product_id", "user_product_preferences", ["product_id"])

    # ── Row Level Security ───────────────────────────────────────────────────

    personal_tables = [
        "orders",
        "order_items",
        "shopping_lists",
        "shopping_list_items",
        "consumption_predictions",
        "store_cookies",
        "user_product_preferences",
    ]
    catalog_tables = ["products", "product_matches", "price_history"]

    for table in personal_tables + catalog_tables:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

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
    op.execute("""
        CREATE POLICY user_product_preferences_isolation ON user_product_preferences
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid())
    """)

    for table in catalog_tables:
        op.execute(f"""
            CREATE POLICY {table}_open ON {table}
                FOR ALL TO authenticated
                USING (true)
                WITH CHECK (true)
        """)


def downgrade() -> None:
    # Drop RLS policies
    policy_map = {
        "orders": "orders_isolation",
        "order_items": "order_items_isolation",
        "shopping_lists": "shopping_lists_isolation",
        "shopping_list_items": "shopping_list_items_isolation",
        "consumption_predictions": "predictions_isolation",
        "store_cookies": "store_cookies_isolation",
        "user_product_preferences": "user_product_preferences_isolation",
        "products": "products_open",
        "product_matches": "product_matches_open",
        "price_history": "price_history_open",
    }
    for table, policy in policy_map.items():
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    all_tables = list(policy_map.keys())
    for table in all_tables:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop tables in reverse dependency order
    op.drop_index("ix_user_product_preferences_product_id", table_name="user_product_preferences")
    op.drop_index("ix_user_product_preferences_user_id", table_name="user_product_preferences")
    op.drop_table("user_product_preferences")

    op.drop_index("ix_shopping_list_items_shopping_list_id", table_name="shopping_list_items")
    op.drop_table("shopping_list_items")

    op.drop_index("ix_product_matches_is_rejected", table_name="product_matches")
    op.drop_index("ix_product_matches_product_b_id", table_name="product_matches")
    op.drop_table("product_matches")

    op.drop_index("ix_price_history_product_id", table_name="price_history")
    op.drop_table("price_history")

    op.drop_index("ix_order_items_product_id", table_name="order_items")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")

    op.drop_index("ix_consumption_predictions_user_id", table_name="consumption_predictions")
    op.drop_table("consumption_predictions")

    op.drop_index("ix_store_cookies_store", table_name="store_cookies")
    op.drop_table("store_cookies")

    op.drop_index("ix_shopping_lists_user_id", table_name="shopping_lists")
    op.drop_table("shopping_lists")

    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_index("ix_orders_store", table_name="orders")
    op.drop_table("orders")

    op.drop_index("ix_products_store", table_name="products")
    op.drop_table("products")

    op.execute("DROP TYPE IF EXISTS liststatus")
    op.execute("DROP TYPE IF EXISTS store")
