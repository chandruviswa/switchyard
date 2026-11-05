import os

from alembic import context
from sqlalchemy import create_engine

from switchyard import models  # noqa: F401  registers the tables on Base
from switchyard.config import get_settings
from switchyard.db import Base

# ALEMBIC_DATABASE_URL lets a test migrate a scratch database.
url = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url
target_metadata = Base.metadata

connectable = create_engine(url)
with connectable.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()
connectable.dispose()
