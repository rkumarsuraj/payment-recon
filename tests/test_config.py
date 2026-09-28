"""Regression tests for the two DATABASE_URL landmines found while
rehearsing deployment: `postgres://` isn't a recognized SQLAlchemy dialect,
and bare `postgresql://` defaults to the psycopg (v3) driver in SQLAlchemy
2.1+ rather than psycopg2, which is what this project actually installs."""
import importlib

import pytest


@pytest.mark.parametrize(
    "raw_scheme,expected_prefix",
    [
        ("postgres://", "postgresql+psycopg2://"),
        ("postgresql://", "postgresql+psycopg2://"),
        ("postgresql+psycopg2://", "postgresql+psycopg2://"),
    ],
)
def test_database_url_normalizes_to_psycopg2(raw_scheme, expected_prefix):
    import app.core.config as cfg

    importlib.reload(cfg)
    settings = cfg.Settings(DATABASE_URL=f"{raw_scheme}user:pass@host:5432/db")
    assert settings.DATABASE_URL.startswith(expected_prefix)


def test_normalized_url_actually_resolves_to_psycopg2_dialect():
    from sqlalchemy import create_engine

    import app.core.config as cfg

    importlib.reload(cfg)
    settings = cfg.Settings(DATABASE_URL="postgres://user:pass@host:5432/db")
    engine = create_engine(settings.DATABASE_URL)
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg2"
