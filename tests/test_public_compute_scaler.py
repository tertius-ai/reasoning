"""Real SQLite/ASGI scoped enrollment, with synthetic HTTP and no cloud/CAS execution."""

import asyncio
import json
import secrets
import sqlite3
import time

import httpx
import pytest
from public_schema_fixture import remove_collaboration_schema
from test_public_compute_ui import IMAGE, ORIGIN, headers
from test_public_compute_ui import fixture as fixture

from reasoning_ledger.deployment import enrollment as module
from reasoning_ledger.deployment.cli import main
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import read_document, read_private, write_document, write_private
from reasoning_ledger.deployment.scaling import read_snapshot, validate_snapshot
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_scaler import ComputeScalers
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.security import digest
from reasoning_ledger.server.store import Store, StoreError


def grant(fixture, **options):
    _, app, owner, _, _ = fixture
    return app.state.compute_scalers.create(
        owner["user_id"],
        owner["organization_id"],
        "Bound scaler",
        [IMAGE["id"]],
        options.pop("resources", DEFAULT_RESOURCES),
        options.pop("maximum_workers", 2),
        **options,
    )


def request(nonce=None, **options):
    return {
        "request_id": nonce or secrets.token_hex(16),
        "name": "Recoverable worker",
        "image_ids": [IMAGE["id"]],
        "resources": dict(DEFAULT_RESOURCES),
        "worker_token": "rnw_" + secrets.token_urlsafe(32),
        **options,
    }


def enroll(fixture, credential, body):
    return fixture[1].state.compute_scalers.enroll(
        credential,
        body["request_id"],
        body["name"],
        body["image_ids"],
        body["resources"],
        body["worker_token"],
    )


def bridge(app, observe=None):
    """Synchronous client HTTP transport into same-thread real ASGI/SQLite."""

    def send(req):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                return await client.request(req.method, str(req.url), headers=req.headers, content=req.content)

        response = asyncio.run(run())
        if observe:
            observe(req, response)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    return httpx.MockTransport(send)


def client(fixture, credential, observe=None):
    return module.ScalerClient(ORIGIN, credential, transport=bridge(fixture[1], observe))


def operator(tmp_path, fixture):
    return {
        "directory": tmp_path / "enrollment",
        "origin": ORIGIN,
        "organization": fixture[2]["organization_id"],
        "name": "Recoverable worker",
        "image_ids": [IMAGE["id"]],
        "limits": DEFAULT_RESOURCES,
        "registration_output": tmp_path / "worker.json",
        "catalog_output": tmp_path / "catalog.json",
    }


def test_scoped_api_separates_admin_chat_worker_bot_and_provider_key_access(fixture):
    store, app, owner, topics, root = fixture
    scoped = grant(fixture)
    store.add_key(owner["user_id"], owner["organization_id"], "openrouter", "Private", "provider-secret")
    compute = app.state.compute
    job = compute.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[2],
        "private-program",
        compute.parameters(IMAGE["id"], "sage", "# private-program-secret", DEFAULT_RESOURCES),
    )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"authorization": "Bearer " + scoped["token"]}
        ) as remote:
            identity = await remote.get("/api/compute-scaler/identity")
            assert identity.status_code == 200 and identity.json()["images"] == [IMAGE]
            snapshot = await remote.get("/api/compute-scaler/snapshot")
            assert snapshot.status_code == 200 and snapshot.json()["jobs"][0]["id"] == job["id"]
            assert "private-program-secret" not in snapshot.text and "provider-secret" not in snapshot.text
            for path in (
                root + "/compute-configuration",
                root + "/compute-scalers",
                root + "/compute-workers",
                root + "/compute-jobs/" + job["id"],
                root + "/provider-keys",
                "/api/worker/identity",
            ):
                assert (await remote.get(path)).status_code == 401
            assert (await remote.post("/api/worker/claim", json={})).status_code == 401
            assert (await remote.get("/api/bot/identity")).status_code == 401
            body = request()
            response = await remote.post("/api/compute-scaler/enrollments", json=body)
            assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
            assert body["worker_token"] not in response.text and "token_hash" not in response.text
            credential = {"authorization": "Bearer " + body["worker_token"]}
            claimed = await remote.post("/api/worker/claim", headers=credential, json={})
            assert claimed.status_code == 200 and claimed.json()["parameters"]["program"] == "# private-program-secret"
            # Enrollment deliberately delegates trusted approved-image execution;
            # the new worker can see its assigned program, not general chat/keys.
            assert (await remote.get("/api/compute-scaler/identity", headers=credential)).status_code == 401
            assert (await remote.get("/api/compute-scaler/identity", headers=headers(owner))).status_code == 401
            remote.headers.pop("authorization")
            remote.cookies.set("__Host-reasoning_session", scoped["token"])
            assert (await remote.get("/api/compute-scaler/identity")).status_code == 401

    asyncio.run(check())


