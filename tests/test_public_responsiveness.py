"""Concurrent browsing shares public metadata and keeps DB work off the event loop."""

import asyncio
import copy
import threading
from contextlib import contextmanager

import httpx
import pytest
from test_public_server import installation as installation
from test_public_server import structure

from reasoning_ledger.server.api import create_app
from reasoning_ledger.server.openrouter import CATALOG_RETRY, CATALOG_TTL, OpenRouter, ProviderError

MODEL = {"id": "fixture/model", "context_length": 8192,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
         "supported_parameters": ["max_completion_tokens"],
         "pricing": {"prompt": "0.000001", "completion": "0.000002"}}


def test_concurrent_catalog_reads_share_refresh_and_do_not_mutate_cached_data():
    async def scenario():
        requests, clock = [], [100.0]

        async def catalog(request):
            requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"data": [MODEL]})

        provider = OpenRouter(transport=httpx.MockTransport(catalog), clock=lambda: clock[0])
        catalogs = await asyncio.gather(*(provider.prices(include_routing=True) for _ in range(30)))
        assert len(requests) == 4  # One public catalog, plus three optional routing queries.
        assert all("Authorization" not in request.headers for request in requests)
        catalogs[0][0]["name"] = "Changed by a caller"
        catalogs[0][0]["input_modalities"].clear()
        assert catalogs[1][0]["name"] == MODEL["id"]
        assert (await provider.prices())[0]["input_modalities"] == ["text"]
        assert "routing_availability" not in (await provider.prices())[0]
        clock[0] += CATALOG_TTL + 1
        await asyncio.gather(*(provider.prices() for _ in range(30)))
        assert len(requests) == 5
        # Optional routing metadata retains its independent five-minute lifetime.
        await provider.prices(include_routing=True)
        assert len(requests) == 5

    asyncio.run(scenario())


def test_catalog_failure_backoff_expires_and_admission_quotes_always_fetch_current_prices():
    async def scenario():
        requests, clock, failing, rate = [], [100.0], [False], ["0.000001"]

        def catalog(request):
            requests.append(request)
            if failing[0]:
                return httpx.Response(503, text="untrusted provider error")
            model = copy.deepcopy(MODEL)
            model["pricing"]["prompt"] = rate[0]
            return httpx.Response(200, json={"data": [model]})

        provider = OpenRouter(transport=httpx.MockTransport(catalog), clock=lambda: clock[0])
        await provider.prices()
        assert len(requests) == 1
        rate[0] = "0.000009"
        assert (await provider.quotes([MODEL["id"]]))[0].prompt == rate[0]
        assert (await provider.prices())[0]["input_usd_per_million"] == 1
        await provider.quotes([MODEL["id"]])
        assert len(requests) == 3
        failing[0] = True
        clock[0] += CATALOG_TTL + 1
        errors = await asyncio.gather(*(provider.prices() for _ in range(30)), return_exceptions=True)
        assert all(isinstance(error, ProviderError) and str(error) == "catalog_unavailable" for error in errors)
        assert len(requests) == 4
        failing[0] = False
        clock[0] += CATALOG_RETRY + 1
        assert (await provider.prices())[0]["id"] == MODEL["id"]
        assert len(requests) == 5

    asyncio.run(scenario())


def test_cancelled_catalog_refresh_releases_followers():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def catalog(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"data": [MODEL]})

        provider = OpenRouter(transport=httpx.MockTransport(catalog))
        first = asyncio.create_task(provider.prices())
        await started.wait()
        follower = asyncio.create_task(provider.prices())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await asyncio.wait_for(follower, 2))[0]["id"] == MODEL["id"]

    asyncio.run(scenario())


def test_concurrent_browsing_offloads_database_and_rechecks_authority_on_cached_reads(installation, monkeypatch):
    config, _, browser, owner = installation
    _, _, topics = structure(browser, owner["organization_id"])
    requests = []

    async def catalog(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"data": [MODEL]})

    app = create_app(config, provider=OpenRouter(transport=httpx.MockTransport(catalog)))
    store = app.state.store
    transaction = store.transaction
    loop_thread = None

    @contextmanager
    def offloaded_transaction(**kwargs):
        assert threading.get_ident() != loop_thread, "database work must not block unrelated requests on the event loop"
        with transaction(**kwargs) as db:
            yield db

    monkeypatch.setattr(store, "transaction", offloaded_transaction)

    async def scenario():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        root = "/api/organizations/" + owner["organization_id"]
        headers = {"Authorization": "Bearer " + owner["access_token"]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=config.public_url, headers=headers) as client:
            paths = [root + suffix for suffix in (
                "/inference-configuration", "/model-prices", "/model-benchmarks", "/my-bots",
                "/topics/" + topics[0]["id"] + "/messages",
            )] + ["/api/health"]
            results = await asyncio.gather(*(client.get(path) for path in paths for _ in range(5)))
            assert all(result.status_code == 200 for result in results)
            assert len(requests) == 4
            # Cached public metadata must never cache membership or session authority.
            await asyncio.to_thread(store.create_organization, owner["user_id"], "Other organization")
            with transaction(write=True) as db:
                db.execute("DELETE FROM memberships WHERE organization_id=? AND user_id=?",
                           (owner["organization_id"], owner["user_id"]))
            for suffix in ("/inference-configuration", "/model-prices", "/model-benchmarks"):
                assert (await client.get(root + suffix)).status_code == 404
            assert len(requests) == 4

    asyncio.run(scenario())
