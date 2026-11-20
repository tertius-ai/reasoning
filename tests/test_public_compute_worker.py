"""Durable-store/HTTP-client fixtures without a socket or CAS execution."""

import hashlib
import json
import threading
from dataclasses import dataclass

import httpx
import pytest
from public_schema_fixture import remove_collaboration_schema

from reasoning_ledger.server.compute_archive import CollectedArtifact
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_docker import CleanupUncertain, Outcome
from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.compute_worker import (
    ComputeWorker,
    Heartbeat,
    WorkerClient,
    WorkerFault,
    enrollment,
    main,
    read_private,
    write_state,
)
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.security import private_file
from reasoning_ledger.server.store import Store, StoreError

IMAGE = {
    "id": "fixture",
    "name": "Nonexecuting protocol fixture",
    "reference": "registry.example/cpu@sha256:" + "a" * 64,
    "languages": ["python"],
    "source_revision": "b" * 40,
    "capabilities_digest": "sha256:" + "c" * 64,
    "sbom_digest": "sha256:" + "d" * 64,
    "capabilities_path": "/etc/research/capabilities.json",
    "sbom_path": "/usr/share/research/sbom.json",
    "maximum_resources": DEFAULT_RESOURCES,
}


@dataclass
class Fixture:
    state: ComputeStore
    user: str
    org: str
    topic: str
    registration: dict
    config: dict

    def enqueue(self, client="first"):
        params = self.state.parameters("fixture", "python", "print('nonexecuting fixture')", DEFAULT_RESOURCES)
        return self.state.enqueue(self.user, self.org, self.topic, client, params)


@pytest.fixture
def fixture(tmp_path):
    store = Store(tmp_path / "server")
    owner = store.bootstrap((store.data_dir / "setup-token").read_text(), "owner@example.org", "Owner", "fixture password long", "Fixture")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Project")
    channel = store.create_entity(user, org, "channels", "Channel", project["id"])
    topic = store.create_entity(user, org, "topics", "Compute", channel["id"], "gamma")
    state = ComputeStore(store, {"fixture": ComputeImage.parse(IMAGE)})
    state.set_policy(user, org, ["fixture"], 10000, 2)
    record = state.register_worker(user, org, "Fixture", ["fixture"], DEFAULT_RESOURCES)
    return Fixture(state, user, org, topic["id"], record, enrollment("https://reasoning.example", record))


class StoreTransport:
    def __init__(self, fixture):
        self.fixture, self.calls, self.lose = fixture, [], None

    def handle(self, request):
        path, body = request.url.path, json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        token = request.headers["authorization"].removeprefix("Bearer ")
        state, code = self.fixture.state, 200
        try:
            if path == "/api/worker/identity":
                value = state.worker_identity(token)
            elif path == "/api/worker/status":
                value = state.worker_status(token)
            elif path == "/api/worker/drain":
                value = state.drain_self(token)
            elif path == "/api/worker/retirement":
                value = state.worker_retirement(token) if request.method == "GET" else state.retire_worker(token, body["cleanup_confirmed"])
            elif path == "/api/worker/claim":
                value = state.claim(token, body.get("claim_id"), supports_workflow_inputs=body.get("supports_workflow_inputs", False),
                                    supports_attachments=body.get("supports_attachments", False))
            else:
                parts = path.split("/")
                job, verb = parts[4], parts[5]
                args = (token, job, body["attempt_id"], body["lease"])
                if verb == "attachments" and len(parts) == 9 and parts[7] == "chunks":
                    value = state.attachment_chunk(*args, parts[6], int(parts[8]))
                elif verb == "heartbeat":
                    value = state.heartbeat(*args)
                elif verb == "logs":
                    state.append_log(*args, body["chunk_index"], body["text"])
                    value, code = None, 204
                elif verb == "finish":
                    value = state.finish(*args, body["status"], body["elapsed_ms"], body["exit_code"], body["error_code"])
                elif verb == "artifacts" and len(parts) == 6:
                    value = state.begin_artifact(*args, body["name"], body["size_bytes"], body["digest"])
                    code = 201
                elif parts[-1] == "chunks":
                    state.artifact_chunk(*args, parts[6], body["chunk_index"], body["content_base64"])
                    value, code = None, 204
                else:
                    assert parts[-1] == "commit"
                    value = state.commit_artifact(*args, parts[6])
            if self.lose is not None and request.method != "GET" and path.endswith(self.lose):
                self.lose = None
                raise httpx.ReadError("synthetic lost response", request=request)
            return httpx.Response(code, json=value) if code != 204 else httpx.Response(204)
        except StoreError as exc:
            return httpx.Response(exc.status, json={"detail": str(exc)})


