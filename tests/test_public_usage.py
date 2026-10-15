"""Reported usage, organization isolation and immutable scenario estimates; no provider billing."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from public_schema_fixture import remove_collaboration_schema
from test_public_inference import Fixture, enqueue, prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.api import create_app
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.openrouter import OpenRouter, Usage
from reasoning_ledger.server.store import Store
from reasoning_ledger.server.usage_store import REFERENCE_KWH_PER_MILLION

PROFILE = {"model": "test/one", "input_kwh_per_million": 10.0, "output_kwh_per_million": 100.0,
           "location": "Assumed region", "grid_grams_co2e_per_kwh": 400.0, "source": "Test scenario, not observed provider data"}
REPORTED_USAGE = Usage(8, 20, 5)


def test_public_routing_availability_is_optional_cached_and_not_inference():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and "Authorization" not in request.headers
        if request.url.params.get("zdr") or request.url.params.get("region") == "us":
            ids = ["open/model"]
        elif request.url.params.get("region") == "eu":
            ids = []
        else:
            return httpx.Response(200, json={"data": [{"id": "open/model", "hugging_face_id": "owner/weights",
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "context_length": 10000, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]})
        return httpx.Response(200, json={"data": [{"id": identity} for identity in ids]})

    provider = OpenRouter(transport=httpx.MockTransport(handle))
    models = asyncio.run(provider.prices(include_routing=True))
    assert len(requests) == 4
    assert models[0]["weights_repository"] == "owner/weights"
    assert models[0]["routing_availability"]["zdr"] is True
    assert models[0]["routing_availability"]["regions"] == ["US"]
    asyncio.run(provider.prices(include_routing=True))
    assert len(requests) == 4


def test_unavailable_routing_metadata_does_not_invent_privacy_or_region_flags():
    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    result = asyncio.run(provider.routing_availability())
    assert result == {"zdr": None, "us": None, "eu": None}


@pytest.mark.parametrize("body", [[], {"data": None}, {"data": "invalid"}])
def test_malformed_optional_routing_metadata_does_not_break_catalog(body):
    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    assert asyncio.run(provider.routing_availability()) == {"zdr": None, "us": None, "eu": None}


def finish(state, owner, topic, *, client="request", models=None, status="succeeded", usage=REPORTED_USAGE):
    runs = enqueue(state, owner, topic, client=client, models=models or ["test/one"])
    for _ in runs:
        claimed = state.claim()
        state.progress(claimed, "Answer", "gen-test", usage)
        state.finish(claimed, status, usage=usage)
    return runs


def test_profile_snapshot_message_metadata_and_organization_project_totals(installation):
    _, state, client, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    root = f"/api/organizations/{org}"
    assert client.put(root + "/impact-profiles", json={"profiles": [PROFILE]}).status_code == 200
    original = finish(state, owner, topics[1]["id"])[0]
    updated = {**PROFILE, "input_kwh_per_million": 20.0, "location": "Different scenario"}
    client.put(root + "/impact-profiles", json={"profiles": [updated]}).raise_for_status()
    run = state.run(user, org, original["id"])
    assert run["usage"]["assumptions"] == PROFILE
    assert run["usage"]["estimated_kwh"] == pytest.approx(0.0007)
    assert run["usage"]["estimated_kg_co2e"] == pytest.approx(0.00028)
    messages = client.get(root + f"/topics/{topics[1]['id']}/messages").json()
    assert "usage" not in messages[0]  # A human chat message is not another inference call.
    assert messages[-1]["usage"] == run["usage"]
    assert client.get(root + "/search", params={"q": "Answer"}).json()[0]["usage"] == run["usage"]
    # Failed/partial and unprofiled calls still contribute their reported tokens and cost.
    finish(state, owner, topics[1]["id"], client="two", models=["test/two"], status="failed")
    finish(state, owner, topics[1]["id"], client="three", status="cancelled", usage=None)
    total = client.get(root + "/usage").json()
    assert total["total"]["runs"] == 3
    assert total["total"]["prompt_tokens"] == 40 and total["total"]["completion_tokens"] == 10
    assert total["total"]["actual_microusd"] == 16 and total["total"]["cost_known_runs"] == 2
    assert total["total"]["energy_known_runs"] == 2 and total["total"]["carbon_known_runs"] == 1
    assert total["total"]["reference_energy_runs"] == 1
    assert total["total"]["estimated_kwh"] == pytest.approx(0.0007 + 25 * REFERENCE_KWH_PER_MILLION / 1_000_000)
    project = total["projects"][0]["project_id"]
    assert client.get(root + "/usage", params={"project_id": project}).json()["total"] == total["total"]
    assert client.get(root + "/usage", params={"project_id": "f" * 32}).status_code == 404


def test_unknown_zero_and_deleted_profiles_do_not_rewrite_history(installation):
    _, state, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}"
    empty = client.get(root + "/usage").json()["total"]
    assert empty["runs"] == 0 and empty["estimated_kwh"] is empty["estimated_kg_co2e"] is None
    finish(state, owner, topics[1]["id"])
    baseline = client.get(root + "/usage").json()["total"]
    assert baseline["estimated_kwh"] == pytest.approx(25 * REFERENCE_KWH_PER_MILLION / 1_000_000)
    assert baseline["reference_energy_runs"] == 1 and baseline["estimated_kg_co2e"] is None
    profile = {**PROFILE, "grid_grams_co2e_per_kwh": None}
    client.put(root + "/impact-profiles", json={"profiles": [profile]}).raise_for_status()
    original = finish(state, owner, topics[1]["id"], client="zero", usage=Usage(0, 0, 0))[0]
    client.put(root + "/impact-profiles", json={"profiles": []}).raise_for_status()
    value = state.run(owner["user_id"], owner["organization_id"], original["id"])["usage"]
    assert value["estimated_kwh"] == 0 and value["estimated_kg_co2e"] is None
    assert value["assumptions"] == profile
    total = client.get(root + "/usage").json()["total"]
    assert total["energy_known_runs"] == 2 and total["reference_energy_runs"] == 1 and total["carbon_known_runs"] == 0


def test_reference_energy_is_identical_for_messages_runs_and_totals_without_backfilling_evidence(installation):
    _, state, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}"
    original = finish(state, owner, topics[1]["id"])[0]
    value = state.run(owner["user_id"], owner["organization_id"], original["id"])["usage"]
    assert value["estimate_kind"] == "reference" and value["estimated_kg_co2e"] is None
    # One joule per processed token is 25 / 3600 Wh for this synthetic usage.
    assert value["estimated_kwh"] * 1000 == pytest.approx(25 / 3600)
    assert "illustrative application assumption" in value["assumptions"]["source"]
    with state.store.transaction() as db:
        assert db.execute("SELECT impact_profile_json FROM llm_runs WHERE id=?", (original["id"],)).fetchone()[0] is None
    totals = client.get(root + "/usage").json()["total"]
    assert totals["estimated_kwh"] == value["estimated_kwh"] and totals["reference_energy_runs"] == 1


def test_provider_call_reference_energy_matches_run_and_aggregate_usage(installation):
    from test_public_research_agent import ResearchFixture, turn

    state, owner, _, run = turn(installation, ResearchFixture())
    client = installation[2]
    root = f"/api/organizations/{owner['organization_id']}"
    calls = client.get(root + "/inference-ledger").json()["calls"]
    assert len(calls) == 2 and all(call["usage"]["estimate_kind"] == "reference" for call in calls)
    energy = sum(call["usage"]["estimated_kwh"] for call in calls)
    assert state.run(owner["user_id"], owner["organization_id"], run["id"])["usage"]["estimated_kwh"] == pytest.approx(energy)
    assert client.get(root + "/usage").json()["total"]["estimated_kwh"] == pytest.approx(energy)


def test_admission_snapshot_and_replay_keep_original_assumptions(installation):
    _, state, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}"
    client.put(root + "/impact-profiles", json={"profiles": [PROFILE]}).raise_for_status()
    original = enqueue(state, owner, topics[1]["id"], models=["test/one"])
    client.put(root + "/impact-profiles", json={"profiles": [{**PROFILE, "output_kwh_per_million": 200.0}]}).raise_for_status()
    assert enqueue(state, owner, topics[1]["id"], models=["test/one"]) == original
    row = state.claim()
    state.progress(row, "Answer", "gen-test", REPORTED_USAGE)
    state.finish(row, "succeeded", usage=REPORTED_USAGE)
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["usage"]["assumptions"] == PROFILE


def test_existing_schema_migration_preserves_usage_without_hypothetical_backfill(installation):
    _, state, _, owner, topics = prepare(installation)
    original = finish(state, owner, topics[1]["id"])[0]
    with state.store.transaction(write=True) as db:
        remove_collaboration_schema(db)
        for table in ("knowledge_writes", "knowledge_versions", "knowledge_entries", "user_presence", "project_memberships"):
            db.execute("DROP TABLE " + table)
        db.execute("DROP TABLE model_impact_profiles")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
        db.execute("PRAGMA user_version=13")
    upgraded = Store(state.store.data_dir)
    run = InferenceStore(upgraded).run(owner["user_id"], owner["organization_id"], original["id"])
    assert run["usage"]["prompt_tokens"] == 20 and run["usage"]["actual_microusd"] == 8
    assert run["usage"]["estimated_kwh"] == pytest.approx(25 * REFERENCE_KWH_PER_MILLION / 1_000_000)
    assert run["usage"]["estimate_kind"] == "reference" and run["usage"]["estimated_kg_co2e"] is None


def test_project_totals_do_not_mix_projects_or_double_count_messages(installation):
    _, state, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}"
    finish(state, owner, topics[1]["id"])
    project = client.post(root + "/projects", json={"name": "Separate project"}).json()
    channel = client.post(root + "/channels", json={"name": "research", "project_id": project["id"]}).json()
    topic = client.post(root + "/topics", json={"name": "Other beta", "kind": "beta", "channel_id": channel["id"]}).json()
    client.post(root + f"/topics/{topic['id']}/messages", json={"content": "Question", "client_id": "other-human"}).raise_for_status()
    finish(state, owner, topic["id"], client="other-project", usage=Usage(12, 30, 10))
    total = client.get(root + "/usage").json()
    assert total["total"]["runs"] == 2 and total["total"]["prompt_tokens"] == 50
    scoped = client.get(root + "/usage", params={"project_id": project["id"]}).json()
    assert scoped["total"]["runs"] == 1 and scoped["total"]["prompt_tokens"] == 30
    assert len(scoped["projects"]) == 1 and scoped["projects"][0]["project_name"] == "Separate project"


@pytest.mark.parametrize("change", [
    {"input_kwh_per_million": -1}, {"output_kwh_per_million": True}, {"grid_grams_co2e_per_kwh": 2001},
    {"model": ""}, {"source": ""}, {"location": ""}, {"unknown": "field"},
])
def test_invalid_profiles_are_rejected_without_writes(installation, change):
    _, _, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    response = client.put(root + "/impact-profiles", json={"profiles": [{**PROFILE, **change}]})
    assert response.status_code == 422
    assert client.get(root + "/impact-profiles").json() == {"profiles": []}


def test_duplicate_models_and_cross_organization_reads_are_refused(installation):
    _, state, client, owner, _ = prepare(installation)
    org = owner["organization_id"]
    root = f"/api/organizations/{org}"
    assert client.put(root + "/impact-profiles", json={"profiles": [PROFILE, PROFILE]}).status_code == 400
    other = state.store.create_organization(owner["user_id"], "Other")
    client.put(root + "/impact-profiles", json={"profiles": [PROFILE]}).raise_for_status()
    assert client.get(f"/api/organizations/{other['id']}/usage").json()["total"]["runs"] == 0
    _, _, member = invited(installation)
    with TestClient(create_app(installation[0])) as outsider:
        headers = {"Authorization": "Bearer " + member["access_token"]}
        for suffix in ("/usage", "/impact-profiles", "/model-prices"):
            assert outsider.get(f"/api/organizations/{other['id']}" + suffix, headers=headers).status_code == 404
        assert outsider.get(root + "/impact-profiles", headers=headers).status_code == 200
        assert outsider.put(root + "/impact-profiles", headers=headers, json={"profiles": []}).status_code == 403


def test_catalog_needs_no_key_no_inference_and_is_available_to_members(installation):
    config, _, client, owner = installation
    org = owner["organization_id"]
    _, _, member = invited(installation)
    fixture = Fixture()
    with TestClient(create_app(config, provider=fixture.provider)) as browser:
        response = browser.get(f"/api/organizations/{org}/model-prices", headers={"Authorization": "Bearer " + member["access_token"]})
        assert response.status_code == 200
        assert response.json()["models"][0]["input_usd_per_million"] == 1
        assert response.json()["models"][0]["output_usd_per_million"] == 2
        assert response.json()["models"][0]["provider"] == "test"
        assert response.json()["models"][0]["supported_parameters"] == ["max_completion_tokens"]
        assert response.json()["routing"] == "recommended"
    assert all(request.method == "GET" and request.url.path.endswith("/models") and "Authorization" not in request.headers
               for request in fixture.requests)
    assert asyncio.run(fixture.provider.prices())


def test_new_key_does_not_snapshot_or_restrict_the_text_model_catalog(installation):
    config, _, _, owner = installation
    fixture = Fixture()
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    root = f"/api/organizations/{owner['organization_id']}"
    with TestClient(create_app(config, provider=fixture.provider)) as client:
        response = client.post(root + "/provider-keys", headers=headers, json={
            "label": "Primary", "key": "sk-or-new-organization-key", "allow_all_text_models": True,
        })
        assert response.status_code == 201, response.text
        assert client.get(root + "/policy", headers=headers).json()["allowed_models"] == []
    assert not fixture.requests


def test_key_setup_and_new_catalogue_models_need_no_model_policy(installation):
    config, _, _, owner = installation
    fixture = Fixture()
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    root = f"/api/organizations/{owner['organization_id']}"
    prices = fixture.provider.prices
    future = "new-provider/new-model"

    async def expanded_prices(**options):
        models = await prices(**options)
        return [*models, {**models[0], "id": future}]

    with TestClient(create_app(config, provider=fixture.provider)) as client:
        created = client.post(root + "/provider-keys", headers=headers, json={
            "label": "All available routers", "key": "sk-or-catalogue-fixture-key",
        })
        assert created.status_code == 201, created.text
        assert not fixture.requests
        assert client.put(root + "/policy", headers=headers, json={"monthly_budget_microusd": 1_000_000}).status_code == 200
        initial = client.get(root + "/inference-configuration", headers=headers).json()
        assert initial["has_organization_key"] and future not in initial["allowed_models"]
        fixture.provider.prices = expanded_prices
        current = client.get(root + "/inference-configuration", headers=headers).json()
        assert future in current["allowed_models"]
        assert future in client.get(root + "/model-prices", headers=headers).json()["allowed_models"]
        assert client.get(root + "/policy", headers=headers).json()["allowed_models"] == []
        fresh = client.post("/api/organizations", headers=headers, json={
            "name": "Fresh catalogue access", "credential_mode": "new", "key_label": "All routers",
            "key": "sk-or-fresh-catalogue-key", "monthly_budget_microusd": 1_000_000,
        })
        assert fresh.status_code == 201, fresh.text


def test_capability_catalog_keeps_text_models_with_unreported_prices():
    def catalog(_request):
        return httpx.Response(200, json={"data": [{
            "id": "test/unpriced", "name": "Unpriced", "description": "Capability metadata remains useful.",
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "reasoning", "max_completion_tokens"],
            "pricing": {"prompt": "-1", "completion": None}, "context_length": 100000,
            "top_provider": {"max_completion_tokens": 4096},
        }]})

    models = asyncio.run(OpenRouter(transport=httpx.MockTransport(catalog)).prices())
    assert models[0]["input_usd_per_million"] is models[0]["output_usd_per_million"] is None
    assert models[0]["supports_tools"] is models[0]["supports_reasoning"] is True
    assert models[0]["input_modalities"] == ["image", "text"]
