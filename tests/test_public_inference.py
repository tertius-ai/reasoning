"""Shared beta execution with real HTTP serialization, durable state, and no paid provider."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.api import create_app
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.inference_worker import InferenceWorker
from reasoning_ledger.server.openrouter import OpenRouter, ProviderError, Quote, Usage, microusd, provider_origin
from reasoning_ledger.server.store import Store, StoreError

KEY = "sk-or-organization-test-secret"
MODELS = ["test/one", "test/two"]
QUOTES = [Quote(model, "0.000001", "0.000002", "0", 200000, 8192) for model in MODELS]


def test_routing_is_enforced_and_pinned_to_each_accepted_request(installation):
    _, state, client, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    policy = {"strategy": "recommended", "zdr": True, "region": "eu"}
    assert client.put(f"/api/organizations/{org}/routing", json=policy).status_code == 200
    assert state.configuration(user, org)["routing"] == policy
    run = enqueue(state, owner, topic, models=MODELS[:1])[0]
    changed = {"strategy": "cheapest", "zdr": False, "region": "global"}
    assert client.put(f"/api/organizations/{org}/routing", json=changed).status_code == 200
    with state.store.transaction() as db:
        quote = Quote(**json.loads(db.execute("SELECT quote_json FROM llm_runs WHERE id=?", (run["id"],)).fetchone()[0]))
    assert quote.routing == policy
    assert quote.route() == {"allow_fallbacks": True, "require_parameters": True, "zdr": True, "data_collection": "deny"}
    assert quote.origin("https://openrouter.ai/api/v1") == "https://eu.openrouter.ai/api/v1"
    fixture = Fixture()
    asyncio.run(collect_stream(fixture.provider, quote))
    sent = [item for item in fixture.requests if item.method == "POST"][0]
    assert str(sent.url) == "https://eu.openrouter.ai/api/v1/chat/completions"
    assert json.loads(sent.content)["provider"] == quote.route()
    cheap = Quote(**{**quote.record(), "routing": {"strategy": "cheapest", "zdr": False, "region": "global"}})
    assert cheap.route()["sort"] == "price" and cheap.route()["max_price"] == {"prompt": 1, "completion": 2}
    assert client.put(f"/api/organizations/{org}/routing", json={**policy, "zdr": "yes"}).status_code == 422


@pytest.mark.parametrize("strategy", ["latency", "throughput"])
def test_speed_routing_is_explicit_and_preserves_privacy_and_regional_constraints(installation, strategy):
    _, state, client, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    policy = {"strategy": strategy, "zdr": True, "region": "us"}
    assert client.put(f"/api/organizations/{org}/routing", json=policy).status_code == 200
    row = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    with state.store.transaction() as db:
        quote = Quote(**json.loads(db.execute("SELECT quote_json FROM llm_runs WHERE id=?", (row["id"],)).fetchone()[0]))
    assert quote.route() == {"allow_fallbacks": True, "require_parameters": True, "sort": strategy,
                             "zdr": True, "data_collection": "deny"}
    assert quote.origin("https://openrouter.ai/api/v1") == "https://us.openrouter.ai/api/v1"
    assert state.configuration(user, org)["routing"] == policy
    assert client.put(f"/api/organizations/{org}/routing", json={**policy, "strategy": "magic"}).status_code == 422


def test_default_worker_runs_eight_provider_calls_concurrently_without_duplicate_submissions(installation):
    from reasoning_ledger.server.openrouter import Chunk

    _, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    release, lock = threading.Event(), threading.Lock()

    class ParallelProvider:
        calls = 0

        async def stream(self, key, quote, messages, maximum):
            with lock:
                self.calls += 1
                identity = self.calls
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield Chunk("Concurrent response.", f"gen-parallel-{identity}", Usage(8, 20, 10), "stop")

    provider = ParallelProvider()
    rows = [enqueue(state, owner, topics[1]["id"], client=f"parallel-{index}", models=MODELS[:1])[0] for index in range(8)]
    worker = InferenceWorker(state, provider)
    assert worker.concurrency == 8
    worker.start()
    try:
        wait(lambda: provider.calls == 8)
        assert all(state.run(user, org, row["id"])["status"] == "running" for row in rows)
        release.set()
        wait(lambda: all(state.run(user, org, row["id"])["status"] == "succeeded" for row in rows))
    finally:
        release.set()
        worker.stop()
    assert provider.calls == 8
    assert sum(state.run(user, org, row["id"])["actual_microusd"] for row in rows) == 64


def test_slow_background_admission_does_not_block_already_queued_inference(installation, monkeypatch):
    from reasoning_ledger.server.research_reviews import ResearchReviews

    entered, closed = threading.Event(), threading.Event()

    async def blocked(self, provider, stopped=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(ResearchReviews, "dispatch", blocked)
    _, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    row = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture()
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(entered.is_set)
        wait(lambda: any(request.method == "POST" for request in fixture.requests))
        # Completion includes PostgreSQL's durable receipt/permission checks for
        # many tiny wire fragments; isolate admission from that accounting cost.
        wait(lambda: state.run(user, org, row["id"])["status"] not in {"queued", "running"}, timeout=30)
        outcome = state.run(user, org, row["id"])
        assert outcome["status"] == "succeeded", outcome.get("error_code")
    finally:
        worker.stop()
    assert closed.is_set()
    assert sum(request.method == "POST" for request in fixture.requests) == 1


async def collect_stream(provider, quote):
    return [chunk async for chunk in provider.stream(KEY, quote, [{"role": "user", "content": "Fixture"}], None)]


def wait(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("inference condition did not become true")


class Fixture:
    def __init__(self, mode="normal"):
        self.mode, self.requests = mode, []
        self.closed = threading.Event()
        self.provider = OpenRouter(transport=httpx.MockTransport(self.handle))

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": model,
                            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                            "context_length": 200000,
                            "supported_parameters": ["max_completion_tokens"],
                            "top_provider": {"max_completion_tokens": 8192},
                        }
                        for model in MODELS
                    ]
                },
            )
        assert request.headers["Authorization"] == "Bearer " + KEY
        if self.mode == "reject":
            return httpx.Response(401, json={"error": {"message": KEY}})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FixtureStream(self))


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, fixture):
        self.fixture = fixture

    async def __aiter__(self):
        def event(body):
            return ("data: " + json.dumps(body, ensure_ascii=False) + "\r\n\r\n").encode()

        try:
            yield b": provider heartbeat\n\n"
            first = event({"id": "gen-local-fixture", "choices": [{"index": 0, "delta": {"content": "α = "}}]})
            for start in range(0, len(first), 3):
                yield first[start : start + 3]
            if self.fixture.mode == "slow":
                await asyncio.sleep(3600)
            if self.fixture.mode == "cutoff":
                return
            if self.fixture.mode == "error":
                yield event({"error": {"message": KEY}})
                return
            yield event({"id": "gen-local-fixture", "choices": [{"index": 0, "delta": {"content": "1."}}]})
            if self.fixture.mode == "truncated":
                finish = "length" if len(self.fixture.requests) < 6 else "stop"
                yield event({"id": "gen-local-fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            if self.fixture.mode != "missing_usage":
                cost = "-1" if self.fixture.mode == "bad_usage" else "0.0000079"
                yield event(
                    {"id": "gen-local-fixture", "choices": [], "usage": {"cost": cost, "prompt_tokens": 20, "completion_tokens": 5}}
                )
            if self.fixture.mode == "after_usage":
                await asyncio.sleep(3600)
            yield b"data: [DONE]\n\n"
        finally:
            self.fixture.closed.set()


def test_new_models_and_router_ids_are_not_restricted_by_old_policy_hints(installation):
    _, state, _, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    state.store.set_policy(user, org, [], 1000000)
    catalog = ["new/fresh-model", "openrouter/auto"]
    assert state.configuration(user, org, models=catalog)["allowed_models"] == catalog
    state.preflight(user, org, topic, catalog)
    parameters = state.parameters(catalog, None, 50, None)
    quotes = [Quote(model, "0.000001", "0.000002", "0", 200000, 8192) for model in catalog]
    runs = state.enqueue(user, org, topic, "fresh-router-request", parameters, quotes)
    assert {run["model"] for run in runs} == set(catalog)
    state.store.set_policy(user, org, ["obsolete/model"], 1000000)
    assert state.claim()["model"] in catalog


def test_quotes_preserve_large_provider_reported_output_maximum():
    def catalog(_request):
        return httpx.Response(200, json={"data": [{
            "id": "test/large", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "context_length": 200000,
            "supported_parameters": ["max_completion_tokens"], "top_provider": {"max_completion_tokens": 131072},
        }]})

    quote = asyncio.run(OpenRouter(transport=httpx.MockTransport(catalog)).quotes(["test/large"]))[0]
    assert quote.max_completion_tokens == 131072


def prepare(installation):
    config, store, client, owner = installation
    org = owner["organization_id"]
    topics = structure(client, org)[2]
    root = f"/api/organizations/{org}"
    client.post(root + "/provider-keys", json={"key": KEY, "label": "Organization"}).raise_for_status()
    client.put(root + "/policy", json={"allowed_models": MODELS, "monthly_budget_microusd": 1_000_000}).raise_for_status()
    client.post(
        root + f"/topics/{topics[1]['id']}/messages", json={"content": "Compute a shared result", "client_id": "human"}
    ).raise_for_status()
    return config, InferenceStore(store), client, owner, topics


def enqueue(state, owner, topic, *, client="request", models=None, through=None, maximum=256):
    models = models or MODELS
    parameters = state.parameters(models, maximum, 50, through)
    return state.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topic,
        client,
        parameters,
        [quote for model in models for quote in QUOTES if quote.model == model],
    )


def test_group_models_use_one_immutable_scoped_context_and_organization_key(installation):
    config, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    runs = enqueue(state, owner, topic)
    client.post(
        f"/api/organizations/{org}/topics/{topic}/messages", json={"content": "Later message is not in this request", "client_id": "later"}
    )
    fixture = Fixture()
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: all(state.run(owner["user_id"], org, run["id"])["status"] == "succeeded" for run in runs))
    finally:
        worker.stop()
    requests = [request for request in fixture.requests if request.method == "POST"]
    assert len(requests) == 2
    payloads = [json.loads(request.content) for request in requests]
    assert payloads[0]["messages"] == payloads[1]["messages"]
    assert "Later message" not in json.dumps(payloads)
    assert all(payload["max_completion_tokens"] == 256 for payload in payloads)
    assert all(payload["provider"] == {"allow_fallbacks": True, "require_parameters": True} for payload in payloads)
    messages = Store(config.data_dir).messages(owner["user_id"], org, topic)
    assistants = [message for message in messages if message["author_kind"] == "assistant"]
    assert len(assistants) == 2 and {message["assistant_model"] for message in assistants} == set(MODELS)
    assert all(message["content"] == "α = 1." and message["inference_run_id"] for message in assistants)
    assert state.balance(owner["user_id"], org)["spent_microusd"] == 16
    assert state.balance(owner["user_id"], org)["reserved_microusd"] == 0
    events = state.events(owner["user_id"], org, runs[0]["id"], 0)
    assert "".join(event["event"].get("text", "") for event in events) == "α = 1."
    assert state.events(owner["user_id"], org, runs[0]["id"], events[-1]["sequence"]) == []
    with state.store.transaction() as db:
        for table in ("llm_requests", "llm_runs", "llm_events"):
            assert KEY not in str([dict(row) for row in db.execute(f"SELECT * FROM {table}")])


def test_competing_requests_retain_estimates_without_a_local_spending_cap(installation):
    _, state, _, owner, topics = prepare(installation)
    first = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    reserve = first["reserved_microusd"]
    state.cancel(owner["user_id"], owner["organization_id"], first["id"])
    state.store.set_policy(owner["user_id"], owner["organization_id"], MODELS, reserve)

    def attempt(index):
        try:
            enqueue(state, owner, topics[1]["id"], models=MODELS[:1], client=str(index))
            return 202
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [202, 202]
    assert state.balance(owner["user_id"], owner["organization_id"])["reserved_microusd"] == 2 * reserve


def test_replay_is_idempotent_and_cannot_change_models_or_context(installation):
    _, state, _, owner, topics = prepare(installation)
    first = enqueue(state, owner, topics[1]["id"])
    assert enqueue(state, owner, topics[1]["id"]) == first
    with pytest.raises(StoreError, match="different model request"):
        enqueue(state, owner, topics[1]["id"], models=MODELS[:1])
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 2


@pytest.mark.parametrize(
    "mode,code",
    [
        ("error", "provider_stream_error"),
        ("cutoff", "provider_incomplete"),
        ("missing_usage", "usage_unavailable"),
        ("bad_usage", "provider_protocol"),
    ],
)
def test_uncertain_failures_keep_budget_and_partial_answer_without_secret_errors(installation, mode, code):
    _, state, _, owner, topics = prepare(installation)
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    worker = InferenceWorker(state, Fixture(mode).provider)
    worker.start()
    try:
        wait(lambda: state.run(owner["user_id"], owner["organization_id"], run["id"])["status"] == "failed")
    finally:
        worker.stop()
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["accounting_state"] == "uncertain" and result["error_code"] == code
    assert state.balance(owner["user_id"], owner["organization_id"])["uncertain_microusd"] == result["reserved_microusd"]
    assert result["reserved_microusd"] >= run["reserved_microusd"]
    assert KEY not in json.dumps(result)
    assert "Partial response: failed" in state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])[-1]["content"]


def test_explicit_provider_rejection_releases_reservation(installation):
    _, state, _, owner, topics = prepare(installation)
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    worker = InferenceWorker(state, Fixture("reject").provider)
    worker.start()
    try:
        wait(lambda: state.run(owner["user_id"], owner["organization_id"], run["id"])["status"] == "failed")
    finally:
        worker.stop()
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["accounting_state"] == "released" and result["error_code"] == "provider_rejected"
    assert KEY not in json.dumps(result)


def test_cancel_closes_silent_provider_stream_and_never_replays_it(installation):
    config, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture("slow")
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: bool(state.run(user, org, run["id"])["output_text"]))
        state.cancel(user, org, run["id"])
        wait(lambda: state.run(user, org, run["id"])["status"] == "cancelled")
        assert fixture.closed.is_set()
    finally:
        worker.stop()
    restarted = InferenceStore(Store(config.data_dir))
    second = InferenceWorker(restarted, fixture.provider)
    second.start()
    try:
        time.sleep(0.2)
        assert len([request for request in fixture.requests if request.method == "POST"]) == 1
        assert restarted.run(user, org, run["id"])["accounting_state"] == "uncertain"
    finally:
        second.stop()


@pytest.mark.parametrize("mutation", ["remove", "viewer", "policy", "key", "budget"])
def test_authorization_changes_stop_queued_work_before_provider_submission(installation, mutation):
    _, state, client, owner, topics = prepare(installation)
    _, _, member = invited(installation)
    actor = {**owner, "user_id": member["user_id"]}
    run = enqueue(state, actor, topics[1]["id"], models=MODELS[:1])[0]
    if mutation in {"remove", "viewer"}:
        state.store.change_member(owner["user_id"], owner["organization_id"], member["user_id"], None if mutation == "remove" else "viewer")
    elif mutation == "key":
        key = state.store.keys(owner["user_id"], owner["organization_id"])[0]
        state.store.disable_key(owner["user_id"], owner["organization_id"], key["id"])
    else:
        state.store.set_policy(
            owner["user_id"], owner["organization_id"], [] if mutation == "policy" else MODELS, 0 if mutation == "budget" else 1_000_000
        )
    if mutation in {"policy", "budget"}:
        assert state.claim()["id"] == run["id"]
        assert state.run(owner["user_id"], owner["organization_id"], run["id"])["status"] == "running"
        return
    assert state.claim() is None
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "cancelled" and result["accounting_state"] == "released"


def test_restart_marks_abandoned_running_work_interrupted_and_fences_old_finish(installation):
    config, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    abandoned = state.claim()
    state.progress(abandoned, "Partial generation", "gen-interrupted")
    recovered = InferenceStore(Store(config.data_dir))
    recovered.recover()
    assert recovered.run(user, org, run["id"])["status"] == "interrupted"
    assert not state.finish(abandoned, "succeeded", usage=Usage(1, 1, 1))
    assert not state.progress(abandoned, "Partial generation but stale", "gen-interrupted")
    assert len([message for message in state.store.messages(user, org, topics[1]["id"]) if message["author_kind"] == "assistant"]) == 1


def test_worker_lock_excludes_a_second_executor_and_preserves_active_work(installation):
    _, state, _, _, _ = prepare(installation)
    first = InferenceWorker(state, Fixture().provider)
    first.start()
    try:
        with pytest.raises(ValueError, match="another inference worker"):
            InferenceWorker(state, Fixture().provider).start()
    finally:
        first.stop()


def test_api_tenant_roles_replay_events_and_admin_reconciliation(installation):
    config, state, _, owner, topics = prepare(installation)
    fixture = Fixture()
    client = TestClient(
        create_app(config, provider=fixture.provider),
        base_url=config.public_url,
        headers={"Authorization": "Bearer " + owner["access_token"]},
    )
    org = owner["organization_id"]
    path = f"/api/organizations/{org}/topics/{topics[1]['id']}/inference-runs"
    body = {"models": MODELS, "client_id": "api-request"}
    first = client.post(path, json=body)
    assert first.status_code == 202, first.text
    assert client.post(path, json=body).json() == first.json()
    assert len(fixture.requests) == 1
    run = first.json()[0]
    run_path = f"/api/organizations/{org}/inference-runs/{run['id']}"
    assert KEY not in json.dumps(first.json()) and "key_id" not in run and "context_json" not in run
    assert client.get(run_path + "/events").json()[0]["event"]["status"] == "queued"
    assert client.post(path, json={**body, "models": ["not/allowed"]}).status_code == 409
    unavailable = client.post(path, json={**body, "client_id": "other", "models": ["not/allowed"]})
    assert unavailable.status_code == 502 and unavailable.json()["detail"] == "model_unavailable"
    for topic in (topics[0], topics[2]):
        assert client.post(path.replace(topics[1]["id"], topic["id"]), json={**body, "client_id": topic["id"]}).status_code == 400
    _, viewer, _ = invited(installation, role="viewer", email="viewer@example.org")
    assert viewer.get(path).status_code == 200
    assert viewer.post(path, json=body).status_code == 403
    assert viewer.post(run_path + "/cancel", json={}).status_code == 403
    _, member, person = invited(installation, email="person@example.org")
    assert member.post(run_path + "/cancel", json={}).status_code == 403
    _, foreign_admin, foreign_person = invited(installation, role="admin", email="foreign-admin@example.org")
    installation[1].change_member(owner["user_id"], org, foreign_person["user_id"], "owner")
    foreign = foreign_admin.post("/api/organizations", json={"name": "Foreign", "credential_mode": "none"}).json()["id"]
    for suffix in ("", "/events"):
        assert client.get(run_path.replace(org, foreign) + suffix).status_code == 404
    assert client.post(run_path.replace(org, foreign) + "/cancel", json={}).status_code == 404
    row = state.claim()
    state.finish(row, "failed", error="unknown_cost")
    actual_path = f"/api/organizations/{org}/inference-runs/{row['id']}/cost"
    assert member.put(actual_path, json={"actual_microusd": 9}).status_code == 403
    assert client.put(actual_path, json={"actual_microusd": 9}).status_code == 204
    assert client.put(actual_path, json={"actual_microusd": 9}).status_code == 409
    assert state.balance(owner["user_id"], org)["spent_microusd"] == 9


def test_decimal_costs_and_endpoint_validation_fail_closed():
    assert microusd("0.0000001") == 1 and microusd("0") == 0
    for value in ("NaN", "Infinity", "-1", True, None):
        with pytest.raises(ProviderError):
            microusd(value)
    for url in (
        "https://evil.example.org",
        "http://192.168.1.1",
        "http://localhost.evil.org",
        "http://user@localhost",
        "http://localhost?q=x",
        "http://localhost\n",
        "http://localhost\\@evil.org",
    ):
        with pytest.raises(ValueError):
            provider_origin(url)
    assert provider_origin("http://127.0.0.1:8000/api/v1/") == "http://127.0.0.1:8000/api/v1"


@pytest.mark.parametrize("mutation", ["remove", "viewer", "policy", "key", "budget"])
def test_authorization_changes_close_an_already_running_stream(installation, mutation):
    _, state, _, owner, topics = prepare(installation)
    _, _, member = invited(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, {**owner, "user_id": member["user_id"]}, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture("slow")
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: bool(state.run(user, org, run["id"])["output_text"]))
        if mutation in {"remove", "viewer"}:
            state.store.change_member(user, org, member["user_id"], None if mutation == "remove" else "viewer")
        elif mutation == "key":
            state.store.disable_key(user, org, state.store.keys(user, org)[0]["id"])
        else:
            state.store.set_policy(user, org, [] if mutation == "policy" else MODELS, 0 if mutation == "budget" else 1_000_000)
        if mutation in {"policy", "budget"}:
            time.sleep(0.2)
            assert state.run(user, org, run["id"])["status"] == "running"
            return
        wait(lambda: state.run(user, org, run["id"])["status"] == "cancelled")
        assert fixture.closed.is_set()
        assert state.run(user, org, run["id"])["accounting_state"] == "uncertain"
    finally:
        worker.stop()


@pytest.mark.parametrize("mode,expected", [("slow", "uncertain"), ("after_usage", "settled")])
def test_timeout_closes_transport_and_preserves_known_or_unknown_cost(installation, mode, expected):
    _, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture(mode)
    worker = InferenceWorker(state, fixture.provider, timeout=1)
    worker.start()
    try:
        wait(lambda: state.run(user, org, run["id"])["status"] == "cancelled")
        result = state.run(user, org, run["id"])
        assert result["error_code"] == "execution_timeout" and result["accounting_state"] == expected
        assert fixture.closed.is_set()
        if expected == "settled":
            assert result["actual_microusd"] == 8
    finally:
        worker.stop()


def test_failed_worker_start_releases_exclusive_lock(installation, monkeypatch):
    _, state, _, _, _ = prepare(installation)
    worker = InferenceWorker(state, Fixture().provider)
    with monkeypatch.context() as changes:
        changes.setattr(state, "recover", lambda: (_ for _ in ()).throw(RuntimeError("fixture recovery failure")))
        with pytest.raises(RuntimeError, match="fixture recovery failure"):
            worker.start()
    worker.start()
    worker.stop()


def test_recovery_keeps_known_usage_and_resumes_only_never_submitted_queue(installation):
    config, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    enqueue(state, owner, topics[1]["id"])
    abandoned = state.claim()
    state.progress(abandoned, "Paid partial result", "gen-abandoned", Usage(11, 20, 3))
    fixture = Fixture()
    recovered = InferenceStore(Store(config.data_dir))
    worker = InferenceWorker(recovered, fixture.provider)
    worker.start()
    try:
        wait(lambda: any(run["status"] == "succeeded" for run in recovered.runs(user, org, topics[1]["id"])))
        assert recovered.run(user, org, abandoned["id"])["status"] == "interrupted"
        assert recovered.run(user, org, abandoned["id"])["actual_microusd"] == 11
        assert recovered.balance(user, org)["spent_microusd"] == 19
        assert len(fixture.requests) == 1
    finally:
        worker.stop()


def test_api_lifespan_executes_queued_requests_and_stops_worker(installation):
    config, _, _, owner, topics = prepare(installation)
    fixture = Fixture()
    app = create_app(config, provider=fixture.provider)
    org = owner["organization_id"]
    with TestClient(app, base_url=config.public_url, headers={"Authorization": "Bearer " + owner["access_token"]}) as client:
        assert client.get(f"/api/organizations/{org}/inference-configuration").json()["worker_running"]
        result = client.post(
            f"/api/organizations/{org}/topics/{topics[1]['id']}/inference-runs", json={"models": MODELS[:1], "client_id": "lifespan"}
        )
        assert result.status_code == 202
        path = f"/api/organizations/{org}/inference-runs/{result.json()[0]['id']}"
        wait(lambda: client.get(path).json()["status"] == "succeeded")
    assert not app.state.inference_worker.running


def test_provider_output_limits_continue_to_a_complete_answer_with_all_charges(installation):
    _, state, _, owner, topics = prepare(installation)
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture("truncated")
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: state.run(owner["user_id"], owner["organization_id"], run["id"])["status"] == "succeeded")
        result = state.run(owner["user_id"], owner["organization_id"], run["id"])
        assert result["error_code"] is None and result["actual_microusd"] == 48
        assert result["accounting_state"] == "settled"
        assert len(fixture.requests) == 6
        assert result["output_text"] == "α = 1." * 6
        message = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])[-1]
        assert "Partial response" not in message["content"]
    finally:
        worker.stop()


def test_inference_cookie_writes_require_origin_and_csrf(installation):
    config, state, _, owner, topics = prepare(installation)
    fixture = Fixture()
    client = TestClient(create_app(config, provider=fixture.provider), base_url=config.public_url)
    session = client.post("/api/auth/login", json={"email": "owner@example.org", "password": "a very long test password"}).json()
    root = f"/api/organizations/{owner['organization_id']}"
    path = root + f"/topics/{topics[1]['id']}/inference-runs"
    body = {"models": MODELS[:1], "client_id": "cookie-request"}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"Origin": config.public_url}).status_code == 403
    headers = {"Origin": config.public_url, "X-CSRF-Token": session["csrf_token"]}
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 202
    run = result.json()[0]
    run_path = root + f"/inference-runs/{run['id']}"
    assert client.post(run_path + "/cancel", json={}).status_code == 403
    assert client.post(run_path + "/cancel", json={}, headers=headers).status_code == 200
    assert client.put(run_path + "/cost", json={"actual_microusd": 0}).status_code == 403
    assert len(fixture.requests) == 1


def test_context_admission_and_bound_validation_never_enqueue_invalid_requests(installation):
    config, state, _, owner, topics = prepare(installation)
    fixture = Fixture()
    client = TestClient(
        create_app(config, provider=fixture.provider),
        base_url=config.public_url,
        headers={"Authorization": "Bearer " + owner["access_token"]},
    )
    root = f"/api/organizations/{owner['organization_id']}"
    path = root + f"/topics/{topics[1]['id']}/inference-runs"
    body = {"models": MODELS[:1], "client_id": "invalid"}
    for changes in (
        {"models": [MODELS[0], MODELS[0]]},
        {"max_completion_tokens": 2**31},
        {"context_message_limit": True},
        {"through_sequence": True},
        {"through_sequence": -1},
    ):
        assert client.post(path, json={**body, **changes}).status_code in {400, 422}
    assert fixture.requests == []
    foreign = client.post(root + f"/topics/{topics[0]['id']}/messages", json={"content": "Foreign topic", "client_id": "foreign"}).json()
    assert client.post(path, json={**body, "through_sequence": foreign["sequence"]}).status_code == 400
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 0


def test_request_rate_is_durable_and_replay_does_not_charge_a_slot(installation):
    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    for index in range(20):
        run = enqueue(state, owner, topic, client=str(index), models=MODELS[:1])[0]
        state.cancel(user, org, run["id"])
    assert enqueue(state, owner, topic, client="0", models=MODELS[:1])[0]["status"] == "cancelled"
    with pytest.raises(StoreError, match="too many model requests"):
        enqueue(state, owner, topic, client="21", models=MODELS[:1])


def test_active_capacity_is_atomic_and_cancellation_opens_a_slot(installation):
    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    runs = [enqueue(state, owner, topic, client=str(index), models=MODELS[:1])[0] for index in range(8)]
    balance = state.balance(user, org)
    with pytest.raises(StoreError, match="eight queued or active"):
        enqueue(state, owner, topic, client="overflow", models=MODELS[:1])
    assert state.balance(user, org) == balance
    state.cancel(user, org, runs[0]["id"])
    assert enqueue(state, owner, topic, client="replacement", models=MODELS[:1])[0]["status"] == "queued"


def test_shutdown_interrupts_and_closes_a_running_provider_call_without_retry(installation):
    _, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    fixture = Fixture("slow")
    worker = InferenceWorker(state, fixture.provider)
    worker.start()
    try:
        wait(lambda: bool(state.run(user, org, run["id"])["output_text"]))
    finally:
        worker.stop()
    assert fixture.closed.is_set()
    result = state.run(user, org, run["id"])
    assert result["status"] == "interrupted" and result["accounting_state"] == "uncertain"
    assert len(fixture.requests) == 1


def test_uncertain_cost_from_previous_month_still_holds_a_reservation(installation):
    _, state, _, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    run = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    row = state.claim()
    state.finish(row, "interrupted")
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_requests SET created_at=1")
        db.execute("UPDATE llm_runs SET started_at=1,finished_at=1")
    assert state.balance(user, org)["uncertain_microusd"] == run["reserved_microusd"]
    state.reconcile(user, org, run["id"], 0)
    assert state.balance(user, org)["uncertain_microusd"] == 0
    assert any(event["action"] == "inference.cost-reconcile" for event in state.store.audit(user, org))


def test_key_rotation_uses_creation_order_not_random_ids_or_timestamp_ties(installation):
    _, state, _, owner, topics = prepare(installation)
    user, org = owner["user_id"], owner["organization_id"]
    original = state.store.keys(user, org)[0]
    replacement = state.store.add_key(user, org, "openrouter", "Replacement", "sk-or-replacement-fixture-only")
    with state.store.transaction(write=True) as db:
        # Creation seconds and random UUID ordering are not rotation ordering.
        db.execute("UPDATE provider_keys SET created_at=1 WHERE id=?", (replacement["id"],))
        db.execute("UPDATE provider_keys SET created_at=9999999999 WHERE id=?", (original["id"],))
    first = enqueue(state, owner, topics[1]["id"], models=MODELS[:1])[0]
    row = state.claim()
    assert row["id"] == first["id"] and row["key_id"] == replacement["id"]
    assert state.execution_key(row) == "sk-or-replacement-fixture-only"
    state.store.disable_key(user, org, replacement["id"])
    assert state.stopped(row) == "key_disabled"
