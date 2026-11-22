"""Public compute protocol checks; fixtures do not execute or simulate algebra."""

import base64
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.api import create_app
from reasoning_ledger.server.compute_catalog import (
    DEFAULT_RESOURCES,
    ComputeImage,
    canonical,
    capabilities_digest,
    load_capabilities,
    load_catalog,
    resources,
)
from reasoning_ledger.server.compute_store import CHUNK_BYTES, ComputeStore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.security import private_file
from reasoning_ledger.server.store import Store, StoreError

IMAGE = {
    "id": "fixture-cas",
    "name": "Non-executing protocol fixture",
    "reference": "registry.example.org/research/cpu@sha256:" + "a" * 64,
    "languages": ["sage", "python", "gap"],
    "source_revision": "b" * 40,
    "capabilities_digest": "sha256:" + "c" * 64,
    "sbom_digest": "sha256:" + "d" * 64,
    "capabilities_path": "/etc/reasoning/capabilities.json",
    "sbom_path": "/etc/reasoning/sbom.json",
    "maximum_resources": {**DEFAULT_RESOURCES, "cpu": 4},
}


@pytest.fixture
def compute(installation, tmp_path):
    config, store, old_client, owner = installation
    catalog = tmp_path / "images.json"
    private_file(catalog, canonical([IMAGE]).encode())
    config = replace(config, compute_images=catalog)
    app = create_app(config)
    client = TestClient(app, base_url=config.public_url, headers=dict(old_client.headers))
    topics = structure(client, owner["organization_id"])[2]
    root = f"/api/organizations/{owner['organization_id']}"
    response = client.put(
        root + "/compute-policy", json={"allowed_images": [IMAGE["id"]], "monthly_cpu_seconds": 10000, "max_active_jobs": 2}
    )
    assert response.status_code == 200, response.text
    capacity(app, owner)
    return config, store, app.state.compute, client, owner, topics


def capacity(app, owner, image=IMAGE):
    """An enabled scaler grant below its ceiling: admission queues jobs before a test registers the worker that claims
    them, and the grant itself claims nothing."""
    return app.state.compute_scalers.create(
        owner["user_id"], owner["organization_id"], "Fixture capacity", [image["id"]], image["maximum_resources"], 64
    )


def without_capacity(compute):
    """Revoke the fixture's scaler grant, so only workers and grants a test adds can run its jobs."""
    _, _, _, client, owner, _ = compute
    scalers = client.app.state.compute_scalers
    for grant in scalers.list(owner["user_id"], owner["organization_id"]):
        scalers.revoke(owner["user_id"], owner["organization_id"], grant["id"])


def submission(client_id="job", **changes):
    return {
        "client_id": client_id,
        "image_id": IMAGE["id"],
        "language": "sage",
        "program": "print(factor(123456789))",
        "resources": dict(DEFAULT_RESOURCES),
        **changes,
    }


def enqueue(compute, client_id="job", **changes):
    _, _, _, client, owner, topics = compute
    response = client.post(
        f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs", json=submission(client_id, **changes)
    )
    assert response.status_code == 202, response.text
    return response.json()


def worker(compute, **changes):
    config, _, _, client, owner, _ = compute
    response = client.post(
        f"/api/organizations/{owner['organization_id']}/compute-workers",
        json={"name": "Fixture worker", "image_ids": [IMAGE["id"]], "resources": IMAGE["maximum_resources"], **changes},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    return record, TestClient(client.app, base_url=config.public_url, headers={"Authorization": "Bearer " + record["token"]})


def lease_body(claimed):
    return {"attempt_id": claimed["attempt_id"], "lease": claimed["lease"]}


def finish(client, claimed, **changes):
    return client.post(
        f"/api/worker/jobs/{claimed['id']}/finish",
        json={**lease_body(claimed), "status": "succeeded", "elapsed_ms": 6001, "exit_code": 0, **changes},
    )


def job_path(owner, job):
    return f"/api/organizations/{owner['organization_id']}/compute-jobs/{job['id']}"


def test_default_compute_is_disabled_and_excludes_alpha(installation, compute):
    _, _, original, owner = installation
    org = owner["organization_id"]
    assert original.get(f"/api/organizations/{org}/compute-configuration").json()["images"] == []
    _, _, _, client, _, topics = compute
    for topic in topics[:1]:
        assert client.post(f"/api/organizations/{org}/topics/{topic['id']}/compute-jobs", json=submission()).status_code == 400
    assert client.post(f"/api/organizations/{org}/topics/{topics[1]['id']}/compute-jobs", json=submission()).status_code == 202
    client.put(f"/api/organizations/{org}/compute-policy", json={"allowed_images": [], "monthly_cpu_seconds": 0})
    assert client.post(f"/api/organizations/{org}/topics/{topics[2]['id']}/compute-jobs", json=submission()).status_code == 409


def test_compute_activity_cursor_pages_equal_timestamp_jobs_without_duplicates(compute):
    config, store, state, client, owner, topics = compute
    created = []
    for index in range(6):
        job = enqueue(compute, f"activity-{index}")
        created.append(job["id"])
        state.cancel(owner["user_id"], owner["organization_id"], job["id"])
    root = f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs"
    newest = client.get(root, params={"standalone": True, "limit": 3}).json()
    older = client.get(root, params={"standalone": True, "limit": 3, "before": newest[-1]["sequence"]}).json()
    assert [item["id"] for item in newest + older] == list(reversed(created))
    assert len({item["sequence"] for item in newest + older}) == 6
    assert client.get(root, params={"before": 0}).status_code == 422
    _, outsider, _ = invited((config, store, client, owner), email="activity-outsider@example.org")
    private = store.create_entity(owner["user_id"], owner["organization_id"], "projects", "Private activity",
                                        visibility="private", owner_id=owner["user_id"])
    channel = store.create_entity(owner["user_id"], owner["organization_id"], "channels", "Private", private["id"])
    topic = store.create_entity(owner["user_id"], owner["organization_id"], "topics", "Private", channel["id"], "gamma")
    response = outsider.get(root.replace(topics[2]["id"], topic["id"]), params={"standalone": True, "before": newest[0]["sequence"]})
    assert response.status_code == 404


def test_replay_is_immutable_durable_and_precedes_changed_image_policy(compute):
    config, _, _, client, owner, topics = compute
    first = enqueue(compute)
    assert enqueue(compute) == first
    path = f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs"
    assert client.post(path, json=submission(program="different source")).status_code == 409
    assert client.post(path.replace(topics[2]["id"], topics[0]["id"]), json=submission()).status_code == 409
    client.put(f"/api/organizations/{owner['organization_id']}/compute-policy", json={"allowed_images": [], "monthly_cpu_seconds": 0})
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(client.headers))
    assert restarted.post(path, json=submission()).json() == first
    assert restarted.get(path).json() == [first]
    assert first["program_digest"] == "sha256:" + hashlib.sha256(first["parameters"]["program"].encode()).hexdigest()
    assert first["image"] == IMAGE


