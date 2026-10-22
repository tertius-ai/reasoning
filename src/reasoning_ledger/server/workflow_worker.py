"""Exclusive installation-owned workflow reconciler; paid work uses child queues."""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading

from .openrouter import ProviderError
from .security import private_file
from .worker_poll import claim_work, database_poll
from .workflow_runs import WorkflowRuns


class WorkflowWorker:
    def __init__(self, state: WorkflowRuns, provider):
        self.state, self.provider = state, provider
        self._stop = threading.Event()
        self._thread = None
        self._lock = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self):
        if self._thread is not None:
            raise ValueError("workflow worker is already started")
        filename = self.state.store.data_dir / "workflow-worker.lock"
        private_file(filename, b"")
        lock = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock)
            raise ValueError("another workflow worker owns this installation") from exc
        self._lock = lock
        self._thread = threading.Thread(target=self._run, name="reasoning-workflows", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self._lock = None
            os.close(lock)
            raise

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise ValueError("workflow worker did not stop; its lock remains held")

    def _run(self):
        try:
            asyncio.run(self._dispatch())
        finally:
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    async def execute_once(self, run_id):
        action = self.state.advance(run_id)
        if action is None or self._stop.is_set():
            return
        task = asyncio.create_task(self.provider.quotes([action["model"]]))
        try:
            deadline = asyncio.get_running_loop().time() + 20
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                if self._stop.is_set():
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    self.state.advance(run_id, quote_action=action, quote_failed=True)
                    return
            try:
                quotes = task.result()
            except ProviderError:
                self.state.advance(run_id, quote_action=action, quote_failed=True)
            else:
                self.state.advance(run_id, quote_action=action, quotes=quotes)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _dispatch(self):
        cursor = 0
        while not self._stop.is_set():
            page = claim_work(lambda cursor=cursor: self.state.active(cursor))
            if not page:
                cursor = 0
                await asyncio.sleep(0.25)
                continue
            for row in page:
                if self._stop.is_set():
                    break
                with database_poll():
                    await self.execute_once(row["id"])
                cursor = row["sequence"]
            await asyncio.sleep(0.25)
