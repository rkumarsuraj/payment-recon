"""Minimal structured (JSON-ish) logging configuration."""
import logging
import sys

from app.core.config import settings


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)
    root.handlers = [handler]
