"""Migrate data from local SQLite database to Supabase (PostgreSQL).

Usage:
    python scripts/migrate_sqlite_to_supabase.py

Reads from data/shopping_agent.db and inserts into the Supabase DATABASE_URL
configured in .env. Safe to re-run — uses INSERT ... ON CONFLICT DO NOTHING.
"""
import asyncio
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = Path(__file__).parent.parent / "data" / "shopping_agent.db"

# Tables in dependency order (parents before children)
TABLES = [
    "orders",
    "products",
    "shopping_lists",
    "store_cookies",
    "order_items",
    "price_history",
    "product_matches",
    "consumption_predictions",
    "shopping_list_items",
]


_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def get_bool_columns(pg: asyncpg.Connection) -> dict[str, set[str]]:
    """Return {table: {bool_column, ...}} from the PostgreSQL information schema."""
    rows = await pg.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type = 'boolean'
        """
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(row["table_name"], set()).add(row["column_name"])
    return result


def coerce(value: object, col: str, bool_cols: set[str]) -> object:
    """Convert SQLite values to Python types compatible with asyncpg/PostgreSQL."""
    if col in bool_cols and isinstance(value, int):
        return bool(value)
    if not isinstance(value, str):
        return value
    if _DATETIME_RE.match(value):
        return datetime.fromisoformat(value)
    if _DATE_RE.match(value):
        return date.fromisoformat(value)
    return value


def get_pg_url(async_url: str) -> str:
    """Convert asyncpg URL to plain postgresql:// for asyncpg.connect."""
    return async_url.replace("postgresql+asyncpg://", "postgresql://")


async def migrate() -> None:
    database_url = os.environ["DATABASE_URL"]
    pg_url = get_pg_url(database_url)

    sqlite = sqlite3.connect(SQLITE_PATH)
    sqlite.row_factory = sqlite3.Row

    pg = await asyncpg.connect(pg_url)

    try:
        bool_cols_by_table = await get_bool_columns(pg)

        for table in TABLES:
            rows = sqlite.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            if not rows:
                print(f"  {table}: 0 rows, skipping")
                continue

            columns = rows[0].keys()
            col_list = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            sql = (
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                f"ON CONFLICT DO NOTHING"
            )

            bool_cols = bool_cols_by_table.get(table, set())
            records = [tuple(coerce(v, c, bool_cols) for v, c in zip(row, columns)) for row in rows]
            await pg.executemany(sql, records)
            print(f"  {table}: {len(records)} rows migrated")

        # Reset sequences so auto-increment starts after the migrated data
        print("\nResetting sequences...")
        for table in TABLES:
            result = await pg.fetchrow(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1)) FROM \"{table}\""
            )
            print(f"  {table}: sequence → {result['setval']}")

        print("\nDone.")
    finally:
        sqlite.close()
        await pg.close()


if __name__ == "__main__":
    asyncio.run(migrate())
