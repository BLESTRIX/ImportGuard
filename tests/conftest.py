import io
import os
import re

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command

# Destructive resets are restricted to a dedicated *_test database.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://importguard:local-password@127.0.0.1:5439/importguard_test",
)
os.environ["APP_ENV"] = "test"
os.environ["ENABLE_FAILURE_INJECTION"] = "true"

from app.core.config import settings  # noqa: E402
from app.db.session import Base, engine, session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.storage import s3  # noqa: E402


@pytest.fixture(scope="session")
def database():
    url = make_url(settings().database_url)
    if not url.database or not re.fullmatch(r"[a-zA-Z0-9_]+_test", url.database):
        pytest.fail("TEST_DATABASE_URL must use a database name ending in _test")
    admin = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    with admin.connect() as connection:
        if not connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        ):
            connection.exec_driver_sql(f'CREATE DATABASE "{url.database}"')
    admin.dispose()
    command.upgrade(Config("alembic.ini"), "head")
    yield
    engine().dispose()


@pytest.fixture
def db(database):
    with engine().begin() as connection:
        names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        connection.exec_driver_sql(f"TRUNCATE {names} CASCADE")
    with session_factory()() as session:
        yield session


@pytest.fixture
def storage(monkeypatch):
    objects = {}
    monkeypatch.setattr(
        s3, "put", lambda key, stream, size: objects.__setitem__(key, stream.read())
    )
    monkeypatch.setattr(s3, "open_source", lambda key: io.BytesIO(objects[key]))
    monkeypatch.setattr(s3, "delete", lambda key: objects.pop(key, None))
    return objects


@pytest.fixture
def client(db, storage):
    with TestClient(app, raise_server_exceptions=False) as value:
        value.get("/login")
        value.headers["X-CSRF-Token"] = value.cookies["ig_csrf"]
        yield value


@pytest.fixture
def admin(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "admin@example.com",
            "password": "correct horse battery staple",
            "organization_name": "North Shop",
        },
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def other(db, storage):
    with TestClient(app, raise_server_exceptions=False) as value:
        value.get("/login")
        value.headers["X-CSRF-Token"] = value.cookies["ig_csrf"]
        response = value.post(
            "/api/v1/auth/register",
            json={
                "email": "other@example.com",
                "password": "correct horse battery staple",
                "organization_name": "South Shop",
            },
        )
        assert response.status_code == 201, response.text
        yield value
