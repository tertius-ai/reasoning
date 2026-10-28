"""Background queues retain work across real SQLite writer contention."""

import asyncio
import json
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


def test_busy_review_completion_is_retried_without_replaying_settled_execution(installation, monkeypatch):
    from test_organization_research_workbench import ReviewerFixture, source_run

    from reasoning_ledger.server.openrouter import Quote
    from reasoning_ledger.server.research_agent import ResearchAgent
    from reasoning_ledger.server.research_reviews import ResearchReviews

    state, owner, topics, parent, tools = source_run(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    reviews = ResearchReviews(state)
    review = reviews.request(tools, proof_id=proof["proof_id"])
    state.finish(parent, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(reviews.dispatch(fixture.provider))
    child = state.claim()
    poll, attempted = contend_once(state.store, ResearchReviews.finish)
    monkeypatch.setattr(ResearchReviews, "finish", poll)

    ResearchAgent(state, fixture.provider, child, Quote(**json.loads(child["quote_json"]))).execute()
    assert attempted.is_set() and len(fixture.requests) == 1
    with state.store.transaction() as db:
        settled = dict(db.execute("SELECT * FROM llm_runs WHERE id=?", (child["id"],)).fetchone())
        assert settled["status"] == "succeeded" and settled["accounting_state"] == "settled"
        assert db.execute("SELECT status FROM mathematical_reviews WHERE id=?", (review["id"],)).fetchone()[0] == "running"
        assert db.execute("SELECT COUNT(*) FROM mathematical_records WHERE record_type='verification'").fetchone()[0] == 0
    balance = state.balance(user, org)

    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: reviews.list(user, org, topic)[0]["status"] == "complete")
        # Maintenance retries bookkeeping; repeated passes must not certify or charge twice.
        asyncio.run(reviews.dispatch(fixture.provider))
        asyncio.run(reviews.dispatch(fixture.provider))
        with state.store.transaction() as db:
            assert dict(db.execute("SELECT * FROM llm_runs WHERE id=?", (child["id"],)).fetchone()) == settled
            assert db.execute("SELECT COUNT(*) FROM mathematical_records WHERE record_type='verification'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM inference_calls WHERE run_id=?", (child["id"],)).fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 2
        assert state.balance(user, org) == balance and len(fixture.requests) == 1
        later = enqueue(state, owner, topic, client="after-review-cleanup", models=MODELS[:1])[0]
        wait(lambda: state.run(user, org, later["id"])["status"] == "succeeded")
        assert worker.running and len(fixture.requests) == 2
    finally:
        worker.stop()


def test_busy_completion_preserves_active_sibling_and_later_dispatch(installation, monkeypatch):
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_reviews import ResearchReviews

    _, state, _, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    first = enqueue(state, owner, topic, client="first-completion", models=MODELS[:1])[0]
    state.store.post_message(user, org, topic, "sibling-wait-marker", "sibling-human")
    sibling = enqueue(state, owner, topic, client="active-sibling", models=MODELS[:1])[0]
    entered, release = threading.Event(), threading.Event()

    class GatedFixture(ResearchFixture):
        def handle(self, request):
            if request.method == "POST":
                if "sibling-wait-marker" in request.content.decode():
                    entered.set()
                    assert release.wait(5), "sibling provider was never released"
                else:
                    assert entered.wait(5), "sibling did not execute concurrently"
            return super().handle(request)

    original_finish = ResearchReviews.finish
    poll, attempted = contend_once(state.store, original_finish)

    def finish(reviews, row):
        return poll(reviews, row) if row["id"] == first["id"] else original_finish(reviews, row)

    monkeypatch.setattr(ResearchReviews, "finish", finish)
    fixture = GatedFixture("final")
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(attempted.is_set)
        assert entered.is_set() and state.run(user, org, first["id"])["status"] == "succeeded"
        assert state.run(user, org, sibling["id"])["status"] == "running"
        release.set()
        wait(lambda: state.run(user, org, sibling["id"])["status"] == "succeeded")
        later = enqueue(state, owner, topic, client="after-sibling", models=MODELS[:1])[0]
        wait(lambda: state.run(user, org, later["id"])["status"] == "succeeded")
        assert worker.running and len(fixture.requests) == 3
        assert all(state.run(user, org, row["id"])["accounting_state"] == "settled" for row in (first, sibling, later))
    finally:
        release.set()
        worker.stop()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR])
