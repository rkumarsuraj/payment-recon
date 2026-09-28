"""Pytest fixtures.

Tests run against a real Postgres database (setu_test), never SQLite --
the schema leans on Postgres-specific features (generated columns,
ON CONFLICT ... DO UPDATE with EXCLUDED, FILTER, partial indexes, enums)
that SQLite cannot express, so testing against SQLite would be testing a
different system.

Set TEST_DATABASE_URL to override the default; otherwise assumes a local
`setu_test` database already exists with the schema migrated
(`alembic upgrade head` run against it once before the first test run).
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/setu_test"
)

# Point the app at the test database *before* importing it, so app.core.db's
# module-level engine binds to setu_test rather than the dev database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

_test_engine = create_engine(TEST_DATABASE_URL, future=True)
_TestSession = sessionmaker(bind=_test_engine, future=True)


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate all tables before every test for full isolation."""
    with _test_engine.begin() as conn:
        conn.execute(text("TRUNCATE payment_events, transactions, merchants, request_log CASCADE;"))
    yield


@pytest.fixture
def db():
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)