class FixtureRunner:
    def __init__(self, *, uncertain=False):
        self.calls, self.cleanups, self.uncertain = 0, 0, uncertain

    def verify_runtime_identity(self):
        pass

    def cleanup(self):
        self.cleanups += 1
        if self.uncertain:
            raise CleanupUncertain("cleanup_uncertain")

    def verify_cleanup(self):
        if self.uncertain:
            raise CleanupUncertain("cleanup_uncertain")

    def run(self, job, spool, log, stopped):
        assert not stopped()
        self.calls += 1
        log(b"protocol fixture log\n")
        body = b"protocol fixture artifact\n" * 1500
        path = spool / "0"
        private_file(path, body)
        artifact = CollectedArtifact("output.txt", path, len(body), "sha256:" + hashlib.sha256(body).hexdigest())
        return Outcome("succeeded", 1500, 0, None, (artifact,))


def worker(tmp_path, fixture, *, runner=None, wire=None):
    runner, wire = runner or FixtureRunner(), wire or StoreTransport(fixture)
    client = WorkerClient(fixture.config, transport=httpx.MockTransport(wire.handle))
    return ComputeWorker(tmp_path / "worker", fixture.config, client, runner), runner, wire


def test_durable_worker_delivers_logs_multichunk_artifacts_and_settles_once(fixture, tmp_path):
    job = fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    agent.run(once=True)
    result = fixture.state.job(fixture.user, fixture.org, job["id"])
    assert result["status"] == "succeeded" and result["used_cpu_seconds"] == 2
    assert result["log_bytes"] == len(b"protocol fixture log\n")
    artifacts = fixture.state.artifacts(fixture.user, fixture.org, job["id"])
    meta, chunks = fixture.state.download(fixture.user, fixture.org, job["id"], artifacts[0]["id"])
    assert b"".join(fixture.state.download_chunks(meta, chunks)) == b"protocol fixture artifact\n" * 1500
    assert runner.calls == runner.cleanups == 1 and agent.state() == {"phase": "idle"}
    assert list(agent.attempts.iterdir()) == [] and len(artifacts) == 1
    assert len([row for row in wire.calls if row[1].endswith("/finish")]) == 1


@pytest.mark.parametrize("lost", ["/claim", "/logs", "/artifacts", "/chunks", "/commit", "/finish"])
def test_lost_responses_retry_delivery_without_executing_twice(fixture, tmp_path, lost):
    job = fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    wire.lose = lost
    with pytest.raises(WorkerFault, match="server_unavailable"):
        agent.run(once=True)
    saved = agent.state()
    assert saved["phase"] == ("claiming" if lost == "/claim" else "finishing" if lost == "/finish" else "delivering")
    restarted = ComputeWorker(agent.data_dir, fixture.config, agent.client, runner)
    restarted.run(once=True)
    assert runner.calls == 1
    assert fixture.state.job(fixture.user, fixture.org, job["id"])["status"] == "succeeded"
    assert restarted.state()["phase"] == "idle"
    assert len(fixture.state.artifacts(fixture.user, fixture.org, job["id"])) == 1


def test_expired_attempt_delivery_resumes_but_execution_does_not(fixture, tmp_path):
    job = fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    wire.lose = "/chunks"
    with pytest.raises(WorkerFault):
        agent.run(once=True)
    with fixture.state.store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (job["id"],))
    assert fixture.state.job(fixture.user, fixture.org, job["id"])["status"] == "outcome_unknown"
    agent.run(once=True)
    assert runner.calls == 1 and fixture.state.job(fixture.user, fixture.org, job["id"])["status"] == "succeeded"