def test_enrollment_replay_stores_hashes_and_is_exact_without_worker_mutation(fixture):
    store, app, owner, _, _ = fixture
    scoped, body = grant(fixture), request()
    first = enroll(fixture, scoped["token"], body)
    assert enroll(fixture, scoped["token"], body) == first
    assert first["worker"]["last_seen_at"] is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM compute_enrollments").fetchone()[0] == 1
        serialized = repr([tuple(row) for row in db.execute("SELECT * FROM compute_enrollments")])
        assert body["worker_token"] not in serialized and digest(body["worker_token"]) in serialized
        assert db.execute("SELECT token_hash FROM compute_scalers").fetchone()[0] == digest(scoped["token"])
    app.state.compute_scalers.snapshot(scoped["token"])
    app.state.compute_scalers.identity(scoped["token"])
    assert app.state.compute.workers(owner["user_id"], owner["organization_id"])[0]["last_seen_at"] is None


@pytest.mark.parametrize("change", ["name", "image_ids", "resources", "worker_token"])
def test_same_nonce_refuses_changed_request(fixture, change):
    scoped, body = grant(fixture), request()
    enroll(fixture, scoped["token"], body)
    if change == "name":
        body[change] = "Changed"
    elif change == "image_ids":
        other = {**IMAGE, "id": "second-fixture"}
        fixture[1].state.compute.images[other["id"]] = ComputeImage.parse(other)
        body[change] = [other["id"]]
    elif change == "resources":
        body[change] = {**DEFAULT_RESOURCES, "wall_time_seconds": 60}
    else:
        body[change] = "rnw_" + secrets.token_urlsafe(32)
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], body)
    assert failure.value.status == 409


def test_same_worker_credential_cannot_create_second_enrollment_even_under_other_grant(fixture):
    first, second, body = grant(fixture), grant(fixture), request()
    enroll(fixture, first["token"], body)
    for credential in (first["token"], second["token"]):
        with pytest.raises(StoreError) as failure:
            enroll(fixture, credential, {**body, "request_id": secrets.token_hex(16)})
        assert failure.value.status == 409


@pytest.mark.parametrize("method", ["revoke", "retire"])
def test_original_revoked_or_retired_worker_nonce_is_never_replaced(fixture, method):
    scoped, body = grant(fixture), request()
    worker = enroll(fixture, scoped["token"], body)["worker"]
    store, app, owner, _, _ = fixture
    if method == "revoke":
        app.state.compute.change_worker(owner["user_id"], owner["organization_id"], worker["id"], revoke=True)
    else:
        app.state.compute.drain_self(body["worker_token"])
        app.state.compute.retire_worker(body["worker_token"], True)
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], body)
    assert failure.value.status == 409
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 1


@pytest.mark.parametrize("busy", ["running", "outcome_unknown"])
def test_revoked_still_busy_worker_consumes_grant_ceiling(fixture, busy):
    scoped, body = grant(fixture, maximum_workers=1), request()
    worker = enroll(fixture, scoped["token"], body)["worker"]
    store, app, owner, topics, _ = fixture
    compute = app.state.compute
    job = compute.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[2],
        "busy",
        compute.parameters(IMAGE["id"], "sage", "# fixture", DEFAULT_RESOURCES),
    )
    compute.claim(body["worker_token"])
    compute.change_worker(owner["user_id"], owner["organization_id"], worker["id"], revoke=True)
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET status=?,lease_expires_at=0 WHERE id=?", (busy, job["id"]))
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 429
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET status='cancelled' WHERE id=?", (job["id"],))
    assert enroll(fixture, scoped["token"], request())["worker"]["id"] != worker["id"]