def test_admission_refuses_a_job_that_no_worker_or_scaler_would_claim(compute):
    _, store, state, client, owner, topics = compute
    org, user = owner["organization_id"], owner["user_id"]
    path = f"/api/organizations/{org}/topics/{topics[2]['id']}/compute-jobs"
    created = client.post(f"/api/organizations/{org}/projects", json={"name": "Project computations"})
    project = f"/api/organizations/{org}/projects/{created.json()['id']}/compute-jobs"
    without_capacity(compute)
    refused = client.post(path, json=submission("nothing-runs"))
    assert refused.status_code == 409
    assert "no compute worker or scaler in this organization runs image fixture-cas" in refused.json()["detail"]
    # Modal setup's shape: one scaler grant, and relays enrolled through it at its limits until its ceiling is full.
    relay = {**DEFAULT_RESOURCES, "memory_bytes": 512 * 1024**2}
    scalers = client.app.state.compute_scalers
    grant = scalers.create(user, org, "Modal relays", [IMAGE["id"]], relay, 2)
    relays = [
        scalers.enroll(grant["token"], f"{index:032x}", f"relay-{index}", [IMAGE["id"]], relay,
                       "rnw_" + "r" * 42 + str(index))["worker"]
        for index in range(2)
    ]
    with store.transaction() as db:
        # The grant's ceiling is full, so it offers nothing beyond its relays, and it could never offer more than them.
        assert state.capacity(db, org) == [{"image_ids": [IMAGE["id"]], "resources": relay}]
    for endpoint in (path, project):
        # Within the image's limits (1 GiB), but no relay takes more than 512 MiB.
        large = client.post(endpoint, json=submission("larger-than-relays"))
        assert large.status_code == 409
        assert "no compute worker or scaler accepts a job this large for image fixture-cas" in large.json()["detail"]
        assert "memory_bytes 536870912" in large.json()["detail"]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 0
    queued = enqueue(compute, "fits", resources=relay)
    # Draining both relays leaves nothing to claim a new job; replaying an accepted request still returns its job.
    for record in relays:
        state.change_worker(user, org, record["id"], draining=True)
    assert enqueue(compute, "fits", resources=relay) == queued
    assert client.post(path, json=submission("while-drained", resources=relay)).status_code == 409
    state.change_worker(user, org, relays[0]["id"], draining=False)
    assert state.claim("rnw_" + "r" * 42 + "0")["id"] == queued["id"]


def test_concurrent_admission_reserves_quota_atomically(compute):
    _, _, state, _, owner, topics = compute
    state.set_policy(owner["user_id"], owner["organization_id"], [IMAGE["id"]], 120, 2)
    body = submission()
    parameters = state.parameters(body["image_id"], body["language"], body["program"], body["resources"])

    def submit(index):
        try:
            state.enqueue(owner["user_id"], owner["organization_id"], topics[2]["id"], str(index), parameters)
            return 202
        except StoreError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == [202, 409]
    balance = state.configuration(owner["user_id"], owner["organization_id"])["balance"]
    assert balance["held_cpu_seconds"] == 120 and balance["available_cpu_seconds"] == 0


def test_concurrent_claims_do_not_duplicate_and_worker_has_one_slot(compute):
    _, _, state, _, owner, _ = compute
    enqueue(compute)
    record, _ = worker(compute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(state.claim, [record["token"], record["token"]]))
    assert sum(claim is not None for claim in claims) == 1
    enqueue(compute, "another")
    assert state.claim(record["token"]) is None
    second, _ = worker(compute, name="Second")
    assert state.claim(second["token"])["client_id"] == "another"
    third, _ = worker(compute, name="Third")
    enqueue(compute, "third")
    assert state.claim(third["token"]) is None
    assert len(state.jobs(owner["user_id"], owner["organization_id"], compute[5][2]["id"])) == 3


def test_worker_finish_events_accounting_and_replay_survive_restart(compute):
    config, store, _, client, owner, _ = compute
    job = enqueue(compute, resources={**DEFAULT_RESOURCES, "cpu": 2})
    registration, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    assert claimed["id"] == job["id"] and claimed["lease"]
    path = f"/api/worker/jobs/{job['id']}"
    assert agent.post(path + "/heartbeat", json=lease_body(claimed)).json()["stop"] is False
    body = {**lease_body(claimed), "chunk_index": 0, "text": "fixture output α\n"}
    assert agent.post(path + "/logs", json=body).status_code == 204
    assert agent.post(path + "/logs", json=body).status_code == 204
    assert agent.post(path + "/logs", json={**body, "text": "different"}).status_code == 409
    assert agent.post(path + "/logs", json={**body, "chunk_index": 2}).status_code == 409
    result = finish(agent, claimed).json()
    assert result["status"] == "succeeded" and result["used_cpu_seconds"] == 14
    assert finish(agent, claimed, status="failed", error_code="execution_failed").json() == result
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(client.headers))
    assert restarted.get(job_path(owner, job)).json() == result
    events = restarted.get(job_path(owner, job) + "/events").json()
    assert [entry["event"]["type"] for entry in events] == ["state", "state", "log", "state"]
    assert restarted.get(job_path(owner, job) + "/events", params={"after": events[-1]["sequence"]}).json() == []
    config_body = restarted.get(f"/api/organizations/{owner['organization_id']}/compute-configuration").json()
    assert config_body["balance"]["used_cpu_seconds"] == 14 and config_body["balance"]["held_cpu_seconds"] == 0
    for body in (result, events, config_body, client.get(f"/api/organizations/{owner['organization_id']}/compute-workers").json()):
        assert registration["token"] not in json.dumps(body) and claimed["lease"] not in json.dumps(body)
        assert "lease_hash" not in json.dumps(body) and "token_hash" not in json.dumps(body)
    with store.transaction() as db:
        assert registration["token"] not in str([dict(row) for row in db.execute("SELECT * FROM compute_workers")])


def test_queued_cancel_releases_and_active_cancel_requires_stopped_worker(compute):
    _, _, _, client, owner, _ = compute
    queued = enqueue(compute)
    cancelled = client.post(job_path(owner, queued) + "/cancel", json={}).json()
    assert cancelled["status"] == "cancelled" and cancelled["used_cpu_seconds"] == 0
    _, agent = worker(compute)
    job = enqueue(compute, "active")
    claimed = agent.post("/api/worker/claim", json={}).json()
    response = client.post(job_path(owner, job) + "/cancel", json={}).json()
    assert response["status"] == "running" and response["used_cpu_seconds"] is None
    assert agent.post(f"/api/worker/jobs/{job['id']}/heartbeat", json=lease_body(claimed)).json()["stop"] is True
    assert finish(agent, claimed).json()["status"] == "cancelled"


def test_lost_worker_never_requeues_and_can_report_same_attempt_late(compute):
    config, store, _, client, owner, _ = compute
    job = enqueue(compute)
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (job["id"],))
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(client.headers))
    unknown = restarted.get(job_path(owner, job)).json()
    assert unknown["status"] == "outcome_unknown" and unknown["accounted_at"] is None
    assert agent.post("/api/worker/claim", json={}).json() is None
    _, spare = worker(compute, name="Spare")
    assert spare.post("/api/worker/claim", json={}).json() is None
    assert agent.post(f"/api/worker/jobs/{job['id']}/heartbeat", json=lease_body(claimed)).json()["stop"] is True
    assert finish(agent, claimed).json()["status"] == "succeeded"
    assert restarted.get(job_path(owner, job)).json()["used_cpu_seconds"] == 7


