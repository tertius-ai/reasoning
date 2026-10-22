"""Retryable database contention at background queue admission boundaries."""

import logging
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def database_busy(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.OperationalError) and isinstance(code, int) and code & 255 in {
        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
    }


@contextmanager
def database_poll():
    """Leave rolled-back admission work for the next poll; never wrap delivery."""
    try:
        yield
    except sqlite3.OperationalError as error:
        if not database_busy(error):
            raise
        # Exception text can contain private database values or paths.
        logger.warning("Background database poll is busy; retained work will be retried")


def claim_work(claim):
    with database_poll():
        return claim()
    return None