@pytest.mark.parametrize("phase,usage", [("claimed", 0), ("executing", 120)])
def test_crashed_started_attempt_is_cleaned_then_failed_never_rerun(fixture, tmp_path, phase, usage):
    original = fixture.enqueue()
    job = fixture.state.claim(fixture.registration["token"], "a" * 32)
    agent, runner, _ = worker(tmp_path, fixture)
    write_state(agent.journal, {"phase": phase, "job": job})
    agent.run(once=True)
    result = fixture.state.job(fixture.user, fixture.org, original["id"])
    assert runner.calls == 0 and runner.cleanups == 1
    assert result["status"] == "failed" and result["error_code"] == "worker_stopped" and result["used_cpu_seconds"] == usage


def test_cleanup_failure_preserves_journal_and_quota_without_terminal_report(fixture, tmp_path):
    job = fixture.enqueue()
    claim = fixture.state.claim(fixture.registration["token"], "a" * 32)
    agent, runner, wire = worker(tmp_path, fixture, runner=FixtureRunner(uncertain=True))
    write_state(agent.journal, {"phase": "executing", "job": claim})
    with pytest.raises(CleanupUncertain):
        agent.run(once=True)
    assert agent.state()["phase"] == "executing" and runner.calls == 0 and wire.calls == []
    assert fixture.state.job(fixture.user, fixture.org, job["id"])["accounted_at"] is None


def test_worker_ownership_excludes_second_process_state(fixture, tmp_path):
    agent, _, _ = worker(tmp_path, fixture)
    second = ComputeWorker(agent.data_dir, fixture.config, agent.client, agent.runner)
    with agent.ownership(), pytest.raises(WorkerFault, match="another_worker_owns_state"), second.ownership():
        pytest.fail("duplicate worker acquired state")


def test_idempotent_claim_replays_immutable_attempt_even_when_draining_or_settled(fixture):
    original = fixture.enqueue()
    token, claim_id = fixture.registration["token"], "e" * 32
    first = fixture.state.claim(token, claim_id)
    fixture.state.change_worker(fixture.user, fixture.org, fixture.registration["id"], draining=True)
    assert fixture.state.claim(token, claim_id) == first
    assert fixture.state.claim(token, "f" * 32) is None
    fixture.state.finish(token, first["id"], first["attempt_id"], first["lease"], "succeeded", 1000, 0, None)
    replay = fixture.state.claim(token, claim_id)
    assert replay["id"] == original["id"] and replay["status"] == "succeeded" and replay["lease"] == first["lease"]
    assert not {"claim_id", "lease", "lease_hash", "attempt_id"}.intersection(fixture.state.job(fixture.user, fixture.org, original["id"]))


def test_claim_replay_is_worker_scoped_and_cannot_bypass_revocation(fixture):
    fixture.enqueue()
    first = fixture.state.claim(fixture.registration["token"], "e" * 32)
    other = fixture.state.register_worker(fixture.user, fixture.org, "Other", ["fixture"], DEFAULT_RESOURCES)
    assert fixture.state.claim(other["token"], "e" * 32) is None
    fixture.state.change_worker(fixture.user, fixture.org, fixture.registration["id"], revoke=True)
    with pytest.raises(StoreError) as error:
        fixture.state.claim(fixture.registration["token"], "e" * 32)
    assert error.value.status == 401 and first["worker_id"] == fixture.registration["id"]


