"""Authenticated per-topic Server-Sent Events for an open conversation.

One stream per open topic replaces the browser's idle polling of messages, beta model runs,
agent threads, the goal and bot invocations. A single watcher per topic and process reads a
cheap change signature from the database (every 250 ms while work is active, every second
otherwise, and at once after a write through this server); when a channel's signature changes,
each subscriber re-reads that channel with its own identity through the same access-checked
store methods as the REST routes and sends the result:

* ``messages``: watermarks ``{"sequence", "updates"}`` (latest message sequence and message
  revision sequence). The browser fetches new messages and edits with its own cursors.
* ``runs`` (beta topics): ``{"order": [run IDs], "runs": [changed runs]}``. A changed run whose
  output only grew carries ``output_append`` instead of the full ``output_text``.
* ``agents``, ``goal`` (beta topics) and ``bot-runs``: complete snapshots, sent when they change.

A direct conversation has the same stream with only the ``messages`` channel, read through the
participant-only direct-message access check.

A (re)connected stream starts with every channel, so catch-up needs no server-side history.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import signal
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse

from .store import StoreError

TOPIC_PATH = re.compile(r"/(?:topics|direct-conversations)/([a-f0-9]{32})(?:/|$)")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
BETA_CHANNELS = ("messages", "runs", "agents", "goal", "bot-runs")
CHANNELS = ("messages", "bot-runs")
DIRECT_CHANNELS = ("messages",)
STREAM_CHANNELS = {"topic": CHANNELS, "beta": BETA_CHANNELS, "direct": DIRECT_CHANNELS}


@dataclass(frozen=True)
class StreamLimits:
    total: int = 256
    per_user: int = 8
    heartbeat: float = 15.0
    recheck: float = 30.0
    lifetime: float = 600.0
    active_interval: float = 0.25
    idle_interval: float = 1.0
    minimum_interval: float = 0.05

    def __post_init__(self) -> None:
        if not 1 <= self.total <= 10_000 or not 1 <= self.per_user <= self.total:
            raise ValueError("topic event stream limits must be positive and bounded")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _values(row) -> tuple:
    # Positional values for both SQLite rows and the PostgreSQL mapping rows, whose iteration yields column names.
    return tuple(row[index] for index in range(len(row)))


def signature(db, organization_id: str, topic_id: str, beta: bool) -> dict[str, Any]:
    """Content-free change markers for one topic, read without a user identity."""
    messages = db.execute(
        "SELECT MAX(sequence) FROM messages WHERE organization_id=? AND topic_id=?", (organization_id, topic_id)
    ).fetchone()[0]
    revisions = db.execute(
        "SELECT MAX(r.sequence) FROM message_revisions r JOIN messages m ON m.id=r.message_id "
        "WHERE r.organization_id=? AND m.topic_id=?",
        (organization_id, topic_id),
    ).fetchone()[0]
    bots = [
        _values(row)
        for row in db.execute(
            "SELECT id,status,phase,updated_at,error_code,activity_json FROM bot_runs "
            "WHERE organization_id=? AND topic_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
            (organization_id, topic_id),
        )
    ]
    result: dict[str, Any] = {"messages": (messages or 0, revisions or 0), "bot-runs": _digest(bots)}
    active = any(row[1] in {"queued", "running", "waiting_approval"} for row in bots)
    if beta:
        runs = [
            _values(row)
            for row in db.execute(
                "SELECT r.id,r.status,r.cancel_requested,r.message_id,r.accounting_state,r.actual_microusd,"
                "r.reserved_microusd,r.prompt_tokens,r.completion_tokens,r.started_at,r.finished_at,r.error_code,"
                "r.provider_generation_id,"
                "(SELECT MAX(e.sequence) FROM llm_events e WHERE e.organization_id=r.organization_id AND e.run_id=r.id),"
                "(SELECT MAX(ce.sequence) FROM inference_calls c JOIN inference_call_events ce "
                "ON ce.organization_id=c.organization_id AND ce.call_id=c.id WHERE c.run_id=r.id) "
                "FROM llm_runs r JOIN llm_requests q ON q.organization_id=r.organization_id AND q.id=r.request_id "
                "WHERE r.organization_id=? AND q.topic_id=? ORDER BY q.created_at DESC,r.id DESC LIMIT 20",
                (organization_id, topic_id),
            )
        ]
        threads = [
            _values(row)
            for row in db.execute(
                "SELECT a.root_run,a.status,a.reason,(SELECT COUNT(*) FROM topic_agent_tasks t WHERE t.root_run=a.root_run) "
                "FROM topic_agent_threads a WHERE a.organization_id=? AND a.topic_id=? ORDER BY a.root_run",
                (organization_id, topic_id),
            )
        ]
        goal = db.execute(
            "SELECT g.*,(SELECT MAX(sequence) FROM agent_goal_events e WHERE e.goal_id=g.id) AS last_event "
            "FROM agent_goals g WHERE g.organization_id=? AND g.topic_id=? ORDER BY g.created_at DESC,g.id DESC LIMIT 1",
            (organization_id, topic_id),
        ).fetchone()
        # Thread status is part of each run's view, so a thread change also refreshes runs.
        result.update(runs=_digest([runs, threads]), agents=_digest(threads), goal=_digest(dict(goal) if goal else None))
        active = active or any(row[1] in {"queued", "running"} for row in runs) or any(row[1] == "active" for row in threads)
        active = active or bool(goal and goal["status"] == "active")
    result["active"] = active
    return result


def direct_signature(db, organization_id: str, conversation_id: str) -> dict[str, Any]:
    """Content-free change markers for one direct conversation, read without a user identity."""
    messages = db.execute(
        "SELECT MAX(sequence) FROM direct_messages WHERE organization_id=? AND conversation_id=?", (organization_id, conversation_id)
    ).fetchone()[0]
    revisions = db.execute(
        "SELECT MAX(r.sequence) FROM direct_message_revisions r JOIN direct_messages m ON m.id=r.message_id "
        "WHERE r.organization_id=? AND m.conversation_id=?",
        (organization_id, conversation_id),
    ).fetchone()[0]
    reactions = db.execute(
        "SELECT MAX(sequence) FROM direct_reaction_changes WHERE organization_id=? AND conversation_id=?",
        (organization_id, conversation_id),
    ).fetchone()[0]
    return {"messages": (messages or 0, revisions or 0, reactions or 0), "active": False}


class _Subscription:
    def __init__(self, channels: tuple[str, ...]):
        self.dirty: set[str] = set(channels)
        self.wake = asyncio.Event()
        self.wake.set()

    def mark(self, channels: set[str]) -> None:
        if channels:
            self.dirty |= channels
            self.wake.set()


class _Watcher:
    def __init__(self, hub: TopicEvents, organization_id: str, stream_id: str, kind: str):
        self.hub, self.organization_id, self.stream_id, self.kind = hub, organization_id, stream_id, kind
        self.subscribers: set[_Subscription] = set()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        previous: dict[str, Any] | None = None
        while self.subscribers and not self.hub.closing:
            try:
                current = await anyio.to_thread.run_sync(self.hub.read_signature, self.organization_id, self.stream_id,
                                                         self.kind, limiter=self.hub.signature_limiter)
            except Exception:  # noqa: BLE001 - a failed read is retried; subscribers keep their last state.
                current = None
            if current is not None:
                if previous is not None:
                    changed = {channel for channel, value in current.items() if channel != "active" and previous.get(channel) != value}
                    for subscriber in list(self.subscribers):
                        subscriber.mark(changed)
                previous = current
            limits = self.hub.limits
            interval = limits.active_interval if current is None or current["active"] else limits.idle_interval
            await asyncio.sleep(limits.minimum_interval)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), max(0.0, interval - limits.minimum_interval))
            self.wake.clear()


class TopicEvents:
    """Per-process topic watchers, stream admission and shutdown."""

    def __init__(self, store, inference, *, enabled: bool = True, limits: StreamLimits | None = None):
        self.store, self.inference, self.enabled = store, inference, enabled
        self.limits = limits or StreamLimits()
        self.closing = False
        self.signature_limiter = anyio.CapacityLimiter(4)
        self.read_limiter = anyio.CapacityLimiter(8)
        self._watchers: dict[tuple[str, str, str], _Watcher] = {}
        self._users: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._previous_signals: dict[int, Any] = {}

    @property
    def open_streams(self) -> int:
        with self._lock:
            return sum(self._users.values())

    @property
    def watchers(self) -> int:
        return len(self._watchers)

    def read_signature(self, organization_id: str, stream_id: str, kind: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            if kind == "direct":
                return direct_signature(db, organization_id, stream_id)
            return signature(db, organization_id, stream_id, kind == "beta")

    def admit(self, user: str):
        """Count one stream against the installation and account limits; the returned release is idempotent."""
        if not self.enabled:
            raise StoreError(503, "topic event stream disabled")
        if self.closing:
            raise StoreError(503, "server is shutting down")
        with self._lock:
            if sum(self._users.values()) >= self.limits.total:
                raise StoreError(429, "too many open topic event streams")
            if self._users[user] >= self.limits.per_user:
                raise StoreError(429, "too many open topic event streams for this account")
            self._users[user] += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._users[user] -= 1
                if self._users[user] <= 0:
                    del self._users[user]

        return release

    def subscribe(self, organization_id: str, stream_id: str, kind: str) -> _Subscription:
        """Watch a topic (kind ``topic`` or ``beta``) or a direct conversation (kind ``direct``)."""
        subscription = _Subscription(STREAM_CHANNELS[kind])
        key = (organization_id, stream_id, kind)
        watcher = self._watchers.get(key)
        if watcher is None or watcher.task is None or watcher.task.done():
            watcher = _Watcher(self, organization_id, stream_id, kind)
            self._watchers[key] = watcher
        watcher.subscribers.add(subscription)
        if watcher.task is None:
            watcher.task = asyncio.get_running_loop().create_task(watcher.run())
        return subscription

    def unsubscribe(self, organization_id: str, stream_id: str, kind: str, subscription: _Subscription) -> None:
        key = (organization_id, stream_id, kind)
        watcher = self._watchers.get(key)
        if watcher is None:
            return
        watcher.subscribers.discard(subscription)
        if not watcher.subscribers:
            watcher.wake.set()
            if self._watchers.get(key) is watcher:
                del self._watchers[key]

    def nudge(self, path: str) -> None:
        """Re-read signatures at once after a write; a path naming a topic or direct conversation wakes only that one."""
        match = TOPIC_PATH.search(path)
        for (_, stream_id, _), watcher in list(self._watchers.items()):
            if match is None or match[1] == stream_id:
                watcher.wake.set()

    def close(self) -> None:
        """Ask every stream to end; called on server shutdown so connections drain within a second."""
        self.closing = True

    def chain_shutdown_signals(self) -> None:
        """Close streams when the server receives SIGINT/SIGTERM, before it waits for open connections."""
        if threading.current_thread() is not threading.main_thread():
            return
        for number in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(number)

            def handler(received, frame, previous=previous):
                self.close()
                if callable(previous):
                    previous(received, frame)

            self._previous_signals[number] = previous
            signal.signal(number, handler)

    def restore_shutdown_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for number, previous in self._previous_signals.items():
            signal.signal(number, previous)
        self._previous_signals.clear()


def _event(name: str, data: Any) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


class _RunPatches:
    """Per-connection record of sent runs, so unchanged runs are not resent and growing output is appended."""

    def __init__(self) -> None:
        self.sent: dict[str, tuple[str, int, str]] = {}
        self.order: list[str] | None = None

    def patch(self, runs: list[dict]) -> dict | None:
        order, changed, sent = [run["id"] for run in runs], [], {}
        for run in runs:
            text = run.get("output_text") or ""
            rest = {key: value for key, value in run.items() if key != "output_text"}
            entry = (_digest(rest), len(text), hashlib.sha256(text.encode()).hexdigest())
            sent[run["id"]] = entry
            previous = self.sent.get(run["id"])
            if previous == entry:
                continue
            grown = previous is not None and len(text) >= previous[1]
            if grown and hashlib.sha256(text[: previous[1]].encode()).hexdigest() == previous[2]:
                changed.append({**rest, "output_append": text[previous[1]:]})
            else:
                changed.append(run)
        if not changed and order == self.order:
            return None
        self.sent, self.order = sent, order
        return {"order": order, "runs": changed}


def install_routes(app, hub: TopicEvents, credentials) -> None:
    from .bot_runtime import BotRuntime
    from .bot_store import BotStore
    from .direct_messages import DirectMessages
    from .research_goals import ResearchGoals
    from .topic_agents import TopicAgents

    store, inference = hub.store, hub.inference
    bots, goals, agents = BotRuntime(BotStore(store)), ResearchGoals(inference), TopicAgents(inference)
    direct = DirectMessages(store)

    def topic_kind(user: str, organization_id: str, topic_id: str) -> str:
        with store.transaction() as db:
            return store._topic_access(db, user, organization_id, topic_id)[4]["kind"]

    def watermarks(user: str, organization_id: str, topic_id: str) -> dict:
        with store.transaction() as db:
            store._topic_access(db, user, organization_id, topic_id)
            value = signature(db, organization_id, topic_id, False)["messages"]
        return {"sequence": value[0], "updates": value[1]}

    readers = {
        "messages": watermarks,
        "runs": lambda user, org, topic: inference.runs(user, org, topic, limit=20),
        "agents": agents.threads,
        "goal": goals.current,
        "bot-runs": bots.listing,
    }

    def respond(user: str, access: str, organization_id: str, stream_id: str, kind: str, readers: dict, check) -> _EventResponse:
        """Stream one readable topic or direct conversation; `check` repeats its access decision."""
        release = hub.admit(user)
        limits = hub.limits

        def revalidate() -> None:
            if store.session(access)["user_id"] != user:
                raise StoreError(401, "sign-in required")
            check(user, organization_id, stream_id)

        async def stream():
            subscription = hub.subscribe(organization_id, stream_id, kind)
            sent: dict[str, str] = {}
            runs = _RunPatches()
            started = time.monotonic()
            last_write = recheck = started
            try:
                yield _event("ready", {"heartbeat_ms": int(limits.heartbeat * 1000), "channels": sorted(subscription.dirty)})
                while True:
                    now = time.monotonic()
                    if hub.closing:
                        yield _event("end", {"reason": "shutdown"})
                        return
                    if now - started >= limits.lifetime:
                        yield _event("end", {"reason": "lifetime"})
                        return
                    if now - recheck >= limits.recheck:
                        recheck = now
                        try:
                            await anyio.to_thread.run_sync(revalidate, limiter=hub.read_limiter)
                        except StoreError as exc:
                            yield _event("end", {"reason": "session" if exc.status == 401 else "access"})
                            return
                    if subscription.dirty:
                        channels, subscription.dirty = subscription.dirty, set()
                        for channel in STREAM_CHANNELS[kind]:
                            if channel not in channels:
                                continue
                            try:
                                value = await anyio.to_thread.run_sync(readers[channel], user, organization_id, stream_id,
                                                                       limiter=hub.read_limiter)
                            except StoreError as exc:
                                yield _event("end", {"reason": "session" if exc.status == 401 else "access"})
                                return
                            if channel == "runs":
                                patch = runs.patch(value)
                                if patch is None:
                                    continue
                                value = patch
                            else:
                                digest = _digest(value)
                                if sent.get(channel) == digest:
                                    continue
                                sent[channel] = digest
                            yield _event(channel, value)
                            last_write = time.monotonic()
                    if time.monotonic() - last_write >= limits.heartbeat:
                        yield b": keepalive\n\n"
                        last_write = time.monotonic()
                    if not subscription.dirty:
                        subscription.wake.clear()
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(subscription.wake.wait(), 1.0)
            finally:
                hub.unsubscribe(organization_id, stream_id, kind, subscription)
                release()

        return _EventResponse(stream(), release)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/events", include_in_schema=False)
    async def topic_events(organization_id: str, topic_id: str, request: Request):
        user, access = await anyio.to_thread.run_sync(lambda: credentials(request))
        kind = await anyio.to_thread.run_sync(topic_kind, user, organization_id, topic_id)
        return respond(user, access, organization_id, topic_id, "beta" if kind == "beta" else "topic", readers, topic_kind)

    @app.get("/api/organizations/{organization_id}/direct-conversations/{conversation_id}/events", include_in_schema=False)
    async def direct_events(organization_id: str, conversation_id: str, request: Request):
        user, access = await anyio.to_thread.run_sync(lambda: credentials(request))
        await anyio.to_thread.run_sync(direct.watermarks, user, organization_id, conversation_id)
        return respond(user, access, organization_id, conversation_id, "direct", {"messages": direct.watermarks}, direct.watermarks)


class _EventResponse(StreamingResponse):
    """An event stream whose admission is released even if the body never starts.

    The application middleware marks it ``Cache-Control: no-store, no-transform``, which keeps
    compressing proxies (Caddy's ``encode``) from holding events back."""

    def __init__(self, body, release):
        super().__init__(body, media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
        self.release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()
