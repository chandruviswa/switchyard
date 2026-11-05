from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from switchyard.db import Base

ROOT = Path(__file__).resolve().parent.parent


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_is_ok_when_the_database_is_up(client):
    assert client.get("/ready").status_code == 200


def test_request_id_is_generated(client):
    assert client.get("/health").headers["X-Request-ID"]


def test_request_id_is_passed_through(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


def test_docs_are_available(client):
    assert client.get("/openapi.json").status_code == 200


def test_migrations_match_the_models(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", url)

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    with engine.connect() as connection:
        drift = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    if drift:
        pytest.fail(f"Models and migrations differ: {drift}")