def test_grant_at_its_ceiling_with_only_draining_workers_offers_no_capacity(fixture):
    # The grant enrolled its full ceiling and every one of those workers drained: nothing claims a job, and the grant
    # can't enroll a replacement. The organization must not advertise compute or accept a job nothing would run.
    store, app, owner, _, _ = fixture
    compute, org, user = app.state.compute, owner["organization_id"], owner["user_id"]
    scoped = grant(fixture, maximum_workers=2)
    workers = [enroll(fixture, scoped["token"], request())["worker"] for _ in range(2)]
    with store.transaction() as db:
        assert compute.available(db, org)
    for worker in workers:
        compute.change_worker(user, org, worker["id"], draining=True)
    with store.transaction() as db:
        assert compute.capacity(db, org) == [] and not compute.available(db, org)
        assert not compute.runnable(db, org, IMAGE["id"], dict(DEFAULT_RESOURCES))
        with pytest.raises(StoreError, match="no compute worker or scaler"):
            compute.require_runnable(db, org, IMAGE["id"], dict(DEFAULT_RESOURCES))
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 429
    # Revoking an idle drained worker frees its seat: the grant can enroll again, so it counts as capacity again.
    compute.change_worker(user, org, workers[0]["id"], revoke=True)
    with store.transaction() as db:
        assert compute.available(db, org) and compute.runnable(db, org, IMAGE["id"], dict(DEFAULT_RESOURCES))
    assert enroll(fixture, scoped["token"], request())["worker"]["id"] not in {worker["id"] for worker in workers}


def test_grant_below_its_ceiling_counts_as_capacity_before_any_worker_exists(fixture):
    store, app, owner, _, _ = fixture
    grant(fixture, maximum_workers=1)
    with store.transaction() as db:
        assert app.state.compute.capacity(db, owner["organization_id"]) == [
            {"image_ids": [IMAGE["id"]], "resources": dict(DEFAULT_RESOURCES)}
        ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_workers", True),
        ("maximum_workers", 0),
        ("maximum_workers", 65),
        ("lifetime", True),
        ("lifetime", 3599),
        ("lifetime", 31536001),
    ],
)
def test_grant_bounds(fixture, field, value):
    with pytest.raises(StoreError) as failure:
        grant(fixture, **{field: value})
    assert failure.value.status == 400


def test_resource_grant_and_current_policy_catalog_gate_new_enrollment(fixture):
    store, app, owner, _, _ = fixture
    scoped = grant(fixture, resources={**DEFAULT_RESOURCES, "wall_time_seconds": 60})
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 403
    body = request(resources={**DEFAULT_RESOURCES, "wall_time_seconds": 60})
    app.state.compute.set_policy(owner["user_id"], owner["organization_id"], [], 10000, 2)
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], body)
    assert failure.value.status == 409
    app.state.compute.set_policy(owner["user_id"], owner["organization_id"], [IMAGE["id"]], 10000, 2)
    app.state.compute.images[IMAGE["id"]] = ComputeImage.parse(
        {**IMAGE, "maximum_resources": {**DEFAULT_RESOURCES, "wall_time_seconds": 30}}
    )
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], body)
    assert failure.value.status == 409
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 0


def test_rotation_retains_exact_request_namespace_and_revocation_is_irreversible(fixture):
    scoped, body = grant(fixture), request()
    receipt = enroll(fixture, scoped["token"], body)
    store, app, owner, _, _ = fixture
    scalers = app.state.compute_scalers
    rotated = scalers.rotate(owner["user_id"], owner["organization_id"], scoped["id"], 3600)
    assert rotated["id"] == scoped["id"] and rotated["token"] != scoped["token"]
    with pytest.raises(StoreError) as failure:
        scalers.identity(scoped["token"])
    assert failure.value.status == 401
    assert enroll(fixture, rotated["token"], body) == receipt
    assert "token" not in scalers.list(owner["user_id"], owner["organization_id"])[0]
    scalers.revoke(owner["user_id"], owner["organization_id"], scoped["id"])
    scalers.revoke(owner["user_id"], owner["organization_id"], scoped["id"])
    with pytest.raises(StoreError) as failure:
        scalers.rotate(owner["user_id"], owner["organization_id"], scoped["id"])
    assert failure.value.status == 409
    assert app.state.compute.worker_identity(body["worker_token"])["id"] == receipt["worker"]["id"]
    # Revoking automation is not proof of remote worker termination/revocation.
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_enrollments").fetchone()[0] == 1


