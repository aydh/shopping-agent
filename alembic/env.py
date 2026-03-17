from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so their tables are registered on Base.metadata
import src.shopping_agent.models  # noqa: F401, E402
from src.shopping_agent.models.base import Base  # noqa: E402
from src.shopping_agent.config import settings  # noqa: E402

target_metadata = Base.metadata

# Use the app's database URL, stripping the async driver prefix so alembic
# can use a synchronous connection (aiosqlite → sqlite).
def get_url() -> str:
    url = str(settings.database_url)
    return url.replace("sqlite+aiosqlite", "sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite ALTER TABLE support
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite ALTER TABLE support
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