def test_revoke_drain_and_confirmed_cleanup_reconciliation(compute):
    _, store, _, client, owner, _ = compute
    job = enqueue(compute)
    registration, agent = worker(compute)
    root = f"/api/organizations/{owner['organization_id']}"
    worker_path = root + f"/compute-workers/{registration['id']}"
    assert client.patch(worker_path, json={"draining": True}).status_code == 204
    assert agent.post("/api/worker/claim", json={}).json() is None
    client.patch(worker_path, json={"draining": False})
    claimed = agent.post("/api/worker/claim", json={}).json()
    client.patch(worker_path, json={"draining": True})
    assert agent.post(f"/api/worker/jobs/{job['id']}/heartbeat", json=lease_body(claimed)).json()["stop"] is False
    assert client.delete(worker_path).status_code == 204
    assert finish(agent, claimed).status_code == 401
    assert agent.post("/api/worker/claim", json={}).status_code == 401
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (job["id"],))
    reconcile = job_path(owner, job) + "/reconciliation"
    assert client.put(reconcile, json={"elapsed_ms": 6001, "cleanup_confirmed": False}).status_code == 400
    assert client.get(root + "/compute-configuration").json()["balance"]["held_cpu_seconds"] == 120
    outcome = client.put(reconcile, json={"elapsed_ms": 6001, "cleanup_confirmed": True}).json()
    assert outcome["status"] == "failed" and outcome["error_code"] == "worker_lost" and outcome["used_cpu_seconds"] == 7
    assert client.put(reconcile, json={"elapsed_ms": 0, "cleanup_confirmed": True}).status_code == 409


@pytest.mark.parametrize("change", ["remove_member", "viewer", "policy", "quota", "image"])
def test_queued_and_running_jobs_recheck_current_authority(compute, installation, change):
    config, _, state, client, owner, topics = compute
    _, other, member = invited(installation)
    member_client = TestClient(client.app, base_url=config.public_url, headers=dict(other.headers))
    root = f"/api/organizations/{owner['organization_id']}"
    path = root + f"/topics/{topics[2]['id']}/compute-jobs"
    first = member_client.post(path, json=submission("first")).json()
    queued = member_client.post(path, json=submission("second")).json()
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    assert claimed["id"] == first["id"]
    if change == "remove_member":
        client.delete(root + f"/members/{member['user_id']}")
    elif change == "viewer":
        client.patch(root + f"/members/{member['user_id']}", json={"role": "viewer"})
    elif change == "policy":
        client.put(root + "/compute-policy", json={"allowed_images": [], "monthly_cpu_seconds": 10000})
    elif change == "quota":
        client.put(root + "/compute-policy", json={"allowed_images": [IMAGE["id"]], "monthly_cpu_seconds": 1})
    else:
        state.images[IMAGE["id"]] = ComputeImage.parse({**IMAGE, "source_revision": "e" * 40})
    assert agent.post(f"/api/worker/jobs/{first['id']}/heartbeat", json=lease_body(claimed)).json()["stop"] is True
    assert finish(agent, claimed).json()["status"] == "cancelled"
    assert agent.post("/api/worker/claim", json={}).json() is None
    result = client.get(job_path(owner, queued)).json()
    assert result["status"] == "failed" and result["error_code"] == "policy_changed" and result["used_cpu_seconds"] == 0


def test_tenant_role_and_credential_boundaries(compute, installation):
    config, _, _, client, owner, topics = compute
    job = enqueue(compute)
    _, other, member = invited(installation)
    _, viewer, _ = invited(installation, role="viewer", email="viewer@example.org")
    member_client = TestClient(client.app, base_url=config.public_url, headers=dict(other.headers))
    root = f"/api/organizations/{owner['organization_id']}"
    assert member_client.post(job_path(owner, job) + "/cancel", json={}).status_code == 403
    assert viewer.get(job_path(owner, job)).status_code == 200
    assert viewer.post(root + f"/topics/{topics[2]['id']}/compute-jobs", json=submission()).status_code == 403
    assert member_client.get(root + "/compute-workers").status_code == 403
    assert member_client.put(root + "/compute-policy", json={"allowed_images": [], "monthly_cpu_seconds": 0}).status_code == 403
    _, foreign_client, foreign_member = invited(installation, role="admin", email="foreign-admin@example.org")
    installation[1].change_member(owner["user_id"], owner["organization_id"], foreign_member["user_id"], "owner")
    foreign = foreign_client.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).json()["id"]
    assert client.get(job_path(owner, job).replace(owner["organization_id"], foreign)).status_code == 404
    record, agent = worker(compute)
    foreign_worker = ComputeStore(client.app.state.store, client.app.state.compute.images).register_worker(
        foreign_member["user_id"], foreign, "Other worker", [IMAGE["id"]], DEFAULT_RESOURCES
    )
    claimed = agent.post("/api/worker/claim", json={}).json()
    alien = TestClient(client.app, base_url=config.public_url, headers={"Authorization": "Bearer " + foreign_worker["token"]})
    assert finish(alien, claimed).status_code == 404
    assert client.post("/api/worker/claim", json={}).status_code == 401
    for path in ("/api/auth/me", root + "/provider-keys", root + "/compute-configuration", job_path(owner, job)):
        assert agent.get(path).status_code == 401
    bad = {**lease_body(claimed), "lease": record["token"]}
    assert agent.post(f"/api/worker/jobs/{job['id']}/heartbeat", json=bad).status_code == 409
    assert agent.post(f"/api/worker/jobs/{job['id']}/heartbeat", json={**lease_body(claimed), "attempt_id": "f" * 32}).status_code == 409


def test_artifacts_are_bounded_integrity_checked_durable_and_download_only(compute):
    config, store, _, client, owner, _ = compute
    job = enqueue(compute)
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    path = f"/api/worker/jobs/{job['id']}/artifacts"
    content = b"<script>not executed</script>" + b"x" * CHUNK_BYTES
    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
    body = {**lease_body(claimed), "name": "result.html", "size_bytes": len(content), "digest": checksum}
    first = agent.post(path, json=body)
    assert first.status_code == 201, first.text
    artifact = first.json()
    assert agent.post(path, json=body).json() == artifact
    assert agent.post(path, json={**body, "size_bytes": 0}).status_code == 409
    download_path = job_path(owner, job) + f"/artifacts/{artifact['id']}"
    assert client.get(download_path).status_code == 404
    assert agent.post(path + f"/{artifact['id']}/commit", json=lease_body(claimed)).status_code == 409
    for index in (1, 0):
        chunk = {
            **lease_body(claimed),
            "chunk_index": index,
            "content_base64": base64.b64encode(content[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]).decode(),
        }
        endpoint = path + f"/{artifact['id']}/chunks"
        assert agent.put(endpoint, json=chunk).status_code == 204
        assert agent.put(endpoint, json=chunk).status_code == 204
    assert agent.post(path + f"/{artifact['id']}/commit", json=lease_body(claimed)).status_code == 200
    finish(agent, claimed).raise_for_status()
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(client.headers))
    download = restarted.get(download_path)
    assert download.status_code == 200 and download.content == content
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"] == 'attachment; filename="result.html"'
    assert download.headers["x-content-type-options"] == "nosniff"
    artifacts = restarted.get(job_path(owner, job) + "/artifacts").json()
    assert len(artifacts) == 1 and artifacts[0]["digest"] == checksum
    chunks = list((store.data_dir / "artifacts" / artifact["id"]).iterdir())
    assert len(chunks) == 2 and all(path.stat().st_mode & 0o077 == 0 for path in chunks)


