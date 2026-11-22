"""Actual API/SQLite contracts used by shared compute controls, without CAS execution.

Inline dispatch is test-local: this sandbox denies cross-thread ASGI wakeups.
These checks do not verify production scheduling, TCP/TLS, or Docker isolation.
"""

import asyncio
import base64
import hashlib

import fastapi.dependencies.utils
import fastapi.routing
import httpx
import pytest
import starlette.concurrency
import starlette.responses

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, canonical
from reasoning_ledger.server.security import private_file
from reasoning_ledger.server.store import Store

IMAGE = {
    "id": "protocol-fixture",
    "name": "Non-executing UI protocol fixture",
    "reference": "registry.example.org/research/cpu@sha256:" + "a" * 64,
    "languages": ["sage", "python"],
    "source_revision": "b" * 40,
    "capabilities_digest": "sha256:" + "c" * 64,
    "sbom_digest": "sha256:" + "d" * 64,
    "capabilities_path": "/etc/reasoning/capabilities.json",
    "sbom_path": "/etc/reasoning/sbom.json",
    "maximum_resources": dict(DEFAULT_RESOURCES),
}
ORIGIN = "https://chat.example.org"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def inline_iterator(items):
        for item in items:
            yield item

    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", inline)
    monkeypatch.setattr(starlette.responses, "iterate_in_threadpool", inline_iterator)
    store = Store(tmp_path / "state")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "long fixture password", "Organization")
    org, user = owner["organization_id"], owner["user_id"]
    project = store.create_entity(user, org, "projects", "Research")
    channel = store.create_entity(user, org, "channels", "algebra", project["id"])
    topics = [store.create_entity(user, org, "topics", kind, channel["id"], kind)["id"] for kind in ("alpha", "beta", "gamma")]
    catalog = tmp_path / "images.json"
    private_file(catalog, canonical([IMAGE]).encode())
    app = create_app(Config(store.data_dir, ORIGIN, inference_enabled=False, compute_images=catalog))
    app.state.compute.set_policy(user, org, [IMAGE["id"]], 10000, 2)
    return store, app, owner, topics, "/api/organizations/" + org


def body(client_id="unchanged"):
    return {
        "client_id": client_id,
        "image_id": IMAGE["id"],
        "language": "sage",
        "program": "# Non-executing protocol fixture",
        "resources": dict(DEFAULT_RESOURCES),
    }


def headers(owner):
    return {"authorization": "Bearer " + owner["access_token"]}


def capacity(fixture):
    """An enabled scaler grant below its ceiling: admission queues jobs before a test registers the worker that claims
    them, and the grant itself claims nothing."""
    _, app, owner, _, _ = fixture
    app.state.compute_scalers.create(
        owner["user_id"], owner["organization_id"], "Fixture capacity", [IMAGE["id"]], IMAGE["maximum_resources"], 64
    )


