"""One installation-owned, bounded executor for beta and workflow model responses."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from .inference_store import MAX_OUTPUT_CHARS, InferenceStore
from .inference_timing import Spans
from .openrouter import OpenRouter, ProviderError, Quote
from .security import private_file
from .store import StoreError
from .worker_poll import claim_work, database_poll

logger = logging.getLogger(__name__)
DEFAULT_AGENT_CONCURRENCY = 8
SETTLEMENT_INTERVAL = 15


class InferenceWorker:
    def __init__(self, state: InferenceStore, provider: OpenRouter, *, concurrency: int = DEFAULT_AGENT_CONCURRENCY, timeout: float = 1800):
        if not 1 <= concurrency <= 256 or not 1 <= timeout <= 3600:
            raise ValueError("inference worker concurrency/timeout out of range")
        self.state, self.provider = state, provider
        self.concurrency, self.timeout = concurrency, timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock: int | None = None
        self.compute, self.workflows = None, None

    def start(self) -> None:
        if self._thread is not None:
            raise ValueError("inference worker is already started")
        filename = self.state.store.data_dir / "inference-worker.lock"
        private_file(filename, b"")
        lock = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock)
            raise ValueError("another inference worker owns this installation") from exc
        self._lock = lock
        try:
            self.state.recover()
            self._thread = threading.Thread(target=self._run, name="reasoning-inference", daemon=True)
            self._thread.start()
        except BaseException:
            os.close(lock)
            self._lock = None
            self._thread = None
            raise

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency + 1, thread_name_prefix="reasoning-agent") as pool:
                async def dispatch():
                    asyncio.get_running_loop().set_default_executor(pool)
                    await self._dispatch()

                asyncio.run(dispatch())
        finally:
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise ValueError("inference worker did not stop; its lock remains held")

    async def _dispatch(self) -> None:
        tasks: set[asyncio.Task] = set()
        maintenance = asyncio.create_task(self._maintain())
        settlement = asyncio.create_task(self._settle())
        try:
            while not self._stop.is_set():
                for background in (maintenance, settlement):
                    if background.done():
                        background.result()
                finished = {task for task in tasks if task.done()}
                for task in finished:
                    task.result()
                tasks -= finished
                while len(tasks) < self.concurrency and not self._stop.is_set():
                    row = await asyncio.to_thread(claim_work, self.state.claim)
                    if row is None:
                        break
                    tasks.add(asyncio.create_task(self._execute(row, spans=Spans("worker"))))
                await asyncio.sleep(0.1)
        finally:
            maintenance.cancel()
            settlement.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(maintenance, settlement, *tasks, return_exceptions=True)

    async def _maintain(self) -> None:
        """Keep catalog-dependent background admission off the runnable queue path."""
        from .research_goals import ResearchGoals
        from .research_reviews import ResearchReviews
        from .topic_agents import TopicAgents

        while not self._stop.is_set():
            try:
                await asyncio.to_thread(TopicAgents(self.state, self.compute).dispatch)
            except Exception:
                logger.warning("topic-agent handoffs unavailable; retained state will be retried")
            with database_poll():
                await ResearchReviews(self.state).dispatch(self.provider, stopped=self._stop.is_set)
                await ResearchGoals(self.state).dispatch(self.provider, stopped=self._stop.is_set)
            await asyncio.sleep(1)

    async def _settle(self) -> None:
        """Settle uncertain charges from provider generation records; never a model request."""
        from .inference_settlement import InferenceSettlement

        while not self._stop.is_set():
            try:
                await InferenceSettlement(self.state).run(self.provider)
            except Exception:
                logger.warning("provider charge lookup unavailable; uncertain charges stay held and are retried on schedule")
            await asyncio.sleep(SETTLEMENT_INTERVAL)

    async def _execute(self, row: dict, *, spans: Spans | None = None) -> None:
        with self.state.store.transaction() as db:
            research = db.execute("SELECT 1 FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()
            topic_kind = db.execute("SELECT kind FROM topics WHERE id=?", (row["topic_id"],)).fetchone()[0]
        if research or topic_kind == "gamma":
            from .research_agent import ResearchAgent

            agent = ResearchAgent(
                self.state,
                self.provider,
                row,
                Quote(**json.loads(row["quote_json"])),
                compute=self.compute,
                workflows=self.workflows,
                stopped=self._stop.is_set,
                timeout=self.timeout,
                spans=spans,
            )
            task = asyncio.create_task(asyncio.to_thread(agent.execute))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
            return
        text, generation, usage, finish_reason = "", None, None, None
        call_id, diagnostics = None, None
        submitted, reason = False, "worker_interrupted"
        last_flush = 0.0
        first_token = False

        async def consume() -> None:
            nonlocal text, generation, usage, submitted, last_flush, finish_reason, call_id, first_token
            key = self.state.execution_key(row)
            quote = Quote(**json.loads(row["quote_json"]))
            parameters = json.loads(row["parameters_json"])
            from .usage_store import UsageStore

            # Accepted beta requests from an earlier release keep their captured
            # context; the absence of a research snapshot does not make them workflows.
            call_id = UsageStore(self.state.store).begin_call(row, quote, purpose="workflow" if topic_kind == "gamma" else "answer")
            submitted = True
            async for chunk in self.provider.stream(key, quote, json.loads(row["context_json"]), parameters["max_completion_tokens"]):
                if chunk.text and not first_token:
                    UsageStore(self.state.store).first_token(call_id)
                    first_token = True
                text += chunk.text
                if len(text) > MAX_OUTPUT_CHARS:
                    raise ProviderError("output_limit")
                if generation and chunk.generation_id and generation != chunk.generation_id:
                    raise ProviderError("provider_protocol")
                generation = chunk.generation_id or generation
                usage = chunk.usage or usage
                finish_reason = chunk.finish_reason or finish_reason
                if time.monotonic() - last_flush >= 0.2 or chunk.usage:
                    if not self.state.progress(row, text, generation, usage):
                        raise ProviderError("execution_fenced")
                    last_flush = time.monotonic()

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + self.timeout
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                reason = self.state.stopped(row) or ""
                if reason or time.monotonic() >= deadline or self._stop.is_set():
                    reason = reason or ("worker_interrupted" if self._stop.is_set() else "execution_timeout")
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    self.state.progress(row, text, generation, usage)
                    self.state.finish(
                        row,
                        "interrupted" if reason == "worker_interrupted" else "cancelled",
                        usage=usage,
                        no_charge=not submitted,
                        error=reason,
                    )
                    return
            await task
            self.state.progress(row, text, generation, usage)
            if usage is None:
                raise ProviderError("usage_unavailable")
            if finish_reason not in {None, "stop"}:
                raise ProviderError("output_token_limit" if finish_reason == "length" else "provider_" + finish_reason)
            if not text.strip():
                raise ProviderError("empty_response")
            self.state.finish(row, "succeeded", usage=usage)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError, ProviderError, StoreError):
                await task
            self.state.progress(row, text, generation, usage)
            self.state.finish(row, "interrupted", usage=usage, no_charge=not submitted, error="worker_interrupted")
        except (ProviderError, StoreError) as exc:
            task.cancel()
            with suppress(asyncio.CancelledError, ProviderError, StoreError):
                await task
            # No upstream exception body, prompt, header, or key is a public error.
            self.state.progress(row, text[:MAX_OUTPUT_CHARS], generation, usage)
            self.state.finish(
                row,
                "failed",
                usage=usage,
                no_charge=not submitted or isinstance(exc, ProviderError) and exc.no_charge,
                error=exc.code if isinstance(exc, ProviderError) else "execution_not_authorized",
            )
            diagnostics = exc.diagnostics if isinstance(exc, ProviderError) else None
        finally:
            if call_id is not None:
                from .usage_store import UsageStore

                with self.state.store.transaction() as db:
                    outcome = dict(db.execute("SELECT * FROM llm_runs WHERE id=?", (row["id"],)).fetchone())
                UsageStore(self.state.store).finish_call(
                    call_id,
                    status=outcome["status"],
                    usage=usage,
                    provider_generation_id=generation,
                    diagnostics=diagnostics,
                    no_charge=outcome["accounting_state"] == "released",
                    error_code=outcome["error_code"],
                )