@pytest.mark.parametrize("name", ["../outside", "a/b", "a\\b", ".", "..", "\nfile", 'file".html', "a" * 129])
def test_artifact_paths_never_accept_traversal_or_header_injection(compute, name):
    job = enqueue(compute)
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    response = agent.post(
        f"/api/worker/jobs/{job['id']}/artifacts",
        json={**lease_body(claimed), "name": name, "size_bytes": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()},
    )
    assert response.status_code in {400, 422}


def test_artifact_digest_mismatch_and_total_budget_reject(compute):
    _, _, _, client, owner, _ = compute
    job = enqueue(compute, resources={**DEFAULT_RESOURCES, "artifact_bytes": 3})
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    path = f"/api/worker/jobs/{job['id']}/artifacts"
    body = {**lease_body(claimed), "name": "result.txt", "size_bytes": 3, "digest": "sha256:" + hashlib.sha256(b"abc").hexdigest()}
    artifact = agent.post(path, json=body).json()
    assert agent.post(path, json={**body, "name": "too-many.txt", "size_bytes": 1}).status_code == 413
    chunk = {**lease_body(claimed), "chunk_index": 0, "content_base64": base64.b64encode(b"bad").decode()}
    assert agent.put(path + f"/{artifact['id']}/chunks", json=chunk).status_code == 204
    assert agent.post(path + f"/{artifact['id']}/commit", json=lease_body(claimed)).status_code == 409
    assert client.get(job_path(owner, job) + "/artifacts").json() == []


def test_strict_inputs_log_limits_and_wall_time_outcomes(compute):
    _, _, _, client, owner, topics = compute
    submit_path = f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs"
    assert client.post(submit_path, json=submission(resources={**DEFAULT_RESOURCES, "cpu": True})).status_code == 422
    assert client.post(submit_path, json=submission(resources={**DEFAULT_RESOURCES, "artifact_bytes": 1024**3})).status_code == 400
    assert client.post(submit_path, json=submission(language="julia")).status_code == 400
    assert client.post(submit_path, json=submission(program="\x00")).status_code == 400
    assert client.post(submit_path, json=submission(resources={**DEFAULT_RESOURCES, "cpu": 8})).status_code == 400
    job = enqueue(compute, resources={**DEFAULT_RESOURCES, "wall_time_seconds": 1})
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    response = agent.post(f"/api/worker/jobs/{job['id']}/logs", json={**lease_body(claimed), "chunk_index": 0, "text": "α" * 8192})
    assert response.status_code == 400
    result = finish(agent, claimed).json()
    assert result["status"] == "failed" and result["error_code"] == "wall_time_limit" and result["used_cpu_seconds"] == 1


def test_request_and_worker_capacity_limits(compute):
    _, _, _, client, owner, _ = compute
    policy = client.put(f"/api/organizations/{owner['organization_id']}/compute-policy", json={
        "allowed_images": [IMAGE["id"]], "monthly_cpu_seconds": 10000, "max_active_jobs": 2,
        "max_pending_jobs": 8, "requests_per_user_15_minutes": 20,
    })
    assert policy.status_code == 200, policy.text
    for index in range(8):
        enqueue(compute, str(index))
    topics = compute[5]
    path = f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs"
    assert client.post(path, json=submission("overflow")).status_code == 429
    jobs = client.get(path).json()
    for job in jobs:
        client.post(job_path(owner, job) + "/cancel", json={})
    for index in range(8, 20):
        job = enqueue(compute, str(index))
        client.post(job_path(owner, job) + "/cancel", json={})
    assert client.post(path, json=submission("rate-limit")).status_code == 429


@pytest.mark.parametrize(
    "change",
    [
        {"reference": "registry.example.org/research/cpu:latest"},
        {"languages": ["sage", "sage"]},
        {"languages": ["bash"]},
        {"source_revision": "main"},
        {"sbom_digest": "missing"},
        {"capabilities_path": "/etc/../outside"},
        {"maximum_resources": {**DEFAULT_RESOURCES, "cpu": True}},
    ],
)
def test_catalog_rejects_mutability_and_invalid_evidence(change):
    with pytest.raises(ValueError):
        ComputeImage.parse({**IMAGE, **change})


def test_catalog_bounds_and_missing_resources(tmp_path):
    assert load_catalog(None) == {}
    path = tmp_path / "images.json"
    private_file(path, canonical([IMAGE, IMAGE]).encode())
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(path)
    with pytest.raises(ValueError):
        resources({"cpu": 1})
    assert (
        ComputeImage.parse({**IMAGE, "sbom_path": "/usr/share/research/sbom.spdx.json"}).sbom_path == "/usr/share/research/sbom.spdx.json"
    )


def test_catalog_capabilities_are_verified_and_kept_out_of_image_identity(tmp_path):
    document = {"runtimes": [{"name": "python", "version": "3.12"}], "tools": [{"name": "pdflatex", "version": "2023"}],
                "paths": {"output": "/work/out"}, "network": {"supported_modes": ["denied"]}, "restrictions": ["No GPU."]}
    image = {**IMAGE, "capabilities_digest": capabilities_digest(document)}
    path = tmp_path / "images.json"
    private_file(path, canonical([{**image, "capabilities": document}]).encode())
    # The record jobs and workers compare is unchanged; the document is available beside it.
    assert load_catalog(path)[IMAGE["id"]].record() == ComputeImage.parse(image).record()
    assert load_capabilities(path) == {IMAGE["id"]: document}
    plain = tmp_path / "plain.json"
    private_file(plain, canonical([image]).encode())
    assert load_capabilities(plain) == {}
    altered = tmp_path / "altered.json"
    private_file(altered, canonical([{**image, "capabilities": {**document, "tools": []}}]).encode())
    with pytest.raises(ValueError, match="capabilities digest"):
        load_catalog(altered)


