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
    return config, store, app.state.compute, client, owner, topics


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