def test_review_completion_defers_only_contention_and_redacts_errors(installation, monkeypatch, caplog, code):
    from test_public_research_agent import ResearchFixture, turn

    from reasoning_ledger.server.research_reviews import ResearchReviews

    error = sqlite3.OperationalError("private completion path and secret")
    error.sqlite_errorcode = code

    def broken(_reviews, _row):
        raise error

    monkeypatch.setattr(ResearchReviews, "finish", broken)
    fixture = ResearchFixture("final")
    if code == sqlite3.SQLITE_IOERR:
        with pytest.raises(sqlite3.OperationalError) as caught:
            turn(installation, fixture)
        assert caught.value is error
    else:
        state, owner, _, row = turn(installation, fixture)
        result = state.run(owner["user_id"], owner["organization_id"], row["id"])
        assert result["status"] == "succeeded" and result["accounting_state"] == "settled"
        assert caplog.records
    assert len(fixture.requests) == 1 and str(error) not in caplog.text


def test_busy_native_completion_preserves_success_and_later_dispatch(installation, monkeypatch, caplog):
    from test_public_research_agent import ResearchFixture

    _, state, _, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    first = enqueue(state, owner, topic, client="primary-completion", models=MODELS[:1])[0]
    finish, outcomes = state.finish, []

    def busy_twice(row, status, **outcome):
        if row["id"] == first["id"]:
            outcomes.append((status, outcome))
            if len(outcomes) <= 2:
                # Fast real contention; the separate session test exercises the
                # original finish transaction and its production 15s timeout.
                with sqlite3.connect(state.store.database) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    with sqlite3.connect(state.store.database, timeout=0.01) as contender:
                        contender.execute("BEGIN IMMEDIATE")
        return finish(row, status, **outcome)

    monkeypatch.setattr(state, "finish", busy_twice)
    provider = ResearchFixture("final")
    worker = InferenceWorker(state, provider.provider)
    worker.start()
    try:
        wait(lambda: state.run(user, org, first["id"])["status"] == "succeeded")
        assert len(outcomes) == 3 and all(outcome == outcomes[0] for outcome in outcomes)
        assert outcomes[0][0] == "succeeded"
        later = enqueue(state, owner, topic, client="after-primary-completion", models=MODELS[:1])[0]
        wait(lambda: state.run(user, org, later["id"])["status"] == "succeeded")
        assert worker.running and len(provider.requests) == 2
        with state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM messages WHERE inference_run_id=?", (first["id"],)).fetchone()[0] == 1
            assert db.execute("SELECT actual_microusd FROM llm_runs WHERE id=?", (first["id"],)).fetchone()[0] == 8
            assert db.execute("SELECT COUNT(*) FROM inference_calls WHERE run_id=?", (first["id"],)).fetchone()[0] == 1
        assert "Native result persistence is busy; retained result will be retried" in caplog.text
    finally:
        worker.stop()


def native_completion(installation):
    from reasoning_ledger.server.openrouter import Quote
    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    enqueue(state, owner, topics[1]["id"], client="native-completion", models=MODELS[:1])
    row = state.claim()
    state.progress(row, "A retained result.", "gen-completion")
    agent = ResearchAgent(state, None, row, Quote(**json.loads(row["quote_json"])))
    return state, owner, row, agent


def test_native_completion_retries_rolled_back_answer_and_accounting(installation, monkeypatch):
    state, owner, row, agent = native_completion(installation)
    event, attempts = state._event, 0

    def busy_after_answer(db, org, identity, body):
        nonlocal attempts
        if body.get("type") == "state" and body.get("status") == "succeeded":
            attempts += 1
            if attempts == 1:
                assert db.execute("SELECT COUNT(*) FROM messages WHERE inference_run_id=?", (identity,)).fetchone()[0] == 1
                error = sqlite3.OperationalError("completion transaction is busy")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
                raise error
        event(db, org, identity, body)

    monkeypatch.setattr(state, "_event", busy_after_answer)
    assert agent._finish("succeeded", usage=Usage(8, 20, 10)) is True
    assert attempts == 2
    # A second completion attempt is fenced, even after a successful retry.
    assert agent._finish("succeeded", usage=Usage(8, 20, 10)) is False
    current = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert current["status"] == "succeeded" and current["accounting_state"] == "settled"
    assert current["actual_microusd"] == 8
    with state.store.transaction() as db:
        parent = db.execute("SELECT id FROM messages WHERE organization_id=? AND topic_id=? AND sequence=?",
                            (row["organization_id"], row["topic_id"], row["context_through_sequence"])).fetchone()[0]
        replies = db.execute("SELECT reply_to FROM messages WHERE inference_run_id=?", (row["id"],)).fetchall()
        assert [reply[0] for reply in replies] == [parent]


