"""Provider-owned daily limits: read-only reporting and safe temporary-credit recovery."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from test_public_inference import KEY, MODELS, prepare
from test_public_research_agent import ResearchFixture, turn
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.api import create_app
from reasoning_ledger.server.openrouter import OpenRouter
from reasoning_ledger.server.provider_budget import ProviderBudget
from reasoning_ledger.server.research_provider import ResearchCompletion
from reasoning_ledger.server.usage_store import UsageStore


def key_data(**changes):
    return {"usage_daily": "12.250001", "limit": 50, "limit_remaining": "37.749999", "limit_reset": "daily",
            "label": KEY, "workspace_id": KEY, **changes}


def environment(installation, data=None):
    config, state, _, owner, _ = prepare(installation)
    requests = []
    def handle(request):
        assert request.method == "GET" and request.url.path == "/api/v1/key"
        assert request.headers["Authorization"] == "Bearer " + KEY
        requests.append(request)
        return httpx.Response(200, json={"data": data if data is not None else key_data()})
    provider = OpenRouter(transport=httpx.MockTransport(handle))
    client = TestClient(create_app(config, provider=provider), base_url=config.public_url,
                        headers={"Authorization": "Bearer " + owner["access_token"]})
    return state, owner, client, provider, requests


def test_daily_key_status_is_member_visible_secret_free_and_read_only(installation):
    _, owner, client, _, requests = environment(installation)
    path = f"/api/organizations/{owner['organization_id']}/provider-daily-spend"
    result = client.get(path)
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    assert result.json()["status"] == "ready" and result.json()["spent_microusd"] == 12_250_001
    assert result.json()["limit_microusd"] == 50_000_000 and result.json()["remaining_microusd"] == 37_749_999
    assert KEY not in result.text and "label" not in result.text and "workspace_id" not in result.text
    _, _, member = invited(installation, role="viewer")
    assert client.get(path, headers={"Authorization": "Bearer " + member["access_token"]}).status_code == 200
    assert len(requests) == 1
    assert client.get(path.replace(owner["organization_id"], "f" * 32)).status_code in {403, 404}


@pytest.mark.parametrize("data,status", [
    (key_data(limit=None, limit_remaining=None), "no_daily_limit"),
    (key_data(limit_reset="monthly"), "no_daily_limit"),
    (key_data(limit=0, limit_remaining=-1), "ready"),
    (key_data(usage_daily=None), "unavailable"),
    (key_data(usage_daily=True), "unavailable"),
    (key_data(limit="NaN"), "unavailable"),
    (key_data(include_byok_in_limit=True, byok_usage_daily="2.5"), "ready"),
])
def test_missing_malformed_or_non_daily_limits_are_never_presented_as_daily_zero(installation, data, status):
    _, owner, client, _, _ = environment(installation, data)
    result = client.get(f"/api/organizations/{owner['organization_id']}/provider-daily-spend").json()
    assert result["status"] == status
    if status != "ready":
        assert result["limit_microusd"] is None and result["remaining_microusd"] is None
    if status == "unavailable":
        assert result["spent_microusd"] is None and result["checked_at"] is None
    if data.get("include_byok_in_limit"):
        assert result["spent_microusd"] == 14_750_001


def test_budget_reads_coalesce_and_refresh_at_midnight_and_after_key_rotation(installation):
    state, owner, _, provider, requests = environment(installation)
    now = [datetime(2026, 10, 27, 23, 59, 59, tzinfo=UTC).timestamp()]
    budget = ProviderBudget(state.store, provider, now=lambda: now[0], clock=lambda: now[0])
    async def read_many():
        values = await asyncio.gather(*(budget.read(owner["user_id"], owner["organization_id"]) for _ in range(30)))
        assert len(requests) == 1 and all(value["status"] == "ready" for value in values)
        now[0] += 2
        await budget.read(owner["user_id"], owner["organization_id"])
        assert len(requests) == 2  # The 30-second cache never crosses a UTC day.
        state.store.add_key(owner["user_id"], owner["organization_id"], "openrouter", "Replacement", KEY)
        await budget.read(owner["user_id"], owner["organization_id"])
        assert len(requests) == 3
    asyncio.run(read_many())


def test_rotation_or_revocation_during_provider_lookup_cannot_publish_old_metadata(installation):
    state, owner, _, provider, _ = environment(installation)
    calls = []
    async def changing(request):
        calls.append(request)
        state.store.add_key(owner["user_id"], owner["organization_id"], "openrouter", "Replacement", KEY)
        return httpx.Response(200, json={"data": key_data()})
    provider.transport = httpx.MockTransport(changing)
    result = asyncio.run(ProviderBudget(state.store, provider).read(owner["user_id"], owner["organization_id"]))
    assert result["status"] == "unavailable" and result["spent_microusd"] is None
    assert len(calls) == 1


def test_provider_outage_cannot_block_inference_or_turn_missing_usage_into_zero(installation):
    state, owner, _, provider, _ = environment(installation)
    provider.transport = httpx.MockTransport(lambda _: httpx.Response(503, content=KEY))
    result = asyncio.run(ProviderBudget(state.store, provider).read(owner["user_id"], owner["organization_id"]))
    assert result["status"] == "unavailable" and result["spent_microusd"] is None and KEY not in json.dumps(result)
    # A retained zero monthly setting is not an inference admission authority.
    state.store.set_policy(owner["user_id"], owner["organization_id"], MODELS, 0)
    with state.store.transaction() as db:
        topic = db.execute("SELECT id FROM topics WHERE kind='beta'").fetchone()[0]
    from test_public_inference import QUOTES

    runs = state.enqueue(owner["user_id"], owner["organization_id"], topic, "no-local-cap",
                         state.parameters([MODELS[0]], 256, 50, None), [QUOTES[0]])
    row = state.claim()
    assert runs[0]["id"] == row["id"] and state.execution_key(row) == KEY
    state.reserve_next_call(row, 10**12)
    assert state.stopped(row) is None


@pytest.mark.parametrize("status,source,reason,attempts", [
    (402, "openrouter_credits", None, 2),
    (402, "openrouter_in_flight_budget", "in_flight_budget_exhausted", 2),
    (402, "openrouter_key_limit", None, 1),
    (402, "openrouter_credits", "weight_exceeds_budget", 1),
    (403, "openrouter_key_limit", None, 1),
    (403, "openrouter_workspace_budget", None, 1),
])
def test_only_temporary_pre_generation_credit_refusals_retry(installation, monkeypatch, status, source, reason, attempts):
    fixture = ResearchFixture("plain")
    original = fixture.handle
    waits, refused = [], []
    def handle(request):
        if request.method == "POST" and not refused:
            refused.append(True)
            return httpx.Response(status, headers={"Retry-After": "7"}, json={"error": {
                "message": KEY, "metadata": {"limit_source": source, **({"reason": reason} if reason else {})}}})
        return original(request)
    fixture.provider.transport = httpx.MockTransport(handle)
    async def wait(self, seconds):
        waits.append(seconds)
    monkeypatch.setattr(ResearchCompletion, "_wait_for_credit", wait)
    state, owner, _, row = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    calls = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]
    assert len(calls) == attempts
    assert waits == ([7] if attempts == 2 else [])
    assert result["status"] == ("succeeded" if attempts == 2 else "failed")
    assert result["accounting_state"] == ("settled" if attempts == 2 else "released")
    assert result["actual_microusd"] == (8 if attempts == 2 else None)
    if attempts == 2:
        assert result["diagnostics"] is None and result["error_code"] is None
    if status == 403:
        assert result["diagnostics"]["category"] == "budget"


def test_credit_retry_is_bounded_and_preserves_no_charge_state(installation, monkeypatch):
    fixture = ResearchFixture("plain")
    fixture.provider.transport = httpx.MockTransport(lambda _: httpx.Response(402, json={"error": {
        "metadata": {"limit_source": "openrouter_credits"}}}))
    waits = []
    async def wait(self, seconds):
        waits.append(seconds)
    monkeypatch.setattr(ResearchCompletion, "_wait_for_credit", wait)
    state, owner, _, row = turn(installation, fixture)
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["accounting_state"] == "released"
    assert waits == [5, 15, 30]
    assert len(UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]) == 4


def test_cancel_during_credit_wait_stops_without_another_submission(installation, monkeypatch):
    fixture = ResearchFixture("plain")
    fixture.provider.transport = httpx.MockTransport(lambda _: httpx.Response(402, json={"error": {
        "metadata": {"limit_source": "openrouter_credits"}}}))
    async def wait(self, seconds):
        self.inference.cancel(self.row["requested_by"], self.row["organization_id"], self.row["id"])
        self.next_cancel_check = 0
        self._check_cancel()
    monkeypatch.setattr(ResearchCompletion, "_wait_for_credit", wait)
    state, owner, _, row = turn(installation, fixture)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "cancelled" and run["accounting_state"] == "released"
    assert len(UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]) == 1


def test_credit_error_after_generation_is_not_resubmitted(installation, monkeypatch):
    from test_public_provider_stream import content, event

    fixture = ResearchFixture("plain")
    wire = event(content("Received work.")) + event({"error": {"code": 402, "metadata": {"limit_source": "openrouter_credits"}}})
    fixture.provider.transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=wire))
    async def must_not_wait(*args):
        pytest.fail("an accepted stream cannot be replayed after a credit error")
    monkeypatch.setattr(ResearchCompletion, "_wait_for_credit", must_not_wait)
    state, owner, _, row = turn(installation, fixture)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "failed" and run["accounting_state"] == "uncertain"
    assert run["output_text"] == "Received work."
    assert len(UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]) == 1
