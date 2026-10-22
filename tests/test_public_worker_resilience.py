"""Background queues retain work across real SQLite writer contention."""

import asyncio
import sqlite3
import threading
from contextlib import contextmanager

import pytest
import test_public_invitation_mail as mail_tests
import test_public_webhooks as webhook_tests
import test_public_workflow_runs as workflow_tests
from fastapi.testclient import TestClient
from test_public_inference import MODELS, Fixture, enqueue, prepare, wait
from test_public_invitation_mail import PROVIDER_ID, mail_row
from test_public_invitation_mail import enqueue as enqueue_mail
from test_public_server import installation as installation
from test_public_webhooks import create, post, records
from test_public_workflow_runs import compute_node, nodes

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.inference_worker import InferenceWorker
from reasoning_ledger.server.mail import MailWorker
from reasoning_ledger.server.openrouter import Chunk, Usage
from reasoning_ledger.server.webhook_worker import WebhookWorker
from reasoning_ledger.server.worker_poll import claim_work
from reasoning_ledger.server.workflow_worker import WorkflowWorker

mail_installation = mail_tests.installation
webhook_installation = webhook_tests.fixture
workflow_installation = workflow_tests.installation


def contend_once(store, operation):
    attempted = threading.Event()

    def poll(*args, **kwargs):
        if not attempted.is_set():
            with sqlite3.connect(store.database) as writer:
                writer.execute("BEGIN IMMEDIATE")
                try:
                    with sqlite3.connect(store.database, timeout=0.01) as contender:
                        contender.execute("BEGIN IMMEDIATE")
                finally:
                    attempted.set()
        return operation(*args, **kwargs)

    return poll, attempted


def test_inference_queue_recovers_without_duplicate_provider_calls(installation, monkeypatch):
    _, state, _, owner, topics = prepare(installation)
    row = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    poll, attempted = contend_once(state.store, state.claim)
    monkeypatch.setattr(state, "claim", poll)
    provider = Fixture()
    worker = InferenceWorker(state, provider.provider)
    worker.start()
    try:
        wait(attempted.is_set)
        wait(lambda: state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "succeeded")
        assert worker.running
        assert sum(request.method == "POST" for request in provider.requests) == 1
    finally:
        worker.stop()


def test_slow_queue_poll_does_not_freeze_active_provider_stream(installation, monkeypatch):
    _, state, _, owner, topics = prepare(installation)
    row = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    claim = state.claim
    polling, streamed, release = threading.Event(), threading.Event(), threading.Event()
    calls = 0

    def slow_claim():
        nonlocal calls
        calls += 1
        if calls == 2:
            polling.set()
            assert release.wait(5)
        return claim()

    class Provider:
        async def stream(self, *_args):
            while not polling.is_set():
                await asyncio.sleep(0.01)
            streamed.set()
            yield Chunk("A complete answer.", "gen-live", Usage(8, 20, 10), "stop")

    monkeypatch.setattr(state, "claim", slow_claim)
    worker = InferenceWorker(state, Provider())
    worker.start()
    try:
        wait(polling.is_set)
        assert streamed.wait(2), "provider stream froze behind the database poll"
        release.set()
        wait(lambda: state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "succeeded")
    finally:
        release.set()
        worker.stop()


def test_mail_queue_recovers_and_sends_once(mail_installation, monkeypatch):
    store, _, state = mail_installation
    enqueue_mail(mail_installation)
    poll, attempted = contend_once(store, state.claim)
    monkeypatch.setattr(state, "claim", poll)
    sent = []

    class Provider:
        async def send(self, row):
            sent.append(row)
            return PROVIDER_ID

    worker = MailWorker(state, Provider())
    worker.start()
    try:
        wait(attempted.is_set)
        wait(lambda: mail_row(store)["state"] == "accepted")
        assert worker.running and len(sent) == 1
    finally:
        worker.stop()


def test_webhook_queue_recovers_and_sends_once(webhook_installation, monkeypatch):
    store, _, _, _, _, state = webhook_installation
    hook = create(webhook_installation)
    post(webhook_installation)
    poll, attempted = contend_once(store, state.claim)
    monkeypatch.setattr(state, "claim", poll)
    sent = []

    class Transport:
        async def send(self, row, authorized):
            assert authorized(row)
            sent.append(row)

    worker = WebhookWorker(state, Transport())
    worker.start()
    try:
        wait(attempted.is_set)
        wait(lambda: records(webhook_installation, hook)[0]["state"] == "accepted")
        assert worker.running and len(sent) == 1
    finally:
        worker.stop()