def test_schema_three_upgrade_keeps_beta_and_creates_disabled_compute(installation, tmp_path):
    config, _, _, owner = installation
    old_dir = tmp_path / "old-state"
    old_dir.mkdir(mode=0o700)
    for name in ("encryption.key", "setup-token"):
        private_file(old_dir / name, (config.data_dir / name).read_bytes())
    private_file(old_dir / "server.sqlite3", b"")
    with sqlite3.connect(old_dir / "server.sqlite3") as old, sqlite3.connect(config.data_dir / "server.sqlite3") as current:
        for migration in MIGRATIONS[:3]:
            for statement in migration:
                old.execute(statement)
        tables = [row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            quoted_table = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in old.execute(f"PRAGMA table_info({quoted_table})")]
            quoted_columns = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
            rows = current.execute(f"SELECT {quoted_columns} FROM {quoted_table}").fetchall()
            old.execute(f"DELETE FROM {quoted_table}")
            old.executemany(f"INSERT INTO {quoted_table}({quoted_columns}) VALUES({','.join('?' for _ in columns)})", rows)
        old.execute("PRAGMA user_version=3")
    migrated = Store(old_dir)
    state = ComputeStore(migrated, {})
    assert state.configuration(owner["user_id"], owner["organization_id"])["policy"]["monthly_cpu_seconds"] == 0
    assert migrated.session(owner["access_token"])["user_id"] == owner["user_id"]


def test_resource_and_language_mismatch_skips_worker_without_consuming_job(compute):
    job = enqueue(compute, resources={**DEFAULT_RESOURCES, "cpu": 2})
    _, small = worker(compute, resources=DEFAULT_RESOURCES)
    assert small.post("/api/worker/claim", json={}).json() is None
    _, large = worker(compute)
    assert large.post("/api/worker/claim", json={}).json()["id"] == job["id"]


def test_only_a_drained_settled_worker_changes_its_limits_within_its_images(compute, installation):
    config, _, _, client, owner, _ = compute
    record, agent = worker(compute, resources=DEFAULT_RESOURCES)
    root = f"/api/organizations/{owner['organization_id']}"
    path = root + f"/compute-workers/{record['id']}"
    larger = {**DEFAULT_RESOURCES, "cpu": 2}
    _, other, _ = invited(installation, email="limits-member@example.org")
    member = TestClient(client.app, base_url=config.public_url, headers=dict(other.headers))
    small = enqueue(compute, "small")
    claimed = agent.post("/api/worker/claim", json={}).json()
    assert claimed["id"] == small["id"]
    response = client.put(path + "/resources", json=larger)
    assert response.status_code == 409 and "drain" in response.json()["detail"]
    client.patch(path, json={"draining": True})
    response = client.put(path + "/resources", json=larger)
    assert response.status_code == 409 and "running or unknown" in response.json()["detail"]
    assert finish(agent, claimed).json()["status"] == "succeeded"
    response = client.put(path + "/resources", json={**larger, "cpu": 8})
    assert response.status_code == 409 and IMAGE["id"] in response.json()["detail"]
    assert client.put(path + "/resources", json={**larger, "cpu": 0}).status_code == 422
    assert member.put(path + "/resources", json=larger).status_code == 403
    assert agent.get("/api/worker/identity").json()["resources"] == DEFAULT_RESOURCES

    changed = client.put(path + "/resources", json=larger)
    assert changed.status_code == 200 and changed.json()["resources"] == larger and "token" not in changed.json()
    assert agent.get("/api/worker/identity").json()["resources"] == larger
    assert agent.get("/api/worker/status").json()["resources"] == larger
    assert client.put(path + "/resources", json=larger).json()["resources"] == larger
    large = enqueue(compute, "large", resources={**DEFAULT_RESOURCES, "cpu": 2})
    assert agent.post("/api/worker/claim", json={}).json() is None
    client.patch(path, json={"draining": False})
    assert agent.post("/api/worker/claim", json={}).json()["id"] == large["id"]
    client.delete(path)
    response = client.put(path + "/resources", json=DEFAULT_RESOURCES)
    assert response.status_code == 409 and "replacement" in response.json()["detail"]
    assert client.put(root + "/compute-workers/" + "0" * 32 + "/resources", json=larger).status_code == 404


def test_lease_expiry_allows_fenced_delivery_but_occupies_active_capacity(compute):
    _, store, _, client, owner, _ = compute
    root = f"/api/organizations/{owner['organization_id']}"
    client.put(root + "/compute-policy", json={"allowed_images": [IMAGE["id"]], "monthly_cpu_seconds": 10000, "max_active_jobs": 1})
    job = enqueue(compute)
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (job["id"],))
    assert client.get(job_path(owner, job)).json()["status"] == "outcome_unknown"
    enqueue(compute, "waiting")
    _, spare = worker(compute)
    assert spare.post("/api/worker/claim", json={}).json() is None
    path = f"/api/worker/jobs/{job['id']}"
    assert agent.post(path + "/logs", json={**lease_body(claimed), "chunk_index": 0, "text": "late"}).status_code == 204
    assert (
        agent.post(
            path + "/artifacts",
            json={**lease_body(claimed), "name": "late", "size_bytes": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()},
        ).status_code
        == 201
    )
    response = client.put(job_path(owner, job) + "/reconciliation", json={"cleanup_confirmed": True, "elapsed_ms": 1000})
    assert response.status_code == 200
    assert spare.post("/api/worker/claim", json={}).json()["client_id"] == "waiting"


def test_deadline_allows_worker_startup_before_the_program_runs(compute, monkeypatch):
    # A Modal relay starts a VM and downloads the image after claiming, so a heartbeating attempt must outlive the
    # program's wall time plus delivery. It ends only after the shared startup allowance is spent too.
    from types import SimpleNamespace

    from reasoning_ledger.server import compute_modal, compute_store

    _, _, _, client, owner, _ = compute
    job = enqueue(compute, resources={**DEFAULT_RESOURCES, "wall_time_seconds": 30})
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    delivery = 120 + 2
    assert compute_modal.OVERHEAD_SECONDS == compute_store.STARTUP_SECONDS
    assert claimed["deadline_at"] == claimed["started_at"] + compute_store.STARTUP_SECONDS + 30 + delivery
    clock = [claimed["started_at"]]
    monkeypatch.setattr(compute_store, "time", SimpleNamespace(time=lambda: clock[0]))
    heartbeat = f"/api/worker/jobs/{job['id']}/heartbeat"
    while clock[0] < claimed["started_at"] + 30 + delivery + 60:
        clock[0] += 10
        assert agent.post(heartbeat, json=lease_body(claimed)).json()["stop"] is False
    assert client.get(job_path(owner, job)).json()["status"] == "running"
    clock[0] = claimed["deadline_at"]
    assert client.get(job_path(owner, job)).json()["status"] == "outcome_unknown"