def test_schema_four_upgrades_without_losing_existing_job_and_random_lease(fixture):
    fixture.enqueue()
    claim = fixture.state.claim(fixture.registration["token"])
    with fixture.state.store.transaction(write=True) as db:
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
            "invitation_mail",
            "host_setup_receipt",
            "compute_scaler_requests",
            "compute_enrollments",
            "compute_scalers",
            "bot_requests",
            "bot_topics",
            "bots",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP INDEX invitation_org_identity")
        db.execute("ALTER TABLE messages DROP COLUMN sender_kind")
        db.execute("ALTER TABLE users DROP COLUMN account_kind")
        db.execute("DROP INDEX compute_worker_retirement")
        db.execute("ALTER TABLE compute_workers DROP COLUMN retirement_id")
        db.execute("ALTER TABLE compute_workers DROP COLUMN retired_at")
        db.execute("DROP INDEX compute_worker_claim")
        db.execute("ALTER TABLE compute_jobs DROP COLUMN claim_id")
        db.execute("PRAGMA user_version=4")
        db.execute("DROP TABLE model_impact_profiles")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("DROP TABLE project_memberships")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
        db.execute("DROP INDEX message_org_identity")
    migrated = Store(fixture.state.store.data_dir)
    state = ComputeStore(migrated, {"fixture": ComputeImage.parse(IMAGE)})
    result = state.finish(fixture.registration["token"], claim["id"], claim["attempt_id"], claim["lease"], "succeeded", 1000, 0, None)
    assert result["status"] == "succeeded"
    with migrated.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:secret@example.org",
        "https://example.org/path",
        "https://example.org?key=secret",
        "https://example.org#fragment",
        "https://example.org:99999",
    ],
)
def test_enrollment_rejects_nonlocal_http_and_nonorigin_urls(fixture, url):
    with pytest.raises(WorkerFault, match="invalid_worker_enrollment"):
        enrollment(url, fixture.registration)


def test_cli_configures_once_with_private_enrollment_and_no_token_output(fixture, tmp_path, capsys):
    path = tmp_path / "registration.json"
    private_file(path, json.dumps(fixture.registration).encode())
    args = [
        "--data-dir",
        str(tmp_path / "configured"),
        "configure",
        "--server-url",
        fixture.config["server_url"],
        "--registration",
        str(path),
    ]
    assert main(args) == main(args) == 0
    output = capsys.readouterr()
    assert fixture.registration["token"] not in output.out + output.err
    saved = tmp_path / "configured/enrollment.json"
    assert saved.stat().st_mode & 0o777 == 0o600 and json.loads(read_private(saved, 65536)) == fixture.config


def test_private_state_refuses_symlink_exposed_file_and_oversized_input(tmp_path):
    path = tmp_path / "state.json"
    private_file(path, b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_private(link, 10)
    path.chmod(0o644)
    with pytest.raises(WorkerFault):
        write_state(path, {"phase": "idle"})
    path.chmod(0o600)
    with pytest.raises(WorkerFault):
        read_private(path, 1)


@pytest.mark.parametrize("status", [302, 401, 409, 500])
def test_client_never_follows_redirects_or_exposes_remote_secret_errors(fixture, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.example"}, json={"detail": fixture.registration["token"]})

    client = WorkerClient(fixture.config, transport=httpx.MockTransport(handler))
    with pytest.raises(WorkerFault) as error:
        client.request("POST", "/api/worker/claim", {})
    assert len(requests) == 1 and fixture.registration["token"] not in str(error.value)


def test_client_caps_response_and_does_not_send_host_environment_credentials(fixture, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://secret-proxy.example")

    def handler(request):
        assert set(request.headers).isdisjoint({"cookie", "proxy-authorization"})
        return httpx.Response(200, content=b" " * (256 * 1024 + 1))

    client = WorkerClient(fixture.config, transport=httpx.MockTransport(handler))
    with pytest.raises(WorkerFault, match="invalid_server_response"):
        client.request("POST", "/api/worker/claim", {})


@pytest.mark.parametrize("change", ["cancel", "policy", "expiry"])
def test_recovered_claim_never_executes_after_stop_or_expiry(fixture, tmp_path, change):
    original = fixture.enqueue()
    claim_id = "f" * 32
    fixture.state.claim(fixture.registration["token"], claim_id)
    if change == "cancel":
        fixture.state.cancel(fixture.user, fixture.org, original["id"])
    elif change == "policy":
        fixture.state.set_policy(fixture.user, fixture.org, [], 0, 2)
    else:
        with fixture.state.store.transaction(write=True) as db:
            db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (original["id"],))
    agent, runner, _ = worker(tmp_path, fixture)
    write_state(agent.journal, {"phase": "claiming", "claim_id": claim_id})
    agent.run(once=True)
    result = fixture.state.job(fixture.user, fixture.org, original["id"])
    assert runner.calls == 0 and result["status"] == "cancelled" and result["used_cpu_seconds"] == 0


def test_claim_replay_from_concurrent_requests_preserves_one_attempt_and_reservation(fixture):
    from concurrent.futures import ThreadPoolExecutor

    original = fixture.enqueue()

    def claim(_index):
        return fixture.state.claim(fixture.registration["token"], "e" * 32)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(8)))
    assert len({row["attempt_id"] for row in results}) == len({row["lease"] for row in results}) == 1
    assert all(row["id"] == original["id"] for row in results)
    balance = fixture.state.configuration(fixture.user, fixture.org)["balance"]
    assert balance["held_cpu_seconds"] == 120