def test_shared_submission_receipt_history_cursor_logs_and_artifact_download(fixture):
    capacity(fixture)
    _, app, owner, topics, root = fixture

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(owner)) as client:
            config = (await client.get(root + "/compute-configuration")).json()
            assert config["images"] == [IMAGE] and config["balance"]["available_cpu_seconds"] == 10000
            path = root + "/topics/" + topics[2] + "/compute-jobs"
            sent = await client.post(path, json=body())
            assert sent.status_code == 202
            job = sent.json()
            assert job["client_id"] == "unchanged" and job["parameters"] == {
                key: value for key, value in body().items() if key != "client_id"
            }
            assert job["image"] == IMAGE and job["requested_by"] == owner["user_id"]
            assert not {"lease", "lease_hash", "attempt_id", "token"} & job.keys()
            assert (await client.post(path, json=body())).json()["id"] == job["id"]
            assert len((await client.get(path + "?limit=50")).json()) == 1
            worker = await client.post(
                root + "/compute-workers", json={"name": "Separate fixture", "image_ids": [IMAGE["id"]], "resources": DEFAULT_RESOURCES}
            )
            assert worker.status_code == 201
            registration = worker.json()
            assert "token" not in (await client.get(root + "/compute-workers")).json()[0]
            credential = {"authorization": "Bearer " + registration["token"]}
            identity = await client.get("/api/worker/identity", headers=credential)
            assert identity.status_code == 200 and identity.json()["id"] == registration["id"]
            assert "token" not in identity.json() and "token_hash" not in identity.json()
            assert (await client.get(root + "/compute-jobs/" + job["id"])).json()["status"] == "queued"
            claimed = (await client.post("/api/worker/claim", headers=credential, json={})).json()
            lease = {key: claimed[key] for key in ("attempt_id", "lease")}
            worker_path = "/api/worker/jobs/" + job["id"]
            output = "Protocol output, not an algebra result. <script>plain text</script>\n"
            assert (
                await client.post(worker_path + "/logs", headers=credential, json={**lease, "chunk_index": 0, "text": output})
            ).status_code == 204
            payload = b"untrusted protocol artifact"
            artifact = (
                await client.post(
                    worker_path + "/artifacts",
                    headers=credential,
                    json={
                        **lease,
                        "name": "fixture.txt",
                        "size_bytes": len(payload),
                        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    },
                )
            ).json()
            artifact_path = worker_path + "/artifacts/" + artifact["id"]
            assert (
                await client.put(
                    artifact_path + "/chunks",
                    headers=credential,
                    json={**lease, "chunk_index": 0, "content_base64": base64.b64encode(payload).decode()},
                )
            ).status_code == 204
            assert (await client.get(root + "/compute-jobs/" + job["id"] + "/artifacts")).json() == []
            assert (await client.post(artifact_path + "/commit", headers=credential, json=lease)).status_code == 200
            assert (
                await client.post(
                    worker_path + "/finish", headers=credential, json={**lease, "status": "succeeded", "elapsed_ms": 1000, "exit_code": 0}
                )
            ).status_code == 200
            job_path = root + "/compute-jobs/" + job["id"]
            events = (await client.get(job_path + "/events?after=0")).json()
            assert [item["event"]["type"] for item in events] == ["state", "state", "log", "artifact", "state"]
            assert next(item["event"]["text"] for item in events if item["event"]["type"] == "log") == output
            assert (await client.get(job_path + "/events?after=" + str(events[-1]["sequence"]))).json() == []
            assert (await client.get(job_path)).json()["used_cpu_seconds"] == 1
            downloaded = await client.get(job_path + "/artifacts/" + artifact["id"])
            assert downloaded.content == payload and downloaded.headers["content-type"] == "application/octet-stream"
            assert downloaded.headers["content-disposition"] == 'attachment; filename="fixture.txt"'
            assert downloaded.headers["x-content-type-options"] == "nosniff" and downloaded.headers["cache-control"] == "no-store"

    asyncio.run(check())


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_member_and_viewer_visibility_do_not_grant_compute_administration(fixture, role):
    capacity(fixture)
    store, app, owner, topics, root = fixture
    invite = store.invite(owner["user_id"], owner["organization_id"], role + "@example.org", role, 3600)
    user = store.accept_invitation(invite["token"], user_id=None, name=role, password="long fixture password")

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(user)) as client:
            assert (await client.get(root + "/compute-configuration")).status_code == 200
            assert (await client.get(root + "/compute-workers")).status_code == 403
            assert (
                await client.put(
                    root + "/compute-policy", json={"allowed_images": [IMAGE["id"]], "monthly_cpu_seconds": 1, "max_active_jobs": 1}
                )
            ).status_code == 403
            assert (await client.post(root + "/topics/" + topics[2] + "/compute-jobs", json=body())).status_code == (
                403 if role == "viewer" else 202
            )
            for topic in topics[:1]:
                assert (await client.post(root + "/topics/" + topic + "/compute-jobs", json=body(topic))).status_code in {400, 403}
            assert (await client.post(root + "/topics/" + topics[1] + "/compute-jobs", json=body("beta"))).status_code == (
                403 if role == "viewer" else 202
            )

    asyncio.run(check())


def test_cookie_submission_requires_same_origin_csrf_and_strict_resource_fields(fixture):
    capacity(fixture)
    _, app, owner, topics, root = fixture

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            path = root + "/topics/" + topics[2] + "/compute-jobs"
            assert (await client.post(path, json=body())).status_code == 403
            csrf = {"origin": ORIGIN, "x-csrf-token": owner["csrf_token"]}
            assert (await client.post(path, headers=csrf, json=body())).status_code == 202
            for value in (True, "1", 1.5):
                invalid = {**body("invalid"), "resources": {**DEFAULT_RESOURCES, "cpu": value}}
                assert (await client.post(path, headers=csrf, json=invalid)).status_code == 422
            assert (await client.post(path, headers={**csrf, "origin": "https://other.example"}, json=body())).status_code == 403

    asyncio.run(check())


