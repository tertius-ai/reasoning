"""Offline concurrent browsing with real SQLite and simulated catalog/DB latency."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
from pathlib import Path

import httpx

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.openrouter import OpenRouter


async def browse(clients: int, configuration_delay_ms: float) -> dict:
    catalog_requests = 0

    async def catalog(request):
        nonlocal catalog_requests
        assert request.method == "GET" and request.url.path.endswith("/models")
        catalog_requests += 1
        await asyncio.sleep(0.025)
        return httpx.Response(200, json={"data": [{
            "id": "fixture/model", "context_length": 8192,
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }]})

    provider = OpenRouter(transport=httpx.MockTransport(catalog))
    with tempfile.TemporaryDirectory(prefix="reasoning-browsing-") as directory:
        config = Config(Path(directory) / "state", "http://localhost", inference_enabled=False)
        app = create_app(config, provider=provider)
        store = app.state.store
        owner = store.bootstrap(store._setup_token, "owner@example.test", "Owner", "fixture-only-long-password", "Fixture")
        user, org = owner["user_id"], owner["organization_id"]
        project = store.create_entity(user, org, "projects", "Project")
        channel = store.create_entity(user, org, "channels", "Channel", project["id"])
        topic = store.create_entity(user, org, "topics", "Conversation", channel["id"], "beta")
        configuration = app.state.inference.configuration

        def delayed_configuration(*args, **kwargs):
            # Controlled contention, not a measurement of the production DB.
            time.sleep(configuration_delay_ms / 1000)
            return configuration(*args, **kwargs)

        app.state.inference.configuration = delayed_configuration
        root = "/api/organizations/" + org
        latencies = {category: [] for category in ("health", "conversation", "bots", "models")}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=config.public_url,
                                     headers={"Authorization": "Bearer " + owner["access_token"]}) as client:
            async def get(path, category):
                started = time.perf_counter()
                response = await client.get(path)
                response.raise_for_status()
                latencies[category].append(time.perf_counter() - started)

            started = time.perf_counter()
            await asyncio.gather(*(get(path, category) for _ in range(clients) for path, category in (
                (root + "/inference-configuration", "models"),
                (root + "/topics/" + topic["id"] + "/messages", "conversation"),
                (root + "/my-bots", "bots"), ("/api/health", "health"),
            )))
            return {
                "clients": clients, "requests": clients * 4,
                "simulated_configuration_delay_ms": configuration_delay_ms,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "upstream_catalog_fetches": catalog_requests,
                "p95_seconds": {category: round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3)
                                for category, values in latencies.items()},
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=30)
    parser.add_argument("--configuration-delay-ms", type=float, default=25)
    args = parser.parse_args()
    if not 1 <= args.clients <= 100 or not 0 <= args.configuration_delay_ms <= 1000:
        parser.error("choose 1–100 clients and 0–1000 ms simulated configuration latency")
    print(json.dumps(asyncio.run(browse(args.clients, args.configuration_delay_ms)), sort_keys=True))


if __name__ == "__main__":
    main()