@pytest.mark.parametrize(
    "field,value", [("file", "../../secret"), ("name", "../secret"), ("size_bytes", True), ("size_bytes", 2**30), ("digest", "mutable")]
)
def test_corrupt_delivery_manifest_fails_before_upload_without_host_paths(fixture, tmp_path, field, value):
    fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    wire.lose = "/logs"
    with pytest.raises(WorkerFault):
        agent.run(once=True)
    state = agent.state()
    state["artifacts"][0][field] = value
    write_state(agent.journal, state)
    before = len(wire.calls)
    with pytest.raises(WorkerFault, match="invalid_private_state"):
        agent.run(once=True)
    assert len(wire.calls) == before and runner.calls == 1


def test_changed_private_spool_bytes_are_refused_before_artifact_registration(fixture, tmp_path):
    fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    wire.lose = "/logs"
    with pytest.raises(WorkerFault):
        agent.run(once=True)
    spool = agent._spool(agent.state()["job"]) / "artifacts/0"
    original = spool.read_bytes()
    spool.write_bytes(b"x" * len(original))
    with pytest.raises(WorkerFault, match="invalid_private_state"):
        agent.run(once=True)
    assert not any(path.endswith("/artifacts") for _, path, _ in wire.calls) and runner.calls == 1


def test_settled_cleanup_is_resumable_after_partial_file_removal(fixture, tmp_path, monkeypatch):
    original = fixture.enqueue()
    agent, runner, _ = worker(tmp_path, fixture)

    def interrupted_forget(state):
        (agent._spool(state["job"]) / "artifacts/0").unlink()
        raise WorkerFault("server_unavailable")

    monkeypatch.setattr(agent, "_forget", interrupted_forget)
    with pytest.raises(WorkerFault):
        agent.run(once=True)
    assert agent.state()["phase"] == "settled"
    restarted = ComputeWorker(agent.data_dir, fixture.config, agent.client, runner)
    restarted.run(once=True)
    assert restarted.state() == {"phase": "idle"} and runner.calls == 1
    assert fixture.state.job(fixture.user, fixture.org, original["id"])["status"] == "succeeded"


def test_cancel_during_retained_delivery_cannot_become_success(fixture, tmp_path):
    original = fixture.enqueue()
    agent, runner, wire = worker(tmp_path, fixture)
    wire.lose = "/chunks"
    with pytest.raises(WorkerFault):
        agent.run(once=True)
    fixture.state.cancel(fixture.user, fixture.org, original["id"])
    agent.run(once=True)
    assert runner.calls == 1 and fixture.state.job(fixture.user, fixture.org, original["id"])["status"] == "cancelled"


@pytest.mark.parametrize("response", [{"stop": False, "status": []}, {"stop": False}, {"stop": 0, "status": "running"}, []])
def test_malformed_heartbeat_fails_closed_even_with_unhashable_fields(fixture, response):
    class InvalidClient:
        def request(self, *_args):
            return response

    fixture.enqueue()
    job = fixture.state.claim(fixture.registration["token"])
    guard = Heartbeat(InvalidClient(), job, threading.Event())
    with pytest.raises(WorkerFault, match="invalid_server_response"):
        guard.tick()
    assert guard.stopped() and guard.reason == "lease_lost"


def test_heartbeat_local_deadline_cannot_be_extended_by_blocked_network_thread(fixture, monkeypatch):
    import reasoning_ledger.server.compute_worker as module

    fixture.enqueue()
    job = fixture.state.claim(fixture.registration["token"])
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    guard = Heartbeat(None, job, threading.Event())
    assert not guard.stopped()
    now[0] = 120.0
    assert guard.stopped() and guard.reason == "lease_lost"


