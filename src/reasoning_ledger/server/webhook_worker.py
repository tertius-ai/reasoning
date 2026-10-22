"""One exclusively locked, bounded webhook sender per installation."""

import asyncio
import fcntl
import os
import threading

from .security import private_file
from .webhook_store import _eligible
from .webhook_transport import WebhookError, WebhookTransport
from .worker_poll import claim_work


class WebhookWorker:
    def __init__(self, state, transport=None):
        self.state = state
        self.transport = transport or WebhookTransport()
        self._stop = threading.Event()
        self._thread = None
        self._lock = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self):
        if self._thread is not None or self._stop.is_set():
            raise ValueError("webhook worker already started or stopped")
        filename = self.state.store.data_dir / "webhook-worker.lock"
        private_file(filename, b"")
        lock = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state.recover()
            self._lock = lock
            self._thread = threading.Thread(target=self._run, name="reasoning-webhooks", daemon=True)
            self._thread.start()
        except BaseException:
            self._lock = None
            self._thread = None
            os.close(lock)
            raise

    def authorized(self, row):
        if self._stop.is_set():
            return False
        with self.state.store.transaction() as db:
            current = db.execute("SELECT * FROM bot_webhook_deliveries WHERE id=?", (row["id"],)).fetchone()
            hook = db.execute("SELECT * FROM bot_webhooks WHERE id=?", (row["webhook_id"],)).fetchone()
            return (
                current is not None
                and current["state"] == "sending"
                and current["attempts"] == row["attempts"]
                and _eligible(db, hook, row["topic_id"])
            )

    async def execute_once(self):
        row = claim_work(self.state.claim)
        if row is None:
            return False
        try:
            await self.transport.send(row, self.authorized)
        except WebhookError as exc:
            self.state.finish(row, exc.state, exc.code)
        except Exception:
            # Adapter error text can contain user-controlled URLs, bodies, or secrets.
            self.state.finish(row, "unknown", "worker_interrupted")
        except BaseException:
            self.state.finish(row, "unknown", "worker_interrupted")
            raise
        else:
            self.state.finish(row, "accepted")
        return True

    def _run(self):
        try:
            while not self._stop.is_set():
                asyncio.run(self.execute_once())
                self._stop.wait(0.25)
        finally:
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=25)
            if self._thread.is_alive():
                raise ValueError("webhook worker did not stop; its lock remains held")