def test_native_completion_rechecks_cancellation_after_contention(installation, monkeypatch):
    state, owner, row, agent = native_completion(installation)
    finish, attempts = state.finish, 0

    def cancel_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            state.cancel(owner["user_id"], owner["organization_id"], row["id"])
            error = sqlite3.OperationalError("completion transaction is busy")
            error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
            raise error
        return finish(*args, **kwargs)

    monkeypatch.setattr(state, "finish", cancel_once)
    assert agent._finish("succeeded", usage=Usage(8, 20, 10)) is True
    current = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert current["status"] == "cancelled" and current["actual_microusd"] == 8
    assert current["accounting_state"] == "settled" and attempts == 2


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_IOERR])
def test_native_completion_does_not_retry_other_database_errors(installation, monkeypatch, caplog, code):
    state, _, _, agent = native_completion(installation)
    error = sqlite3.OperationalError("private completion error")
    if code is not None:
        error.sqlite_errorcode = code
    attempts = 0

    def broken(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(state, "finish", broken)
    with pytest.raises(sqlite3.OperationalError) as caught:
        agent._finish("succeeded", usage=Usage(8, 20, 10))
    assert caught.value is error and attempts == 1
    assert str(error) not in caplog.text


def test_native_completion_stops_retrying_on_shutdown(installation, monkeypatch, caplog):
    state, _, row, agent = native_completion(installation)
    stopped, attempts = threading.Event(), 0
    agent.stop_requested = stopped.is_set

    def busy(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        stopped.set()
        error = sqlite3.OperationalError("private completion lock")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(state, "finish", busy)
    assert agent._finish("succeeded", usage=Usage(8, 20, 10)) is None, "stopping for shutdown differs from being fenced"
    assert attempts == 1 and "private completion lock" not in caplog.text
    with state.store.transaction() as db:
        current = db.execute("SELECT status,accounting_state,output_text FROM llm_runs WHERE id=?", (row["id"],)).fetchone()
        assert tuple(current) == ("running", "reserved", "A retained result.")


def test_a_fenced_failure_still_records_its_failure(installation, monkeypatch):
    from test_public_research_agent import ResearchFixture, turn

    from reasoning_ledger.server import research_agent
    from reasoning_ledger.server.inference_store import InferenceStore

    def broken(*_args, **_kwargs):
        raise RuntimeError("private native failure")

    statuses = []

    def fenced(_state, _row, status, **_outcome):
        # Another path already completed this attempt: finish commits nothing and reports the fence.
        statuses.append(status)
        return False

    monkeypatch.setattr(research_agent.ReasoningNativeExecutor, "execute", broken)
    monkeypatch.setattr(InferenceStore, "finish", fenced)
    state, owner, _, row = turn(installation, ResearchFixture("final"))
    assert statuses == ["failed"]
    phases = [event["event"] for event in state.events(owner["user_id"], owner["organization_id"], row["id"], 0)]
    failed = [event for event in phases if event.get("phase") == "failed"]
    assert [event["reason"] for event in failed] == ["agent_execution_error"], "only stopping for shutdown ends the turn early"


def test_busy_plain_completion_is_retried_with_its_outcome(installation, monkeypatch, caplog):
    _, state, _, owner, topics = prepare(installation)
    user, org = owner["user_id"], owner["organization_id"]
    row = enqueue(state, owner, topics[1]["id"], client="plain-completion", models=MODELS[:1])[0]
    with state.store.transaction(write=True) as db:
        # A request accepted by an earlier release has no research snapshot and streams one plain reply.
        db.execute("DELETE FROM research_contexts WHERE request_id=?", (row["request_id"],))
    finish, outcomes = state.finish, []

    def busy_twice(run, status, **outcome):
        if run["id"] == row["id"]:
            outcomes.append((status, outcome))
            if len(outcomes) <= 2:
                error = sqlite3.OperationalError("private plain completion lock")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
        return finish(run, status, **outcome)

    monkeypatch.setattr(state, "finish", busy_twice)
    provider = Fixture()
    worker = InferenceWorker(state, provider.provider)
    worker.start()
    try:
        wait(lambda: state.run(user, org, row["id"])["status"] == "succeeded")
        assert len(outcomes) == 3 and all(outcome == outcomes[0] for outcome in outcomes) and outcomes[0][0] == "succeeded"
        assert worker.running
        with state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM messages WHERE inference_run_id=?", (row["id"],)).fetchone()[0] == 1
            calls = db.execute("SELECT id FROM inference_calls WHERE run_id=?", (row["id"],)).fetchall()
            assert len(calls) == 1, "the model is not called again"
            assert db.execute("SELECT COUNT(*) FROM inference_call_events WHERE call_id=? AND kind='completion'",
                              (calls[0][0],)).fetchone()[0] == 1
        assert "Run result persistence is busy; retained result will be retried" in caplog.text
        assert "private plain completion lock" not in caplog.text
    finally:
        worker.stop()