@pytest.mark.parametrize("disabled", ["expiry", "demotion", "removed", "bot"])
def test_current_grant_and_human_sponsor_required(fixture, disabled):
    scoped = grant(fixture)
    store, app, owner, _, _ = fixture
    with store.transaction(write=True) as db:
        if disabled == "expiry":
            db.execute("UPDATE compute_scalers SET expires_at=0")
        elif disabled == "demotion":
            db.execute("UPDATE memberships SET role='member' WHERE user_id=?", (owner["user_id"],))
        elif disabled == "removed":
            db.execute("DELETE FROM memberships WHERE user_id=?", (owner["user_id"],))
        else:
            db.execute("UPDATE users SET account_kind='bot' WHERE id=?", (owner["user_id"],))
    for operation in (app.state.compute_scalers.identity, app.state.compute_scalers.snapshot):
        with pytest.raises(StoreError) as failure:
            operation(scoped["token"])
        assert failure.value.status == 401
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 401


def test_grant_ceiling_rate_limit_and_failed_attempts_are_counted(fixture):
    store, app, owner, _, _ = fixture
    scoped = grant(fixture)
    for _ in range(7):
        grant(fixture)
    with pytest.raises(StoreError) as failure:
        grant(fixture)
    assert failure.value.status == 429
    body = request(worker_token="invalid")
    for _ in range(30):
        with pytest.raises(StoreError) as failure:
            enroll(fixture, scoped["token"], body)
        assert failure.value.status == 400
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 429
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_scaler_requests SET attempted_at=0")
    assert enroll(fixture, scoped["token"], request())["worker"]["organization_id"] == owner["organization_id"]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_scaler_requests").fetchone()[0] == 1
    app.state.compute_scalers.revoke(owner["user_id"], owner["organization_id"], scoped["id"])
    grant(fixture)


