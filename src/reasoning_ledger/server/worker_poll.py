"""Retryable database contention at background queue admission and run completion boundaries."""

import logging
import sqlite3
import time
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


def persist_completion(finish, *, stopped, warning):
    """Run an atomic completion transaction again after SQLite contention, with the same outcome and usage.

    Only the rolled-back transaction is retried; nothing calls the model again. Returns what ``finish`` returns, or
    None when retries stop because the worker is stopping: the run then stays running for restart recovery. The wait
    between attempts is bounded; the number of attempts is not, since the outcome would otherwise be lost.
    """
    delay, warned = 0.1, False
    while True:
        try:
            return finish()
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise
            if not warned:
                # Exception text can contain private database values or paths.
                logger.warning(warning)
                warned = True
            if stopped():
                return None
            time.sleep(delay)
            if stopped():
                return None
            delay = min(1.0, delay * 2)


def claim_work(claim):
    with database_poll():
        return claim()
    return None
