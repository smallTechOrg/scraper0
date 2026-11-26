"""structlog: one JSON object per line on stdout (Cloud Logging parses `severity`)."""
from __future__ import annotations

import logging
import sys

import structlog


def _severity(_, method, event):
    event["severity"] = method.upper() if method != "exception" else "ERROR"
    return event


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    shared = [structlog.contextvars.merge_contextvars, structlog.stdlib.add_log_level,
              structlog.stdlib.add_logger_name, structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp")]
    renderer = structlog.dev.ConsoleRenderer(colors=False) if fmt == "console" else structlog.processors.JSONRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared,
                                                    processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                                                                _severity, renderer])
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(processors=shared + [structlog.processors.format_exc_info,
                                             structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
                        logger_factory=structlog.stdlib.LoggerFactory(), cache_logger_on_first_use=True)