def test_partial_and_empty_artifact_uploads_can_resume_across_restart(compute):
    config, _, _, client, owner, _ = compute
    job = enqueue(compute)
    registration, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    endpoint = f"/api/worker/jobs/{job['id']}/artifacts"
    content = b"a" * CHUNK_BYTES + b"last bytes"
    meta = {
        **lease_body(claimed),
        "name": "data.bin",
        "size_bytes": len(content),
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }
    artifact = agent.post(endpoint, json=meta).json()
    chunk = {**lease_body(claimed), "chunk_index": 0, "content_base64": base64.b64encode(content[:CHUNK_BYTES]).decode()}
    assert agent.put(endpoint + f"/{artifact['id']}/chunks", json=chunk).status_code == 204
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(agent.headers))
    assert restarted.post(endpoint, json=meta).json() == artifact
    assert restarted.put(endpoint + f"/{artifact['id']}/chunks", json=chunk).status_code == 204
    assert (
        restarted.put(
            endpoint + f"/{artifact['id']}/chunks",
            json={**chunk, "chunk_index": 1, "content_base64": base64.b64encode(content[CHUNK_BYTES:]).decode()},
        ).status_code
        == 204
    )
    assert restarted.post(endpoint + f"/{artifact['id']}/commit", json=lease_body(claimed)).status_code == 200
    empty = restarted.post(
        endpoint, json={**lease_body(claimed), "name": "empty.txt", "size_bytes": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()}
    ).json()
    assert restarted.post(endpoint + f"/{empty['id']}/commit", json=lease_body(claimed)).status_code == 200
    assert client.get(job_path(owner, job) + f"/artifacts/{empty['id']}").content == b""
    assert registration["token"] not in json.dumps(client.get(job_path(owner, job) + "/artifacts").json())


def test_crash_orphan_chunk_is_adopted_only_for_identical_retry(compute):
    _, store, state, client, owner, _ = compute
    job = enqueue(compute)
    record, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    artifact = state.begin_artifact(
        record["token"], job["id"], claimed["attempt_id"], claimed["lease"], "result", 3, "sha256:" + hashlib.sha256(b"abc").hexdigest()
    )
    directory = store.data_dir / "artifacts" / artifact["id"]
    directory.mkdir(mode=0o700)
    private_file(directory / "0", b"abc")
    endpoint = f"/api/worker/jobs/{job['id']}/artifacts/{artifact['id']}"
    body = {**lease_body(claimed), "chunk_index": 0, "content_base64": base64.b64encode(b"bad").decode()}
    assert agent.put(endpoint + "/chunks", json=body).status_code == 409
    assert agent.put(endpoint + "/chunks", json={**body, "content_base64": base64.b64encode(b"abc").decode()}).status_code == 204
    assert agent.post(endpoint + "/commit", json=lease_body(claimed)).status_code == 200
    assert client.get(job_path(owner, job) + f"/artifacts/{artifact['id']}").content == b"abc"


def test_artifact_symlinks_and_exposed_state_are_refused(compute, tmp_path):
    _, store, state, _, _, _ = compute
    job = enqueue(compute)
    record, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    artifact = state.begin_artifact(
        record["token"], job["id"], claimed["attempt_id"], claimed["lease"], "result", 3, "sha256:" + hashlib.sha256(b"abc").hexdigest()
    )
    outside = tmp_path / "outside"
    private_file(outside, b"sensitive outside content")
    directory = store.data_dir / "artifacts" / artifact["id"]
    directory.mkdir(mode=0o700)
    (directory / "0").symlink_to(outside)
    with pytest.raises(OSError):
        state.artifact_chunk(
            record["token"], job["id"], claimed["attempt_id"], claimed["lease"], artifact["id"], 0, base64.b64encode(b"abc").decode()
        )
    assert outside.read_bytes() == b"sensitive outside content"
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_artifact_chunks").fetchone()[0] == 0
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        state.artifact_chunk(
            record["token"], job["id"], claimed["attempt_id"], claimed["lease"], artifact["id"], 0, base64.b64encode(b"abc").decode()
        )


