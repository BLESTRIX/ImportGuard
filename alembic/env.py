from logging.config import fileConfig

import app.models  # noqa: F401
from alembic import context
from app.db.session import Base, engine

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)


def run():
    with engine().connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
