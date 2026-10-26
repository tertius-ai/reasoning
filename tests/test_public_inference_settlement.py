"""Uncertain charges settle from the provider's generation record, never by replaying a request."""

import asyncio
import json

import httpx
import pytest
from test_public_inference import KEY, QUOTES, enqueue, prepare
from test_public_server import installation as installation

from reasoning_ledger.server.inference_settlement import LOOKUP_DELAYS, InferenceSettlement
from reasoning_ledger.server.openrouter import OpenRouter
from reasoning_ledger.server.usage_store import UsageStore

GENERATION = "gen-1760000000-settleFixture"


class Provider:
    """A generation-statistics endpoint; any other request fails the test."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.provider = OpenRouter(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/v1/generation"
        assert request.headers["Authorization"] == "Bearer " + KEY
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def record(generation=GENERATION, cost=0.000123, **fields):
    return httpx.Response(200, json={"data": {
        "id": generation, "total_cost": cost, "usage": cost, "tokens_prompt": 40, "tokens_completion": 7,
        "native_tokens_reasoning": 3, "cancelled": True, "streamed": True, **fields,
    }})


def uncertain(installation, generation=GENERATION):
    """A submitted call stopped before usage arrived, as a cancelled comparison leaves it."""
    _, state, client, owner, topics = prepare(installation)
    run = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    row = state.claim()
    ledger = UsageStore(state.store)
    call = ledger.begin_call(row, QUOTES[0])
    ledger.finish_call(call, status="cancelled", provider_generation_id=generation, error_code="execution_cancelled")
    state.finish(row, "cancelled", error="execution_cancelled", accounting_unknown=True)
    view = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert view["accounting_state"] == "uncertain"
    return state, client, owner, view, call


def settle(state, provider, finished, delay):
    return asyncio.run(InferenceSettlement(state).run(provider.provider, now=finished + delay))


def lookups(state, owner, run):
    return [
        event["event"] for event in state.events(owner["user_id"], owner["organization_id"], run["id"], 0)
        if event["event"]["type"] == "accounting_lookup"
    ]


def test_provider_record_settles_a_cancelled_call_and_releases_its_hold(installation):
    state, client, owner, run, call = uncertain(installation)
    user, org = owner["user_id"], owner["organization_id"]
    assert state.balance(user, org)["uncertain_microusd"] == run["reserved_microusd"]
    provider = Provider(record())
    assert settle(state, provider, run["finished_at"], LOOKUP_DELAYS[0] - 1) == 0
    assert provider.requests == []
    assert settle(state, provider, run["finished_at"], LOOKUP_DELAYS[0]) == 1
    assert [str(request.url) for request in provider.requests] == ["https://openrouter.ai/api/v1/generation?id=" + GENERATION]
    settled = state.run(user, org, run["id"])
    assert settled["accounting_state"] == "settled" and settled["actual_microusd"] == 123
    assert settled["prompt_tokens"] == 40 and settled["completion_tokens"] == 7
    assert settled["status"] == "cancelled"
    assert state.balance(user, org)["uncertain_microusd"] == 0
    detail = client.get(f"/api/organizations/{org}/inference-ledger/{call}").json()
    assert detail["accounting_state"] == "settled" and detail["actual_microusd"] == 123
    assert detail["receipts"][-1]["kind"] == "provider_settlement"
    assert detail["receipts"][-1]["data"]["settled_from"] == "provider_generation"
    assert detail["receipts"][-1]["data"]["provider_cancelled"] is True
    assert detail["receipts"][-1]["data"]["reasoning_tokens"] == 3
    assert detail["receipts"][-2]["data"]["accounting_state"] == "uncertain"
    assert lookups(state, owner, run) == [
        {"type": "accounting_lookup", "call_id": call, "attempt": 1, "outcome": "settled", "cost_microusd": 123}
    ]
    assert any(event["action"] == "inference.cost-settle" and event["target_id"] == call and event["actor_id"] is None
               for event in state.store.audit(user, org))
    assert settle(state, provider, run["finished_at"], 10**6) == 0
    assert len(provider.requests) == 1
    with pytest.raises(Exception, match="only uncertain costs"):
        state.reconcile(user, org, run["id"], 0)


@pytest.mark.parametrize("generation", [None, "chatcmpl-upstream-only"])
def test_call_without_a_provider_generation_stays_uncertain_for_manual_reconciliation(installation, generation):
    state, _, owner, run, _ = uncertain(installation, generation)
    user, org = owner["user_id"], owner["organization_id"]
    provider = Provider(record())
    for delay in (*LOOKUP_DELAYS, 10**6):
        assert settle(state, provider, run["finished_at"], delay) == 0
    assert provider.requests == [] and lookups(state, owner, run) == []
    assert state.run(user, org, run["id"])["accounting_state"] == "uncertain"
    assert state.balance(user, org)["uncertain_microusd"] == run["reserved_microusd"]
    state.reconcile(user, org, run["id"], 50)
    assert state.run(user, org, run["id"])["actual_microusd"] == 50


@pytest.mark.parametrize("response,outcome", [
    (httpx.Response(404, json={"error": {"code": 404, "message": "Resource not found"}}), "not_found"),
    (httpx.Response(500, json={"error": {"code": 500, "message": "Internal Server Error"}}), "generation_unavailable"),
    (httpx.ConnectError("unreachable"), "generation_unavailable"),
    (record(generation="gen-another-generation"), "provider_protocol"),
    (record(cost=-1), "provider_protocol"),
])
def test_failed_lookups_follow_a_bounded_schedule_and_keep_the_charge_uncertain(installation, response, outcome):
    state, _, owner, run, call = uncertain(installation)
    user, org = owner["user_id"], owner["organization_id"]
    provider = Provider(response)
    for attempt, delay in enumerate(LOOKUP_DELAYS, start=1):
        assert settle(state, provider, run["finished_at"], delay) == 0
        assert settle(state, provider, run["finished_at"], delay) == 0, "an attempt is never repeated early"
        assert len(provider.requests) == attempt
    assert settle(state, provider, run["finished_at"], 10**6) == 0
    assert len(provider.requests) == len(LOOKUP_DELAYS)
    events = lookups(state, owner, run)
    assert [event["attempt"] for event in events] == list(range(1, len(LOOKUP_DELAYS) + 1))
    assert {event["outcome"] for event in events} == {outcome}
    assert events[-1]["final"] is True and not any(event.get("final") for event in events[:-1])
    assert state.run(user, org, run["id"])["accounting_state"] == "uncertain"
    assert state.balance(user, org)["uncertain_microusd"] == run["reserved_microusd"]
    assert not any(event["action"] == "inference.cost-settle" for event in state.store.audit(user, org))
    state.reconcile(user, org, run["id"], 70)
    assert state.run(user, org, run["id"])["accounting_state"] == "settled"
    assert json.dumps(events).find(KEY) == -1


def test_a_late_record_settles_on_a_later_attempt(installation):
    state, _, owner, run, _ = uncertain(installation)
    provider = Provider(httpx.Response(404, json={"error": {"code": 404}}), record(cost=0))
    assert settle(state, provider, run["finished_at"], LOOKUP_DELAYS[0]) == 0
    assert settle(state, provider, run["finished_at"], LOOKUP_DELAYS[1]) == 1
    settled = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert settled["accounting_state"] == "settled" and settled["actual_microusd"] == 0
    assert [event["outcome"] for event in lookups(state, owner, run)] == ["not_found", "settled"]


def test_manual_reconciliation_during_a_lookup_wins_and_is_not_overwritten(installation):
    state, client, owner, run, call = uncertain(installation)
    user, org = owner["user_id"], owner["organization_id"]
    settlement = InferenceSettlement(state)
    due = settlement.due(run["finished_at"] + LOOKUP_DELAYS[0])
    assert [item["call_id"] for item in due] == [call]
    state.reconcile(user, org, run["id"], 90)
    record_body = {"cost_microusd": 123, "prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": None,
                   "reasoning_tokens": None, "cancelled": False}
    assert settlement.record(due[0], "settled", record_body) is False
    assert state.run(user, org, run["id"])["actual_microusd"] == 90
    receipts = client.get(f"/api/organizations/{org}/inference-ledger/{call}").json()["receipts"]
    assert [receipt["kind"] for receipt in receipts][-1] == "reconciliation"


def test_disabled_key_is_never_used_for_a_lookup(installation):
    state, client, owner, run, _ = uncertain(installation)
    org = owner["organization_id"]
    key = state.store.keys(owner["user_id"], org)[0]
    assert client.delete(f"/api/organizations/{org}/provider-keys/{key['id']}").status_code in {200, 204}
    provider = Provider(record())
    assert settle(state, provider, run["finished_at"], LOOKUP_DELAYS[0]) == 0
    assert provider.requests == []
    assert [event["outcome"] for event in lookups(state, owner, run)] == ["key_unavailable"]


def test_regional_routing_reads_the_generation_from_the_same_region(installation):
    state, _, _, run, _ = uncertain(installation)
    settlement = InferenceSettlement(state)
    due = settlement.due(run["finished_at"] + LOOKUP_DELAYS[0])
    provider = Provider(record())
    routing = {"strategy": "cheapest", "zdr": False, "region": "eu"}
    regional = {**due[0], "quote_json": json.dumps({**json.loads(due[0]["quote_json"]), "routing": routing})}
    settlement.due = lambda now=None, limit=16: [regional]
    assert asyncio.run(settlement.run(provider.provider)) == 1
    assert str(provider.requests[0].url).startswith("https://eu.openrouter.ai/api/v1/generation?")