@pytest.mark.parametrize("boundary", ["active", "advance"])
def test_workflow_retries_contention_without_duplicate_children(workflow_installation, monkeypatch, boundary):
    env = workflow_installation
    run = env.start([compute_node()])
    poll, attempted = contend_once(env.store, getattr(env.runs, boundary))
    monkeypatch.setattr(env.runs, boundary, poll)
    worker = WorkflowWorker(env.runs, None)
    worker.start()
    try:
        wait(attempted.is_set)
        wait(lambda: nodes(env.view(run))["compute"]["status"] == "running")
        assert worker.running
        with env.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 1
    finally:
        worker.stop()


@pytest.mark.parametrize("worker", ["inference", "workflow", "webhook", "mail"])
def test_health_fails_when_a_required_worker_stops(tmp_path, worker):
    from test_public_invitation_mail import CONFIG

    app = create_app(Config(tmp_path / "state", "https://chat.example.org", mail=CONFIG))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        getattr(app.state, worker + "_worker").stop()
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert response.json()[worker + "_worker_running"] is False
        assert response.headers["Cache-Control"] == "no-store"
        assert CONFIG.api_key not in response.text and CONFIG.sender not in response.text


def test_database_poll_retries_only_contention_and_redacts_exception_text(caplog):
    error = sqlite3.OperationalError("private path and secret")
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR

    def broken():
        raise error

    with pytest.raises(sqlite3.OperationalError):
        claim_work(broken)
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    assert claim_work(broken) is None
    assert "private path and secret" not in caplog.text


@pytest.mark.parametrize("admission", ["ResearchReviews", "ResearchGoals"])
def test_busy_maintenance_does_not_stop_inference(installation, monkeypatch, admission):
    from reasoning_ledger.server.research_goals import ResearchGoals
    from reasoning_ledger.server.research_reviews import ResearchReviews

    _, state, _, _, _ = prepare(installation)
    calls = 0

    async def busy_once(self, provider, stopped=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    monkeypatch.setattr({"ResearchReviews": ResearchReviews, "ResearchGoals": ResearchGoals}[admission], "dispatch", busy_once)
    worker = InferenceWorker(state, None)
    worker.start()
    try:
        wait(lambda: calls >= 2)
        assert worker.running
    finally:
        worker.stop()


def busy_first_write(store, monkeypatch):
    transaction = store.transaction
    attempted = False

    @contextmanager
    def busy_transaction(*, write=False, **kwargs):
        nonlocal attempted
        if write and not attempted:
            attempted = True
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        with transaction(write=write, **kwargs) as db:
            yield db

    monkeypatch.setattr(store, "transaction", busy_transaction)


def test_busy_review_admission_stays_pending_then_enqueues_once(installation, monkeypatch):
    from test_organization_research_workbench import ReviewerFixture, source_run

    from reasoning_ledger.server.research_reviews import ResearchReviews

    state, owner, topics, parent, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "Every integer equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Equality is reflexive."})["result"]
    reviews = ResearchReviews(state)
    review = reviews.request(tools, proof_id=proof["proof_id"])
    state.progress(parent, "The proof is ready for review.", None)
    state.finish(parent, "succeeded", usage=Usage(8, 20, 10))
    busy_first_write(state.store, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(reviews.dispatch(ReviewerFixture().provider))
    with state.store.transaction() as db:
        assert db.execute("SELECT status FROM mathematical_reviews WHERE id=?", (review["id"],)).fetchone()[0] == "pending"
    asyncio.run(reviews.dispatch(ReviewerFixture().provider))
    asyncio.run(reviews.dispatch(ReviewerFixture().provider))
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_runs WHERE status='queued'").fetchone()[0] == 1


def test_busy_goal_admission_stays_active_then_enqueues_once(installation, monkeypatch):
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_goals import ResearchGoals

    _, state, _, owner, topics = prepare(installation)
    goals = ResearchGoals(state)
    goals.create(owner["user_id"], owner["organization_id"], topics[1]["id"],
                 client_id="busy-goal-fixture-001", objective="Explain the supplied result",
                 acceptance=["Give a precise argument"], model=MODELS[0], budget_microusd=1_000_000,
                 max_turns=3, max_tool_calls=5, duration_seconds=3600)
    provider = ResearchFixture("goal").provider
    busy_first_write(state.store, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(goals.dispatch(provider))
    current = goals.current(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert current["status"] == "active" and current["turns"] == 0
    asyncio.run(goals.dispatch(provider))
    asyncio.run(goals.dispatch(provider))
    current = goals.current(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert current["status"] == "active" and current["turns"] == 1
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 1
