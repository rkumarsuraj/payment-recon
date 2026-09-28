"""FastAPI application entrypoint."""
from fastapi import FastAPI, status
from sqlalchemy import text

from app.api import events, reconciliation, transactions
from app.core.config import settings
from app.core.db import engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(
    title="Setu Assignment — Partner Reconciliation Service",
    version=settings.APP_VERSION,
    description=(
        "Ingests payment lifecycle events, maintains transaction and "
        "reconciliation state, and exposes APIs for operations teams."
    ),
)

register_exception_handlers(app)

app.include_router(events.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            event_count = conn.execute(text("SELECT count(*) FROM payment_events")).scalar()
        return {
            "status": "ok",
            "database": "ok",
            "version": settings.APP_VERSION,
            "event_count": event_count,
        }
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "unreachable", "version": settings.APP_VERSION},
        )