def test_cookie_origin_csrf_and_secret_validation_apply_to_compute(compute):
    config, _, _, client, owner, topics = compute
    access = client.headers["Authorization"].split(" ")[1]
    cookie = TestClient(client.app, base_url=config.public_url)
    cookie.cookies.set("__Host-reasoning_session", access)
    path = f"/api/organizations/{owner['organization_id']}/topics/{topics[2]['id']}/compute-jobs"
    assert cookie.post(path, json=submission()).status_code == 403
    response = cookie.post("/api/auth/login", json={"email": "owner@example.org", "password": "a very long test password"})
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    assert cookie.post(path, json=submission(), headers={"Origin": config.public_url}).status_code == 403
    assert cookie.post(path, json=submission(), headers={"Origin": config.public_url, "X-CSRF-Token": csrf}).status_code == 202
    assert client.get(path, headers={"Origin": "https://foreign.example.org"}).status_code == 403
    registration, agent = worker(compute)
    invalid = agent.post("/api/worker/jobs/absent/heartbeat", json={"attempt_id": "bad", "lease": registration["token"]})
    assert invalid.status_code == 422 and registration["token"] not in invalid.text
    assert agent.post("/api/worker/claim", content="not JSON", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(
        path, content=json.dumps(submission(program="\ud800")), headers={"Content-Type": "application/json"}
    ).status_code in {400, 422}


def test_uncertain_prior_month_reservations_remain_held(compute):
    _, store, state, _, owner, _ = compute
    job = enqueue(compute)
    _, agent = worker(compute)
    agent.post("/api/worker/claim", json={})
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET created_at=0,started_at=0,lease_expires_at=0 WHERE id=?", (job["id"],))
    balance = state.configuration(owner["user_id"], owner["organization_id"])["balance"]
    assert balance["held_cpu_seconds"] == 120 and balance["used_cpu_seconds"] == 0


def test_real_http_server_crash_resumes_fenced_artifact_delivery(compute):
    config, _, _, client, owner, _ = compute
    job = enqueue(compute)
    registration, _ = worker(compute)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    secrets = [registration["token"], owner["access_token"]]

    @contextmanager
    def server():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reasoning_ledger.server.cli",
                "--data-dir",
                str(config.data_dir),
                "serve",
                "--public-url",
                url,
                "--port",
                str(port),
                "--compute-images",
                str(config.compute_images),
                "--no-inference-worker",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with httpx.Client(base_url=url, timeout=2, headers={"Authorization": "Bearer " + registration["token"]}) as agent:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        pytest.fail("compute fixture server exited before readiness")
                    try:
                        response = agent.get("/api/health")
                        response.raise_for_status()
                        break
                    except httpx.TransportError:
                        if time.monotonic() >= deadline:
                            pytest.fail("compute fixture server did not become ready")
                        time.sleep(0.05)
                yield process, agent
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                output, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate(timeout=5)
            assert not any(value.encode() in output + errors for value in secrets)

    content = b"a" * CHUNK_BYTES + b"durable last chunk"
    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
    endpoint = f"/api/worker/jobs/{job['id']}"
    with server() as (process, agent):
        claimed = agent.post("/api/worker/claim", json={}).json()
        assert claimed["id"] == job["id"]
        secrets.append(claimed["lease"])
        artifact = agent.post(
            endpoint + "/artifacts", json={**lease_body(claimed), "name": "result.bin", "size_bytes": len(content), "digest": checksum}
        ).json()
        chunk_path = endpoint + f"/artifacts/{artifact['id']}/chunks"
        assert (
            agent.put(
                chunk_path,
                json={**lease_body(claimed), "chunk_index": 0, "content_base64": base64.b64encode(content[:CHUNK_BYTES]).decode()},
            ).status_code
            == 204
        )
        assert (
            agent.post(
                endpoint + "/logs", json={**lease_body(claimed), "chunk_index": 0, "text": "non-executing protocol fixture"}
            ).status_code
            == 204
        )
        process.kill()
        process.wait(timeout=5)
    with server() as (_, agent):
        assert agent.post("/api/worker/claim", json={}).json() is None
        assert agent.post(endpoint + "/heartbeat", json=lease_body(claimed)).json()["stop"] is False
        assert (
            agent.put(
                chunk_path,
                json={**lease_body(claimed), "chunk_index": 1, "content_base64": base64.b64encode(content[CHUNK_BYTES:]).decode()},
            ).status_code
            == 204
        )
        committed = agent.post(endpoint + f"/artifacts/{artifact['id']}/commit", json=lease_body(claimed))
        assert committed.status_code == 200, committed.text
        outcome = finish(agent, claimed)
        assert outcome.status_code == 200 and outcome.json()["status"] == "succeeded"
        with httpx.Client(base_url=url, timeout=2, headers=dict(client.headers)) as human:
            download = human.get(job_path(owner, job) + f"/artifacts/{artifact['id']}")
            assert download.status_code == 200 and download.content == content
            events = human.get(job_path(owner, job) + "/events").json()
            assert [entry["event"]["type"] for entry in events] == ["state", "state", "log", "artifact", "state"]


def test_artifact_hashing_does_not_block_heartbeats_and_rechecks_revocation(compute, monkeypatch):
    import threading

    _, _, state, client, owner, _ = compute
    job = enqueue(compute)
    registration, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    credential, attempt, lease = registration["token"], claimed["attempt_id"], claimed["lease"]
    artifact = state.begin_artifact(credential, job["id"], attempt, lease, "output.txt", 3, "sha256:" + hashlib.sha256(b"abc").hexdigest())
    state.artifact_chunk(credential, job["id"], attempt, lease, artifact["id"], 0, base64.b64encode(b"abc").decode())
    reading, release = threading.Event(), threading.Event()
    original = state._read_chunk

    def read(artifact_id, index):
        reading.set()
        assert release.wait(5)
        return original(artifact_id, index)

    monkeypatch.setattr(state, "_read_chunk", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        commit = pool.submit(state.commit_artifact, credential, job["id"], attempt, lease, artifact["id"])
        try:
            assert reading.wait(5)
            heartbeat = pool.submit(state.heartbeat, credential, job["id"], attempt, lease)
            assert heartbeat.result(timeout=2)["stop"] is False
            state.change_worker(owner["user_id"], owner["organization_id"], registration["id"], revoke=True)
        finally:
            release.set()
        with pytest.raises(StoreError, match="credential"):
            commit.result(timeout=5)
    assert client.get(job_path(owner, job) + "/artifacts").json() == []


def test_log_byte_limit_and_exclusive_event_pages(compute):
    _, _, state, _, owner, _ = compute
    job = enqueue(compute)
    registration, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={}).json()
    credentials = (registration["token"], job["id"], claimed["attempt_id"], claimed["lease"])
    for index in range(256):
        state.append_log(*credentials, index, "x" * 4096)
    with pytest.raises(StoreError, match="1 MiB"):
        state.append_log(*credentials, 256, "x")
    first = state.events(owner["user_id"], owner["organization_id"], job["id"])
    second = state.events(owner["user_id"], owner["organization_id"], job["id"], first[-1]["sequence"])
    assert len(first) == 200 and len(second) == 58
    assert first[-1]["sequence"] < second[0]["sequence"]
    assert state.job(owner["user_id"], owner["organization_id"], job["id"])["log_bytes"] == 1024**2


def test_artifact_count_and_cross_job_binding(compute):
    _, _, state, _, _, _ = compute
    first = enqueue(compute, "first")
    second = enqueue(compute, "second")
    first_worker, first_agent = worker(compute)
    second_worker, second_agent = worker(compute)
    first_claim = first_agent.post("/api/worker/claim", json={}).json()
    second_claim = second_agent.post("/api/worker/claim", json={}).json()
    assert first_claim["id"] == first["id"] and second_claim["id"] == second["id"]
    checksum = "sha256:" + hashlib.sha256(b"").hexdigest()
    for index in range(64):
        artifact = state.begin_artifact(
            first_worker["token"], first["id"], first_claim["attempt_id"], first_claim["lease"], f"empty-{index}", 0, checksum
        )
    with pytest.raises(StoreError, match="count"):
        state.begin_artifact(first_worker["token"], first["id"], first_claim["attempt_id"], first_claim["lease"], "extra", 0, checksum)
    with pytest.raises(StoreError, match="not found"):
        state.commit_artifact(second_worker["token"], second["id"], second_claim["attempt_id"], second_claim["lease"], artifact["id"])


def data_file(compute, topic, body, name="input.bin", *, finish=True):
    from reasoning_ledger.server.research_media import DATA_FILE, ResearchMedia

    _, store, _, _, owner, _ = compute
    media, user, org = ResearchMedia(store), owner["user_id"], owner["organization_id"]
    start = media.begin(user, org, topic, name, DATA_FILE, len(body), hashlib.sha256(body).hexdigest())
    for offset in range(0, len(body), CHUNK_BYTES):
        media.chunk(user, org, topic, start["id"], offset // CHUNK_BYTES, base64.b64encode(body[offset : offset + CHUNK_BYTES]).decode())
    if not finish:
        return {"id": start["id"], "sha256": hashlib.sha256(body).hexdigest()}
    return media.finish(user, org, topic, start["id"])


def pin(file):
    return {"identity": file["id"], "sha256": file["sha256"]}


def test_attachments_pin_same_topic_and_same_project_files_and_leave_plain_jobs_unchanged(compute):
    _, _, state, _, _, topics = compute
    own = data_file(compute, topics[2]["id"], b"\x00\x01 own topic bytes", "data set (v1).bin")
    sibling = data_file(compute, topics[1]["id"], b"x" * (CHUNK_BYTES + 5), "sibling.csv")
    job = enqueue(compute, "pinned", attachments=[pin(own), pin(sibling)])
    assert job["parameters"]["attachments"] == [
        {"identity": own["id"], "sha256": own["sha256"], "topic_id": topics[2]["id"], "name": "data set (v1).bin",
         "mime": "application/octet-stream", "size_bytes": 18, "path": "01-data_set__v1_.bin"},
        {"identity": sibling["id"], "sha256": sibling["sha256"], "topic_id": topics[1]["id"], "name": "sibling.csv",
         "mime": "application/octet-stream", "size_bytes": CHUNK_BYTES + 5, "path": "02-sibling.csv"},
    ]
    assert enqueue(compute, "pinned", attachments=[pin(own), pin(sibling)])["id"] == job["id"]
    plain = enqueue(compute, "plain")
    assert set(plain["parameters"]) == {"image_id", "language", "program", "resources"}
    assert enqueue(compute, "empty", attachments=[])["parameters"].keys() == plain["parameters"].keys()
    unpinned = state.parameters(IMAGE["id"], "sage", "1", DEFAULT_RESOURCES)
    assert unpinned == state.parameters(IMAGE["id"], "sage", "1", DEFAULT_RESOURCES, [])
    long = state.parameters(IMAGE["id"], "sage", "1", DEFAULT_RESOURCES, [pin(own)])
    assert long["attachments"] == [pin(own)]


def test_attachments_refuse_unverifiable_foreign_duplicate_excess_and_oversize_files(compute):
    _, store, state, client, owner, topics = compute
    user, org, topic = owner["user_id"], owner["organization_id"], topics[2]["id"]
    good = data_file(compute, topic, b"good")
    pending = data_file(compute, topic, b"pending", finish=False)
    elsewhere = structure(client, org)[2][2]["id"]
    foreign = data_file(compute, elsewhere, b"foreign")
    deleted_topic = topics[0]["id"]
    removed = data_file(compute, deleted_topic, b"in a deleted topic")
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=? WHERE id=?", (int(time.time()), deleted_topic))

    def refused(items, status, **limits):
        parameters = state.parameters(IMAGE["id"], "sage", "print(1)", {**DEFAULT_RESOURCES, **limits}, items)
        with pytest.raises(StoreError) as error:
            state.enqueue(user, org, topic, "refused", parameters)
        assert error.value.status == status, str(error.value)

    refused([{**pin(good), "sha256": "0" * 64}], 404)
    refused([pin(pending)], 404)
    refused([pin(foreign)], 404)
    refused([pin(removed)], 404)
    with pytest.raises(StoreError) as duplicate:
        state.parameters(IMAGE["id"], "sage", "print(1)", DEFAULT_RESOURCES, [pin(good), pin(good)])
    assert duplicate.value.status == 400
    with pytest.raises(StoreError) as excess:
        state.parameters(IMAGE["id"], "sage", "print(1)", DEFAULT_RESOURCES, [pin({"id": f"{n:032x}", "sha256": "a" * 64})
                                                                              for n in range(5)])
    assert excess.value.status == 400
    root = f"/api/organizations/{org}/topics/{topic}/compute-jobs"
    five = [{"identity": f"{n:032x}", "sha256": "a" * 64} for n in range(5)]
    assert client.post(root, json=submission("five", attachments=five)).status_code == 422
    assert client.post(root, json=submission("extra", attachments=[{**pin(good), "path": "x"}])).status_code == 422
    # Inputs share /work with outputs: an 8 MiB disk with 1 MiB of artifacts leaves 2.5 MiB for inputs.
    large = data_file(compute, topic, b"z" * (3 * 1024**2), "large.bin")
    refused([pin(large)], 400, disk_bytes=8 * 1024**2)
    from reasoning_ledger.server.compute_store import attachment_allowance

    assert attachment_allowance({**DEFAULT_RESOURCES, "disk_bytes": 8 * 1024**2}) == 5 * 1024**2 // 2
    assert attachment_allowance({**DEFAULT_RESOURCES, "disk_bytes": 1024**3}) == 32 * 1024**2
    assert attachment_allowance({**DEFAULT_RESOURCES, "artifact_bytes": 128 * 1024**2}) == 0
    assert state.jobs(user, org, topic) == []


def test_claim_requires_attachment_support_and_rechecks_pinned_files(compute):
    _, store, state, client, owner, topics = compute
    sibling = data_file(compute, topics[1]["id"], b"sibling bytes")
    pinned = enqueue(compute, "pinned", attachments=[pin(sibling)])
    plain = enqueue(compute, "plain")
    _, old = worker(compute)
    claim_id = "e" * 32
    first = old.post("/api/worker/claim", json={"claim_id": claim_id, "supports_workflow_inputs": True}).json()
    assert first["id"] == plain["id"]
    _, new = worker(compute, name="New")
    claimed = new.post("/api/worker/claim", json={"claim_id": claim_id, "supports_attachments": True}).json()
    assert claimed["id"] == pinned["id"]
    assert new.post("/api/worker/claim", json={"claim_id": claim_id}).status_code == 409
    assert new.post("/api/worker/claim", json={"claim_id": claim_id, "supports_attachments": True}).json()["id"] == pinned["id"]
    assert new.post("/api/worker/claim", json={"supports_attachments": "yes"}).status_code == 422
    # A pinned file whose topic is gone no longer runs; a queued job fails before any worker claims it.
    again = enqueue(compute, "again", attachments=[pin(sibling)])
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=? WHERE id=?", (int(time.time()), topics[1]["id"]))
    assert new.post(f"/api/worker/jobs/{pinned['id']}/heartbeat", json=lease_body(claimed)).json()["stop"] is True
    assert finish(new, claimed).json()["status"] == "cancelled"
    assert finish(old, first).json()["status"] == "succeeded"
    _, third = worker(compute, name="Third")
    assert third.post("/api/worker/claim", json={"supports_attachments": True}).json() is None
    result = client.get(job_path(owner, again)).json()
    assert result["status"] == "failed" and result["error_code"] == "policy_changed"


def test_attachment_chunks_reach_only_the_running_attempt_for_pinned_files(compute):
    _, _, state, _, _, topics = compute
    body = bytes(range(256)) * 200
    file = data_file(compute, topics[2]["id"], body, "table.bin")
    other = data_file(compute, topics[2]["id"], b"not pinned")
    job = enqueue(compute, "pinned", attachments=[pin(file)])
    _, agent = worker(compute)
    claimed = agent.post("/api/worker/claim", json={"supports_attachments": True}).json()
    path = f"/api/worker/jobs/{job['id']}/attachments/{file['id']}/chunks/"
    first = agent.post(path + "0", json=lease_body(claimed))
    assert first.status_code == 200, first.text
    second = agent.post(path + "1", json=lease_body(claimed)).json()
    assert first.json()["next_index"] == 1 and second["next_index"] is None
    assert (second["index"], second["size_bytes"], second["sha256"]) == (1, len(body), file["sha256"])
    assert base64.b64decode(first.json()["data_base64"]) + base64.b64decode(second["data_base64"]) == body
    assert agent.post(path + "2", json=lease_body(claimed)).status_code == 416
    assert agent.post(path + "-1", json=lease_body(claimed)).status_code == 400
    assert agent.post(path + "0", json={**lease_body(claimed), "lease": "wrong"}).status_code == 409
    assert agent.post(path + "0", json={**lease_body(claimed), "attempt_id": "f" * 32}).status_code == 409
    unpinned = f"/api/worker/jobs/{job['id']}/attachments/{other['id']}/chunks/0"
    assert agent.post(unpinned, json=lease_body(claimed)).status_code == 404
    _, stranger = worker(compute, name="Stranger")
    assert stranger.post(path + "0", json=lease_body(claimed)).status_code == 409
    assert finish(agent, claimed).json()["status"] == "succeeded"
    assert agent.post(path + "0", json=lease_body(claimed)).status_code == 409
    assert agent.post(f"/api/worker/jobs/{job['id']}/finish", json={**lease_body(claimed), "status": "failed", "elapsed_ms": 1,
                                                                    "error_code": "input_verification_failed"}).status_code == 200