def test_uncertain_cancellation_holds_quota_and_requires_explicit_cleanup_reconciliation(fixture):
    capacity(fixture)
    store, app, owner, topics, root = fixture
    compute = app.state.compute
    job = compute.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[2],
        "uncertain",
        compute.parameters(IMAGE["id"], "sage", "# fixture", DEFAULT_RESOURCES),
    )
    worker = compute.register_worker(owner["user_id"], owner["organization_id"], "Fixture", [IMAGE["id"]], DEFAULT_RESOURCES)
    compute.claim(worker["token"])
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (job["id"],))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(owner)) as client:
            path = root + "/compute-jobs/" + job["id"]
            assert (await client.get(path)).json()["status"] == "outcome_unknown"
            cancelled = (await client.post(path + "/cancel", json={})).json()
            assert cancelled["status"] == "outcome_unknown" and cancelled["cancel_requested"] == 1
            balance = (await client.get(root + "/compute-configuration")).json()["balance"]
            assert balance["held_cpu_seconds"] == 120
            assert (await client.put(path + "/reconciliation", json={"elapsed_ms": 1234, "cleanup_confirmed": False})).status_code == 400
            confirmed = await client.put(path + "/reconciliation", json={"elapsed_ms": 1234, "cleanup_confirmed": True})
            assert confirmed.status_code == 200 and confirmed.json()["status"] == "cancelled"
            assert (await client.get(root + "/compute-configuration")).json()["balance"]["held_cpu_seconds"] == 0
            assert (await client.patch(root + "/compute-workers/" + worker["id"], json={"draining": True})).status_code == 204
            assert (await client.get(root + "/compute-workers")).json()[0]["draining"] == 1
            assert (await client.delete(root + "/compute-workers/" + worker["id"])).status_code == 204
            assert (
                await client.post("/api/worker/claim", headers={"authorization": "Bearer " + worker["token"]}, json={})
            ).status_code == 401

    asyncio.run(check())


def test_job_and_artifact_routes_refuse_cross_organization_ids(fixture):
    capacity(fixture)
    store, app, owner, topics, root = fixture
    compute = app.state.compute
    job = compute.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[2],
        "tenant",
        compute.parameters(IMAGE["id"], "sage", "# fixture", DEFAULT_RESOURCES),
    )
    other = store.create_organization(owner["user_id"], "Other")["id"]

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(owner)) as client:
            path = "/api/organizations/" + other + "/compute-jobs/" + job["id"]
            for suffix in ("", "/events", "/artifacts", "/artifacts/" + "f" * 32):
                assert (await client.get(path + suffix)).status_code == 404
            assert (await client.post(path + "/cancel", json={})).status_code == 404
            assert (await client.get(root + "/compute-jobs/" + job["id"])).status_code == 200

    asyncio.run(check())


def test_event_backlog_pages_use_exclusive_cursor_without_lost_logs(fixture):
    capacity(fixture)
    _, app, owner, topics, root = fixture
    compute = app.state.compute
    job = compute.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[2],
        "backlog",
        compute.parameters(IMAGE["id"], "sage", "# fixture", DEFAULT_RESOURCES),
    )
    worker = compute.register_worker(owner["user_id"], owner["organization_id"], "Fixture", [IMAGE["id"]], DEFAULT_RESOURCES)
    claim = compute.claim(worker["token"])
    for index in range(205):
        compute.append_log(worker["token"], job["id"], claim["attempt_id"], claim["lease"], index, f"fixture {index}\n")

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(owner)) as client:
            path = root + "/compute-jobs/" + job["id"] + "/events"
            first = (await client.get(path)).json()
            second = (await client.get(path + "?after=" + str(first[-1]["sequence"]))).json()
            assert len(first) == 200 and len(second) == 7
            events = first + second
            assert len({item["sequence"] for item in events}) == 207
            assert [item["event"]["text"] for item in events if item["event"]["type"] == "log"] == [
                f"fixture {index}\n" for index in range(205)
            ]

    asyncio.run(check())


def test_worker_identity_requires_worker_bearer_and_never_claims_or_updates_activity(fixture):
    capacity(fixture)
    _, app, owner, topics, root = fixture
    compute = app.state.compute
    org, user = owner["organization_id"], owner["user_id"]
    job = compute.enqueue(user, org, topics[2], "identity-only", compute.parameters(IMAGE["id"], "sage", "# fixture", DEFAULT_RESOURCES))
    worker = compute.register_worker(user, org, "Identity fixture", [IMAGE["id"]], DEFAULT_RESOURCES)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            assert (await client.get("/api/worker/identity", headers=headers(owner))).status_code == 401
            client.cookies.set("__Host-reasoning_session", worker["token"])
            assert (await client.get("/api/worker/identity")).status_code == 401
            client.cookies.clear()
            credential = {"authorization": "Bearer " + worker["token"]}
            result = await client.get("/api/worker/identity", headers=credential)
            assert result.status_code == 200 and result.json()["organization_id"] == org
            assert result.json()["last_seen_at"] is None and not {"token", "token_hash", "lease", "lease_hash"} & result.json().keys()
            assert result.headers["cache-control"] == "no-store"
            assert (
                await client.get("/api/worker/identity", headers={**credential, "origin": "https://attacker.example"})
            ).status_code == 403
            assert (await client.get(root + "/compute-configuration", headers=credential)).status_code == 401
            assert compute.job(user, org, job["id"])["status"] == "queued"
            assert compute.workers(user, org)[0]["last_seen_at"] is None
            compute.change_worker(user, org, worker["id"], revoke=True)
            assert (await client.get("/api/worker/identity", headers=credential)).status_code == 401

    asyncio.run(check())
