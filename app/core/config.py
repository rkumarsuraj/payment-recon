"""Central application settings, loaded from environment variables (.env)."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/setu_db"

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_scheme(cls, v: str) -> str:
        """Two real deployment landmines, both handled here so the app works
        regardless of exactly what connection string the platform hands us:

        1. Some managed-Postgres providers (Heroku-style, and historically
           Render) inject `postgres://` URLs. SQLAlchemy 1.4+/2.x does not
           recognize "postgres" as a dialect name and raises
           `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres`
           at engine-creation time -- i.e. on the very first request.

        2. As of SQLAlchemy 2.1, a bare `postgresql://` (no driver suffix)
           defaults to the `psycopg` (v3) driver rather than `psycopg2` --
           confirmed by actually creating an engine with one during
           development, which failed with `ModuleNotFoundError: No module
           named 'psycopg'` even though psycopg2-binary was installed and
           working. Since this project only installs psycopg2-binary, the
           driver is pinned explicitly here rather than left to whatever
           SQLAlchemy's current default happens to be.

        Both cases are normalized to `postgresql+psycopg2://`. A URL that
        already names an explicit driver (e.g. `+psycopg2`, `+asyncpg`) is
        left untouched.
        """
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg2://" + v[len("postgresql://"):]
        return v

    # Reconciliation grace windows (hours)
    UNSETTLED_AFTER_HOURS: int = 48
    STUCK_AFTER_HOURS: int = 24

    # Request limits
    MAX_BATCH_SIZE: int = 1000
    MAX_PAGE_SIZE: int = 200
    DEFAULT_PAGE_SIZE: int = 50
    MAX_SUMMARY_PAGE_SIZE: int = 500
    DEFAULT_SUMMARY_PAGE_SIZE: int = 100

    # Clock-skew tolerance for incoming event timestamps
    FUTURE_TIMESTAMP_TOLERANCE_MINUTES: int = 5

    # Optional API key. If set, all endpoints under /events and /transactions
    # and /reconciliation require `X-API-Key: <value>`. Empty = disabled.
    API_KEY: str = ""

    LOG_LEVEL: str = "INFO"
    APP_VERSION: str = "1.0.0"


settings = Settings()