def test_schema_seven_migrates_without_changing_existing_worker_and_checks_tenant_links(fixture):
    store, app, owner, _, _ = fixture
    worker = app.state.compute.register_worker(owner["user_id"], owner["organization_id"], "Legacy", [IMAGE["id"]], DEFAULT_RESOURCES)
    with store.transaction(write=True) as db:
        remove_collaboration_schema(db)
        for table in (
            "knowledge_writes",
            "knowledge_versions",
            "knowledge_entries",
            "user_presence",
            "bot_webhook_requests",
            "bot_webhook_deliveries",
            "bot_webhooks",
            "workflow_run_actions",
            "workflow_run_events",
            "workflow_node_attempts",
            "workflow_runs",
            "workflow_writes",
            "workflow_versions",
            "workflows",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DROP TABLE invitation_mail")
        db.execute("DROP INDEX invitation_org_identity")
        for name in ("host_setup_receipt", "compute_scaler_requests", "compute_enrollments", "compute_scalers"):
            db.execute("DROP TABLE " + name)
        db.execute("PRAGMA user_version=7")
        db.execute("DROP TABLE model_impact_profiles")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("DROP TABLE project_memberships")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
        db.execute("DROP INDEX message_org_identity")
    recovered = Store(store.data_dir)
    scalers = ComputeScalers(app.state.compute)
    scoped = grant(fixture)
    with recovered.transaction(write=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT token_hash FROM compute_workers WHERE id=?", (worker["id"],)).fetchone()[0] == digest(worker["token"])
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO compute_enrollments VALUES(?,?,?,?,?,?)", ("f" * 32, scoped["id"], "a" * 32, worker["id"], "{}", 1))
    assert scalers.identity(scoped["token"])["organization_id"] == owner["organization_id"]


def test_expired_rotation_cannot_bypass_eight_current_grant_ceiling(fixture):
    scoped = grant(fixture)
    store, app, owner, _, _ = fixture
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_scalers SET expires_at=0 WHERE id=?", (scoped["id"],))
    for _ in range(8):
        grant(fixture)
    with pytest.raises(StoreError) as failure:
        app.state.compute_scalers.rotate(owner["user_id"], owner["organization_id"], scoped["id"])
    assert failure.value.status == 429


def test_sponsor_revoked_between_rate_check_and_enrollment_never_registers(fixture, monkeypatch):
    scoped = grant(fixture)
    store, app, owner, _, _ = fixture
    rate = app.state.compute_scalers._rate

    def revoke(credential):
        rate(credential)
        app.state.compute_scalers.revoke(owner["user_id"], owner["organization_id"], scoped["id"])

    monkeypatch.setattr(app.state.compute_scalers, "_rate", revoke)
    with pytest.raises(StoreError) as failure:
        enroll(fixture, scoped["token"], request())
    assert failure.value.status == 401
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 0


def test_grant_does_not_depend_on_sponsoring_human_login_session(fixture):
    scoped = grant(fixture)
    with fixture[0].transaction(write=True) as db:
        db.execute("DELETE FROM sessions")
    assert fixture[1].state.compute_scalers.identity(scoped["token"])["enabled"]
    assert enroll(fixture, scoped["token"], request())["worker"]["organization_id"] == fixture[2]["organization_id"]


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_scaler_admin_api_denies_non_admins_and_other_tenants(fixture, role):
    store, app, owner, _, root = fixture
    scoped = grant(fixture)
    invite = store.invite(owner["user_id"], owner["organization_id"], role + "@example.org", role, 3600)
    member = store.accept_invitation(invite["token"], user_id=None, name=role, password="long fixture password")
    other = store.create_organization(owner["user_id"], "Other")

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers(member)) as remote:
            for method, path, body in (
                ("GET", root + "/compute-scalers", None),
                (
                    "POST",
                    root + "/compute-scalers",
                    {"name": "Grant", "image_ids": [IMAGE["id"]], "resources": DEFAULT_RESOURCES, "maximum_workers": 1},
                ),
                ("POST", root + "/compute-scalers/" + scoped["id"] + "/rotate", {}),
                ("DELETE", root + "/compute-scalers/" + scoped["id"], None),
            ):
                assert (await remote.request(method, path, json=body)).status_code == 403
            remote.headers.update(headers(owner))
            path = "/api/organizations/" + other["id"] + "/compute-scalers/" + scoped["id"]
            assert (await remote.delete(path)).status_code == 404
            assert (await remote.post(path + "/rotate", json={})).status_code == 404

    asyncio.run(check())


def test_lost_post_response_recovers_same_credential_and_worker_before_any_cloud_creation(fixture, tmp_path):
    scoped = grant(fixture)
    parameters = operator(tmp_path, fixture)
    count = []

    def lose(req, response):
        if req.method == "POST":
            original = read_document(parameters["directory"] / module.INTENT)
            assert json.loads(req.content)["worker_token"] == original["worker_token"]
            assert response.status_code == 201
            count.append(response.json()["worker"]["id"])
            if len(count) == 1:
                raise httpx.ReadError("secret provider body must not leak", request=req)

    remote = client(fixture, scoped["token"], lose)
    try:
        with pytest.raises(InstallError, match="resume the original private intent") as failure:
            module.enroll_worker(remote, **parameters)
        assert "secret provider body" not in str(failure.value)
        assert not parameters["registration_output"].exists()
        original = read_private(parameters["directory"] / module.INTENT)
        result = module.enroll_worker(remote, **parameters)
        assert count == [result["worker_id"]] * 2 and not result["creates_cloud_resources"]
        assert read_private(parameters["directory"] / module.INTENT) == original
        registration = read_document(parameters["registration_output"])
        assert registration["token"] == json.loads(original)["worker_token"]
        assert json.loads(read_private(parameters["catalog_output"])) == [IMAGE]
        assert fixture[1].state.compute.worker_identity(registration["token"])["id"] == result["worker_id"]
        assert scoped["token"].encode() not in original and "token" not in result
        assert all(path.stat().st_mode & 0o077 == 0 for path in parameters["directory"].iterdir())
    finally:
        remote.close()


def test_catalog_output_failure_resumes_original_receipt_without_overwriting_registration(fixture, tmp_path, monkeypatch):
    scoped, parameters = grant(fixture), operator(tmp_path, fixture)
    remote = client(fixture, scoped["token"])
    original = module.write_private

    def fail(path, body):
        if path == parameters["catalog_output"]:
            raise OSError("fixture catalog disk failure")
        return original(path, body)

    monkeypatch.setattr(module, "write_private", fail)
    try:
        with pytest.raises(OSError):
            module.enroll_worker(remote, **parameters)
        saved = read_private(parameters["registration_output"])
        registration = json.loads(saved)
        fixture[1].state.compute.worker_identity(registration["token"])
        monkeypatch.setattr(module, "write_private", original)
        result = module.enroll_worker(remote, **parameters)
        assert result["worker_id"] == registration["id"] and read_private(parameters["registration_output"]) == saved
        assert json.loads(read_private(parameters["catalog_output"])) == [IMAGE]
    finally:
        remote.close()


@pytest.mark.parametrize("changed", ["name", "organization", "output", "catalog", "grant", "revoked-worker", "existing-output"])
def test_client_changed_inputs_or_identity_never_overwrite_or_replace(fixture, tmp_path, changed):
    scoped, parameters = grant(fixture), operator(tmp_path, fixture)
    remote = client(fixture, scoped["token"])
    try:
        result = module.enroll_worker(remote, **parameters)
        saved = read_private(parameters["registration_output"])
        if changed == "name":
            parameters["name"] = "Different"
        elif changed == "organization":
            parameters["organization"] = "f" * 32
        elif changed == "output":
            parameters["registration_output"] = tmp_path / "different.json"
        elif changed == "catalog":
            fixture[1].state.compute.images[IMAGE["id"]] = ComputeImage.parse(
                {**IMAGE, "reference": IMAGE["reference"].replace("a" * 64, "f" * 64)}
            )
        elif changed == "grant":
            remote.close()
            remote = client(fixture, grant(fixture)["token"])
        elif changed == "revoked-worker":
            fixture[1].state.compute.change_worker(fixture[2]["user_id"], fixture[2]["organization_id"], result["worker_id"], revoke=True)
        else:
            parameters["registration_output"].write_bytes(b"different private data")
            saved = b"different private data"
        with pytest.raises(InstallError):
            module.enroll_worker(remote, **parameters)
        assert read_private(tmp_path / "worker.json") == saved
        with fixture[0].transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 1
    finally:
        remote.close()


def test_client_rotation_after_lost_reply_keeps_grant_and_nonce(fixture, tmp_path):
    scoped, parameters = grant(fixture), operator(tmp_path, fixture)

    def lose(req, _response):
        if req.method == "POST":
            raise httpx.ReadError("lost", request=req)

    remote = client(fixture, scoped["token"], lose)
    with pytest.raises(InstallError):
        module.enroll_worker(remote, **parameters)
    remote.close()
    rotated = fixture[1].state.compute_scalers.rotate(fixture[2]["user_id"], fixture[2]["organization_id"], scoped["id"])
    remote = client(fixture, rotated["token"])
    try:
        assert module.enroll_worker(remote, **parameters)["scaler_id"] == scoped["id"]
        with fixture[0].transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM compute_workers").fetchone()[0] == 1
    finally:
        remote.close()


@pytest.mark.parametrize("mode", ["redirect", "unauthorized", "oversized", "duplicate", "malformed"])
def test_client_bounded_fixed_origin_no_redirect_or_secret_error_echo(mode):
    calls = []

    def send(req):
        calls.append(req)
        assert req.method == "GET" and req.url.path == "/api/compute-scaler/identity" and not req.content
        if mode == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.example"}, text="secret")
        if mode == "unauthorized":
            return httpx.Response(401, text="secret")
        if mode == "oversized":
            return httpx.Response(200, content=b"x" * (256 * 1024 + 1))
        if mode == "duplicate":
            return httpx.Response(200, content=b'{"token":"secret","token":"second"}')
        return httpx.Response(200, content=b"secret invalid JSON")

    remote = module.ScalerClient(ORIGIN, "rns_" + "a" * 43, transport=httpx.MockTransport(send))
    try:
        with pytest.raises(InstallError) as failure:
            remote.request("identity")
        assert "secret" not in str(failure.value) and len(calls) == 1
    finally:
        remote.close()


def test_cli_enrollment_uses_private_files_outputs_only_metadata_and_closes_client(fixture, tmp_path, monkeypatch, capsys):
    scoped, parameters = grant(fixture), operator(tmp_path, fixture)
    token, limits = tmp_path / "scaler-token", tmp_path / "limits.json"
    write_private(token, scoped["token"].encode())
    write_document(limits, dict(DEFAULT_RESOURCES), new=True)
    remote = client(fixture, scoped["token"])
    monkeypatch.setattr(module, "ScalerClient", lambda origin, credential: remote)
    args = [
        "worker-enroll",
        "--server-url",
        ORIGIN,
        "--organization-id",
        parameters["organization"],
        "--scaler-token-file",
        str(token),
        "--name",
        parameters["name"],
        "--image-id",
        IMAGE["id"],
        "--resources-file",
        str(limits),
        "--state-dir",
        str(parameters["directory"]),
        "--registration-output",
        str(parameters["registration_output"]),
        "--catalog-output",
        str(parameters["catalog_output"]),
    ]
    assert main(args) == 0 and remote.client.is_closed
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err and not result["creates_cloud_resources"]
    assert scoped["token"] not in output.out and read_document(parameters["registration_output"])["token"] not in output.out


def test_scoped_snapshot_client_uses_fixed_path_and_binds_returned_tenant(fixture):
    scoped = grant(fixture)
    paths = []
    snapshot = read_snapshot(
        ORIGIN, fixture[2]["organization_id"], scoped["token"], transport=bridge(fixture[1], lambda req, _: paths.append(req.url.path))
    )
    assert paths == ["/api/compute-scaler/snapshot"]
    validate_snapshot(snapshot, fixture[2]["organization_id"], int(time.time()))
    with pytest.raises(InstallError):
        validate_snapshot(snapshot, "f" * 32, int(time.time()))


def test_admin_grant_http_create_rotate_and_cookie_csrf_follow_existing_security(fixture):
    _, app, owner, _, root = fixture
    body = {"name": "HTTP grant", "image_ids": [IMAGE["id"]], "resources": DEFAULT_RESOURCES, "maximum_workers": 2}

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as remote:
            remote.cookies.set("__Host-reasoning_session", owner["access_token"])
            assert (await remote.post(root + "/compute-scalers", json=body)).status_code == 403
            csrf = {"origin": ORIGIN, "x-csrf-token": owner["csrf_token"]}
            response = await remote.post(root + "/compute-scalers", headers=csrf, json=body)
            assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
            scoped = response.json()
            assert scoped["enabled"] and scoped["token"].startswith("rns_")
            metadata = (await remote.get(root + "/compute-scalers")).json()
            assert "token" not in metadata[0] and "token_hash" not in metadata[0]
            assert (await remote.post(root + "/compute-scalers/" + scoped["id"] + "/rotate", json={})).status_code == 403
            rotated = await remote.post(root + "/compute-scalers/" + scoped["id"] + "/rotate", headers=csrf, json={})
            assert rotated.status_code == 200 and rotated.json()["id"] == scoped["id"]
            assert rotated.json()["token"] != scoped["token"]
            assert (await remote.delete(root + "/compute-scalers/" + scoped["id"], headers=csrf)).status_code == 204

    asyncio.run(check())


def test_enrollment_validation_never_echoes_client_secrets_or_accepts_tenant_override(fixture):
    scoped = grant(fixture)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fixture[1]), base_url=ORIGIN, headers={"authorization": "Bearer " + scoped["token"]}
        ) as remote:
            for changes in (
                {"worker_token": "private-invalid-secret"},
                {"resources": {**DEFAULT_RESOURCES, "cpu": True}},
                {"organization_id": "f" * 32},
                {"request_id": "invalid"},
            ):
                body = request(**changes)
                reply = await remote.post("/api/compute-scaler/enrollments", json=body)
                assert reply.status_code == 422
                assert body["worker_token"] not in reply.text and scoped["token"] not in reply.text

    asyncio.run(check())


def test_orphan_receipt_and_existing_outputs_refuse_before_any_enrollment_request(fixture, tmp_path):
    parameters = operator(tmp_path, fixture)
    parameters["directory"].mkdir(mode=0o700)
    write_document(parameters["directory"] / module.RECEIPT, {"orphan": "preserve"}, new=True)

    class NoRequest:
        def request(self, *_args, **_kwargs):
            pytest.fail("orphan ownership must fail before contacting the server")

    with pytest.raises(InstallError, match="without its original intent"):
        module.enroll_worker(NoRequest(), **parameters)
    assert not (parameters["directory"] / module.INTENT).exists()


def test_exclusive_private_byte_outputs_reject_replacement_and_keep_private_modes(tmp_path):
    path = tmp_path / "private-catalog.json"
    write_private(path, b"[]\n")
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(InstallError, match="already exists"):
        write_private(path, b"replacement")
    assert read_private(path) == b"[]\n"