def data_file(fixture, body, name="values.bin"):
    import base64

    from reasoning_ledger.server.research_media import CHUNK_BYTES, DATA_FILE, ResearchMedia

    media = ResearchMedia(fixture.state.store)
    start = media.begin(fixture.user, fixture.org, fixture.topic, name, DATA_FILE, len(body), hashlib.sha256(body).hexdigest())
    for offset in range(0, len(body), CHUNK_BYTES):
        encoded = base64.b64encode(body[offset : offset + CHUNK_BYTES]).decode()
        media.chunk(fixture.user, fixture.org, fixture.topic, start["id"], offset // CHUNK_BYTES, encoded)
    return media.finish(fixture.user, fixture.org, fixture.topic, start["id"])


def enqueue_with(fixture, *files):
    params = fixture.state.parameters("fixture", "python", "print(open('/work/in/01-values.bin').read())", DEFAULT_RESOURCES,
                                      [{"identity": file["id"], "sha256": file["sha256"]} for file in files])
    return fixture.state.enqueue(fixture.user, fixture.org, fixture.topic, "inputs", params)


class InputRunner(FixtureRunner):
    def run(self, job, spool, log, stopped):
        self.received = []
        for item, path in zip(job["parameters"]["attachments"], job["attachment_files"], strict=True):
            with open(path, "rb") as stream:
                self.received.append((item["path"], stream.read()))
        return super().run(job, spool, log, stopped)


def test_worker_downloads_and_verifies_pinned_inputs_before_running(fixture, tmp_path):
    first, second = bytes(range(256)) * 160, b"second file"
    files = [data_file(fixture, first), data_file(fixture, second, "notes.txt")]
    job = enqueue_with(fixture, *files)
    agent, runner, wire = worker(tmp_path, fixture, runner=InputRunner())
    agent.run(once=True)
    assert runner.received == [("01-values.bin", first), ("02-notes.txt", second)]
    claim = next(body for _, path, body in wire.calls if path == "/api/worker/claim")
    assert claim["supports_attachments"] is True and claim["supports_workflow_inputs"] is True
    chunks = [path for _, path, _ in wire.calls if "/attachments/" in path]
    assert chunks == [f"/api/worker/jobs/{job['id']}/attachments/{files[0]['id']}/chunks/{index}" for index in range(2)] + [
        f"/api/worker/jobs/{job['id']}/attachments/{files[1]['id']}/chunks/0"]
    assert fixture.state.job(fixture.user, fixture.org, job["id"])["status"] == "succeeded"
    assert list(agent.attempts.iterdir()) == [] and agent.state() == {"phase": "idle"}


def test_worker_fails_without_running_when_input_bytes_differ_from_their_checksum(fixture, tmp_path):
    import base64

    file = data_file(fixture, b"pinned bytes")
    job = enqueue_with(fixture, file)
    with fixture.state.store.transaction(write=True) as db:
        changed = base64.b64encode(b"PINNED BYTES").decode()
        db.execute("UPDATE research_media_chunks SET data_base64=? WHERE media_id=?", (changed, file["id"]))
    agent, runner, _ = worker(tmp_path, fixture)
    agent.run(once=True)
    result = fixture.state.job(fixture.user, fixture.org, job["id"])
    assert runner.calls == 0
    assert result["status"] == "failed" and result["error_code"] == "input_verification_failed" and result["used_cpu_seconds"] == 0
    assert list(agent.attempts.iterdir()) == [] and agent.state() == {"phase": "idle"}


def test_worker_refuses_a_claim_whose_pinned_inputs_exceed_its_bounds(fixture, tmp_path):
    job = enqueue_with(fixture, data_file(fixture, b"bounded"))
    claimed = fixture.state.claim(fixture.registration["token"], supports_attachments=True)
    agent, _, _ = worker(tmp_path, fixture)
    agent.validate_job(claimed)
    pinned = claimed["parameters"]["attachments"][0]
    for change in ({"path": "../escape"}, {"size_bytes": 0}, {"sha256": "x"}, {"extra": 1}):
        altered = {**claimed, "parameters": {**claimed["parameters"], "attachments": [{**pinned, **change}]}}
        with pytest.raises(WorkerFault, match="invalid_server_job"):
            agent.validate_job(altered)
    assert job["id"] == claimed["id"]
