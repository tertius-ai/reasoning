"""Real SQLite and child-queue protocols, not Docker execution or live providers."""

import asyncio
import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass

import pytest
from public_schema_fixture import remove_collaboration_schema
from test_public_compute_worker import IMAGE

from reasoning_ledger.server.backup import backup, create_key, restore
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.openrouter import ProviderError, Quote, Usage
from reasoning_ledger.server.store import Store, StoreError
from reasoning_ledger.server.workflow_inputs import input_bytes, read_inputs
from reasoning_ledger.server.workflow_runs import WorkflowRuns
from reasoning_ledger.server.workflow_store import WorkflowStore
from reasoning_ledger.server.workflow_worker import WorkflowWorker

QUOTE = Quote("fixture/model", "0.000001", "0.000002", "0", 200000, 8192)


def compute_node(node_id="compute", dependencies=None, inputs=None):
    return {
        "id": node_id,
        "kind": "compute",
        "depends_on": dependencies or [],
        "inputs": inputs or {},
        "parameters": {"image_id": "fixture", "language": "python", "program": "print(4)", "resources": dict(DEFAULT_RESOURCES)},
    }


def approval_node(dependencies=None):
    return {
        "id": "approve",
        "kind": "approval",
        "depends_on": dependencies or [],
        "inputs": {},
        "parameters": {"message": "Review the result", "required_role": "admin"},
    }


def llm_node(dependencies=None, inputs=None):
    return {
        "id": "reason",
        "kind": "llm",
        "depends_on": dependencies or [],
        "inputs": inputs or {},
        "parameters": {"model": QUOTE.model, "prompt": "Explain the result", "max_completion_tokens": 128},
    }


@dataclass
class Installation:
    store: Store
    definitions: WorkflowStore
    runs: WorkflowRuns
    compute: ComputeStore
    inference: InferenceStore
    user: str
    org: str
    topic: str
    worker: dict

    def start(self, nodes, client="workflow-run-fixture-001"):
        saved = self.definitions.create(
            self.user, self.org, self.topic, client + "-definition", {"format": 1, "name": "Fixture", "nodes": nodes}
        )
        return self.runs.start(self.user, self.org, saved["workflow_id"], saved["version"], client)

    def view(self, run):
        return self.runs.get(self.user, self.org, run["id"])

    def claim(self):
        return self.compute.claim(self.worker["token"], supports_workflow_inputs=True)

    def finish(self, child, status="succeeded"):
        return self.compute.finish(
            self.worker["token"], child["id"], child["attempt_id"], child["lease"], status, 1000, 0 if status == "succeeded" else 1, None
        )


@pytest.fixture
def installation(tmp_path):
    store = Store(tmp_path / "state")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "workflow fixture password", "Fixture")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Project")
    channel = store.create_entity(user, org, "channels", "Channel", project["id"])
    topic = store.create_entity(user, org, "topics", "Workflow", channel["id"], "gamma")["id"]
    compute = ComputeStore(store, {"fixture": ComputeImage.parse(IMAGE)})
    compute.set_policy(user, org, ["fixture"], 10000, 2)
    worker = compute.register_worker(user, org, "Worker", ["fixture"], DEFAULT_RESOURCES)
    store.add_key(user, org, "openrouter", "Fixture", "secret-never-in-run-state")
    store.set_policy(user, org, [QUOTE.model], 10000000)
    definitions, inference = WorkflowStore(store), InferenceStore(store)
    runs = WorkflowRuns(definitions, compute, inference)
    return Installation(store, definitions, runs, compute, inference, user, org, topic, worker)


def nodes(view):
    return {node["node_id"]: node for node in view["nodes"]}


def uncertain_model_workflow(env, client):
    run = env.start([llm_node()], client=client)
    action = env.runs.advance(run["id"])
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    child = env.inference.claim()
    env.inference.finish(child, "interrupted", error="worker_interrupted", accounting_unknown=True)
    env.runs.advance(run["id"])
    return env.view(run), child


def test_administrator_releases_stranded_capacity_without_resolving_accounting_or_replaying(installation):
    env = installation
    retained = [uncertain_model_workflow(env, f"uncertain-workflow-{index}") for index in range(8)]
    assert env.runs.capacity(env.user, env.org)["in_use"] == 8
    with pytest.raises(StoreError, match="Workflow capacity"):
        env.start([llm_node()], client="blocked-retained-start")
    before = env.inference.balance(env.user, env.org)
    run, child = retained[0]
    receipt = env.runs.release_capacity(env.user, env.org, run["id"], run["revision"], "release-capacity-fixture")
    assert env.runs.release_capacity(env.user, env.org, run["id"], run["revision"], "release-capacity-fixture") == receipt
    after = env.view(run)
    assert after["capacity_released"] == after["cancel_requested"] == 1
    assert after["status"] == "outcome_unknown" and after["finished_at"] is None
    assert receipt["uncertainty_retained"] is True
    assert env.inference.balance(env.user, env.org) == before
    with env.store.transaction() as db:
        assert db.execute("SELECT accounting_state FROM llm_runs WHERE id=?", (child["id"],)).fetchone()[0] == "uncertain"
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 8
    assert env.runs.capacity(env.user, env.org)["in_use"] == 7
    admitted = env.start([llm_node()], client="blocked-retained-start")
    assert env.start([llm_node()], client="blocked-retained-start")["id"] == admitted["id"]
    assert env.runs.capacity(env.user, env.org)["in_use"] == 8
    with pytest.raises(StoreError, match="current failed or cancelled"):
        env.runs.retry(env.user, env.org, run["id"], after["revision"], "no-uncertain-replay")


def test_capacity_release_refuses_stale_revisions_and_nonadministrators(installation):
    env = installation
    run, _ = uncertain_model_workflow(env, "uncertain-authority-fixture")
    with pytest.raises(StoreError, match="current uncertain"):
        env.runs.release_capacity(env.user, env.org, run["id"], 1, "stale-capacity-fixture")
    invitation = env.store.invite(env.user, env.org, "member@example.org", "member", 3600)
    member = env.store.accept_invitation(invitation["token"], user_id=None, name="Member", password="member fixture password")["user_id"]
    with pytest.raises(StoreError) as denied:
        env.runs.release_capacity(member, env.org, run["id"], run["revision"], "member-capacity-fixture")
    assert denied.value.status == 403
    assert env.runs.capacity(env.user, env.org)["in_use"] == 1


def test_capacity_release_refuses_a_workflow_with_another_active_child(installation):
    env = installation
    second = llm_node()
    second["id"] = "another"
    run = env.start([llm_node(), second], client="mixed-uncertain-active")
    for _ in range(2):
        action = env.runs.advance(run["id"])
        env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    child = env.inference.claim()
    env.inference.finish(child, "interrupted", accounting_unknown=True, error="worker_interrupted")
    env.runs.advance(run["id"])
    current = env.view(run)
    assert current["status"] == "outcome_unknown"
    with pytest.raises(StoreError, match="stop active"):
        env.runs.release_capacity(env.user, env.org, run["id"], current["revision"], "reject-active-release")
    assert env.runs.capacity(env.user, env.org)["in_use"] == 1


def test_restored_uncertain_work_can_release_its_slot_without_lifting_the_restore_fence(installation, tmp_path):
    env = installation
    run, _ = uncertain_model_workflow(env, "restored-capacity-fixture")
    key, archive, target = tmp_path / "key", tmp_path / "copy.rnbk", tmp_path / "restored"
    create_key(key)
    result = backup(env.store.data_dir, archive, key)
    restore(archive, key, result["sha256"], target, source_stopped=True)
    store = Store(target)
    state = WorkflowRuns(WorkflowStore(store), ComputeStore(store, {}), InferenceStore(store))
    current = state.get(env.user, env.org, run["id"])
    assert current["restore_fenced"] == 1
    state.release_capacity(env.user, env.org, run["id"], current["revision"], "retire-restored-capacity")
    after = state.get(env.user, env.org, run["id"])
    assert after["restore_fenced"] == 1 and after["capacity_released"] == 1
    assert state.advance(run["id"]) is None


def test_comparison_history_retains_full_question_and_models_without_a_model_whitelist(installation):
    env = installation
    question = "Full retained comparison question\n" + "Explain every step. " * 90
    first, second = llm_node(), llm_node()
    first["parameters"].update(prompt=question, model="new/provider-model")
    second["id"] = "alternative"
    second["parameters"].update(prompt=question, model="openrouter/auto")
    env.store.set_policy(env.user, env.org, [], 10000000)
    run = env.start([first, second])
    summary = env.runs.list(env.user, env.org, env.topic)[0]
    assert summary["comparison"]["question"] == question
    assert set(summary["comparison"]["models"]) == {"new/provider-model", "openrouter/auto"}
    assert all(node["parameters"]["prompt"] == question for node in env.view(run)["definition"]["nodes"])


def test_noncomparison_history_has_no_misleading_compare_question(installation):
    env = installation
    env.start([llm_node()])
    assert env.runs.list(env.user, env.org, env.topic)[0]["comparison"] is None


def test_bot_workflow_dependency_uses_local_receipts_without_openrouter_key(installation):
    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.bot_store import BotStore

    env = installation
    bots = BotStore(env.store)
    bot = bots.create(env.user, env.org, "Codex", [env.topic], ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    with env.store.transaction(write=True) as db:
        db.execute("UPDATE provider_keys SET disabled_at=1")
        db.execute("UPDATE organizations SET model_policy_json='[]'")
    env.runs.inference_enabled = False
    first, second = llm_node(), llm_node(["first"], {"previous": {"node": "first", "output": "text"}})
    first["id"], second["id"] = "first", "second"
    for node in (first, second):
        node["parameters"]["model"] = f"bot/{bot['id']}/default"
    run = env.start([first, second])
    assert env.runs.advance(run["id"]) is None
    claimed = runtime.claim(bot["token"])
    assert claimed and "Explain" in claimed["prompt"]
    runtime.finish(bot["token"], claimed["id"], claimed["lease"], "succeeded", "First result")
    env.runs.advance(run["id"])
    second_run = runtime.claim(bot["token"])
    assert "First result" in second_run["prompt"]
    runtime.finish(bot["token"], second_run["id"], second_run["lease"], "succeeded", "Second result")
    env.runs.advance(run["id"])
    assert env.view(run)["status"] == "succeeded"
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0


def test_compute_approval_llm_compute_with_frozen_inputs_and_no_duplicate_dispatch(installation):
    env = installation
    run = env.start(
        [
            compute_node(),
            approval_node(["compute"]),
            llm_node(
                ["compute", "approve"], {"log": {"node": "compute", "output": "logs"}, "review": {"node": "approve", "output": "decision"}}
            ),
            compute_node("final", ["reason"], {"answer": {"node": "reason", "output": "text"}}),
        ]
    )
    assert env.runs.advance(run["id"]) is None
    assert env.compute.claim(env.worker["token"]) is None  # old workers cannot discard workflow inputs
    first = env.claim()
    assert first["parameters"]["inputs"] == {}
    env.compute.append_log(env.worker["token"], first["id"], first["attempt_id"], first["lease"], 0, "4\n")
    env.finish(first)
    assert env.runs.advance(run["id"]) is None
    assert env.view(run)["status"] == "awaiting_approval"
    receipt = env.runs.approve(env.user, env.org, run["id"], "approve", 1, "approve", "approval-request-001")
    assert env.runs.approve(env.user, env.org, run["id"], "approve", 1, "approve", "approval-request-001") == receipt
    action = env.runs.advance(run["id"])
    assert action["node_id"] == "reason"
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    llm = env.inference.claim()
    context = json.loads(llm["context_json"])
    assert context[1]["content"] == "Explain the result"
    captured = env.runs.inputs(env.user, env.org, run["id"], "reason", 1)
    assert captured["inputs"]["log"] == "4\n"
    assert captured["inputs"]["review"]["approved_by"] == env.user
    assert "secret-never-in-run-state" not in json.dumps(env.view(run))
    assert env.inference.progress(llm, "Four — inert $(touch /tmp/nope)", "fixture-generation")
    assert env.inference.finish(llm, "succeeded", usage=Usage(100, 20, 10))
    env.runs.advance(run["id"])
    final = env.claim()
    assert final["parameters"]["program"] == "print(4)"
    assert final["parameters"]["inputs"] == {"answer": "Four — inert $(touch /tmp/nope)"}
    env.finish(final)
    # A new store and reconciler see exactly the same linked children after restart.
    recovered = WorkflowRuns(WorkflowStore(Store(env.store.data_dir)), env.compute, env.inference)
    for _ in range(3):
        recovered.advance(run["id"])
    assert env.view(run)["status"] == "succeeded"
    assert env.inference.balance(env.user, env.org)["spent_microusd"] == 100
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 1
    events = env.runs.events(env.user, env.org, run["id"])
    assert events[-1]["event"] == {"type": "state", "status": "succeeded"}
    assert env.runs.events(env.user, env.org, run["id"], events[-1]["sequence"]) == []


def test_child_link_and_budget_reservation_roll_back_together(installation, monkeypatch):
    env = installation
    run = env.start([compute_node()])
    original = env.runs._change

    def fail_link(*args, **kwargs):
        if kwargs.get("compute_job_id"):
            raise sqlite3.OperationalError("synthetic link failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(env.runs, "_change", fail_link)
    with pytest.raises(sqlite3.OperationalError):
        env.runs.advance(run["id"])
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM compute_events").fetchone()[0] == 0
    assert nodes(env.view(run))["compute"]["status"] == "pending"
    monkeypatch.setattr(env.runs, "_change", original)
    env.runs.advance(run["id"])
    assert env.claim() is not None


def test_rejected_approval_blocks_descendants_and_retry_keeps_successful_work(installation):
    env = installation
    run = env.start([compute_node(), approval_node(["compute"]), llm_node(["approve"])])
    env.runs.advance(run["id"])
    first = env.claim()
    env.finish(first)
    env.runs.advance(run["id"])
    env.runs.approve(env.user, env.org, run["id"], "approve", 1, "reject", "approval-reject-001")
    env.runs.advance(run["id"])
    failed = env.view(run)
    assert failed["status"] == "failed"
    assert nodes(failed)["reason"]["status"] == "blocked"
    receipt = env.runs.retry(env.user, env.org, run["id"], failed["revision"], "retry-request-001")
    assert env.runs.retry(env.user, env.org, run["id"], failed["revision"], "retry-request-001") == receipt
    env.runs.advance(run["id"])
    current = nodes(env.view(run))
    assert current["compute"]["attempt"] == 1 and current["compute"]["compute_job_id"] == first["id"]
    assert current["approve"]["attempt"] == 2 and current["approve"]["status"] == "awaiting_approval"
    assert len(env.runs.attempts(env.user, env.org, run["id"], "approve")) == 2
    with pytest.raises(StoreError, match="waiting current attempt"):
        env.runs.approve(env.user, env.org, run["id"], "approve", 1, "approve", "stale-approval-001")


def test_cancel_queued_child_and_replay_is_stable(installation):
    env = installation
    run = env.start([compute_node(), approval_node(["compute"])])
    env.runs.advance(run["id"])
    receipt = env.runs.cancel(env.user, env.org, run["id"], "cancel-request-001")
    env.runs.advance(run["id"])
    assert env.view(run)["status"] == "cancelled"
    assert env.runs.cancel(env.user, env.org, run["id"], "cancel-request-001") == receipt
    assert env.claim() is None
    with env.store.transaction() as db:
        child = dict(db.execute("SELECT * FROM compute_jobs").fetchone())
        assert child["status"] == "cancelled" and child["accounted_at"] is not None


def test_uncertain_inference_is_not_replayed_before_reconciliation(installation):
    env = installation
    run = env.start([llm_node()])
    action = env.runs.advance(run["id"])
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    child = env.inference.claim()
    env.inference.recover()
    env.runs.advance(run["id"])
    view = env.view(run)
    assert view["status"] == "outcome_unknown"
    with pytest.raises(StoreError):
        env.runs.retry(env.user, env.org, run["id"], view["revision"], "retry-unknown-001")
    for _ in range(3):
        assert env.runs.advance(run["id"]) is None
    assert env.inference.claim() is None
    env.inference.reconcile(env.user, env.org, child["id"], 200)
    env.runs.advance(run["id"])
    view = env.view(run)
    assert view["status"] == "failed"
    env.runs.retry(env.user, env.org, run["id"], view["revision"], "retry-settled-001")
    action = env.runs.advance(run["id"])
    assert action["attempt"] == 2
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    assert env.inference.claim()["id"] != child["id"]


def test_restore_fences_workflow_and_queued_child(installation, tmp_path):
    env = installation
    run = env.start([compute_node(), approval_node(["compute"])])
    env.runs.advance(run["id"])
    key, archive, target = tmp_path / "backup-key", tmp_path / "backup.rnbk", tmp_path / "restored"
    create_key(key)
    result = backup(env.store.data_dir, archive, key)
    report = restore(archive, key, result["sha256"], target, source_stopped=True)
    assert report["workflow_runs_held"] == 1
    store = Store(target)
    compute = ComputeStore(store, {"fixture": ComputeImage.parse(IMAGE)})
    runs = WorkflowRuns(WorkflowStore(store), compute, InferenceStore(store))
    for _ in range(3):
        assert runs.advance(run["id"]) is None
    restored = runs.get(env.user, env.org, run["id"])
    assert restored["restore_fenced"] == 1 and restored["status"] == "outcome_unknown"
    with pytest.raises(StoreError, match="restored workflow"):
        runs.retry(env.user, env.org, run["id"], restored["revision"], "retry-restored-001")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT typeof(event_json) FROM workflow_run_events WHERE json_extract(event_json,'$.type')='backup_restored'"
            ).fetchone()[0]
            == "text"
        )


def test_schema_eleven_preserves_definitions(installation):
    env = installation
    saved = env.definitions.create(
        env.user, env.org, env.topic, "definition-migrate-001", {"format": 1, "name": "Fixture", "nodes": [approval_node()]}
    )
    with env.store.transaction(write=True) as db:
        remove_collaboration_schema(db)
        for table in (
            "knowledge_writes",
            "knowledge_versions",
            "knowledge_entries",
            "user_presence",
            "model_impact_profiles",
            "bot_webhook_requests",
            "bot_webhook_deliveries",
            "bot_webhooks",
            "workflow_run_actions",
            "workflow_run_events",
            "workflow_node_attempts",
            "workflow_runs",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=11")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("DROP TABLE project_memberships")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
        db.execute("DROP INDEX message_org_identity")
    store = Store(env.store.data_dir)
    assert WorkflowStore(store).get(env.user, env.org, saved["workflow_id"]) == saved
    with store.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 0


def test_quote_worker_failure_and_restart_do_not_create_paid_child(installation):
    env = installation
    run = env.start([llm_node()])

    class Provider:
        async def quotes(self, models):
            raise ProviderError("fixture-unavailable", no_charge=True)

    worker = WorkflowWorker(env.runs, Provider())
    asyncio.run(worker.execute_once(run["id"]))
    assert env.view(run)["status"] == "failed"
    assert nodes(env.view(run))["reason"]["error_code"] == "quote_unavailable"
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [[], {"bad alias": 1}, {"x": float("nan")}, {"x": "x" * 65536}, {"x": [0] * 129}, {"x": "\ud800"}])
def test_input_bounds_reject_ambiguous_or_oversized_data(bad):
    with pytest.raises(ValueError):
        input_bytes(bad)


def test_input_encoding_is_canonical_and_not_source_interpolation():
    source = {"z": "$(touch /tmp/nope)", "a": {"unicode": "α"}}
    assert input_bytes(source) == b'{"a":{"unicode":"\xce\xb1"},"z":"$(touch /tmp/nope)"}'
    assert hashlib.sha256(input_bytes(source)).digest() == hashlib.sha256(input_bytes(copy.deepcopy(source))).digest()


def test_llm_child_link_and_reservation_roll_back_together(installation, monkeypatch):
    env = installation
    run = env.start([llm_node()])
    action = env.runs.advance(run["id"])
    original = env.runs._change

    def fail_link(*args, **kwargs):
        if kwargs.get("inference_run_id"):
            raise sqlite3.OperationalError("synthetic link failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(env.runs, "_change", fail_link)
    with pytest.raises(sqlite3.OperationalError):
        env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
    assert env.inference.balance(env.user, env.org)["reserved_microusd"] == 0
    monkeypatch.setattr(env.runs, "_change", original)
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    assert env.inference.claim() is not None


def test_changed_policy_between_quote_and_dispatch_refuses_without_charge(installation):
    env = installation
    run = env.start([llm_node()])
    action = env.runs.advance(run["id"])
    env.store.set_policy(env.user, env.org, [], 0)
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    assert env.view(run)["status"] == "failed"
    assert env.inference.balance(env.user, env.org)["reserved_microusd"] == 0
    assert env.inference.claim() is None


def test_stale_quote_after_cancel_cannot_dispatch(installation):
    env = installation
    run = env.start([llm_node()])
    action = env.runs.advance(run["id"])
    env.runs.cancel(env.user, env.org, run["id"], "cancel-before-quote-001")
    assert env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE]) is None
    assert env.view(run)["status"] == "cancelled"
    assert env.inference.claim() is None


def test_compute_unknown_requires_cleanup_reconciliation(installation):
    env = installation
    run = env.start([compute_node(), approval_node(["compute"])])
    env.runs.advance(run["id"])
    child = env.claim()
    with env.store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET lease_expires_at=0 WHERE id=?", (child["id"],))
    env.runs.advance(run["id"])
    view = env.view(run)
    assert view["status"] == "outcome_unknown"
    assert nodes(view)["approve"]["status"] == "pending"
    assert env.claim() is None
    with pytest.raises(StoreError):
        env.runs.retry(env.user, env.org, run["id"], view["revision"], "retry-lost-worker-001")
    env.compute.reconcile(env.user, env.org, child["id"], 1000, True)
    env.runs.advance(run["id"])
    assert env.view(run)["status"] == "failed"


def test_scope_roles_and_requester_revocation_cancel_existing_work(installation):
    env = installation
    run = env.start([approval_node()])
    env.runs.advance(run["id"])
    other = env.store.create_organization(env.user, "Other")["id"]
    with pytest.raises(StoreError) as failure:
        env.runs.get(env.user, other, run["id"])
    assert failure.value.status == 404
    invite = env.store.invite(env.user, env.org, "member@example.org", "member", 3600)
    member = env.store.accept_invitation(invite["token"], user_id=None, name="Member", password="member fixture password")["user_id"]
    assert env.runs.get(member, env.org, run["id"])["id"] == run["id"]
    with pytest.raises(StoreError, match="administrator"):
        env.runs.approve(member, env.org, run["id"], "approve", 1, "approve", "member-approval-001")
    with pytest.raises(StoreError):
        env.runs.cancel(member, env.org, run["id"], "member-cancel-001")
    saved = env.definitions.create(
        member, env.org, env.topic, "member-definition-001", {"format": 1, "name": "Member compute", "nodes": [compute_node()]}
    )
    owned = env.runs.start(member, env.org, saved["workflow_id"], 1, "member-run-request-001")
    env.runs.advance(owned["id"])
    env.store.change_member(env.user, env.org, member, None)
    env.runs.advance(owned["id"])
    assert env.view(owned)["status"] == "cancelled"
    assert env.claim() is None
    with pytest.raises(StoreError):
        env.runs.get(member, env.org, owned["id"])


def test_start_replay_conflicts_and_active_limit_also_applies_to_retry(installation):
    env = installation
    first = env.start([approval_node()])
    assert env.runs.start(env.user, env.org, first["workflow_id"], 1, first["client_id"])["id"] == first["id"]
    env.runs.cancel(env.user, env.org, first["id"], "cancel-for-limit-001")
    terminal = env.view(first)
    for index in range(8):
        env.runs.start(env.user, env.org, first["workflow_id"], 1, f"capacity-request-{index:03}")
    with pytest.raises(StoreError) as failure:
        env.runs.retry(env.user, env.org, first["id"], terminal["revision"], "retry-at-limit-001")
    assert failure.value.status == 429
    assert env.runs.start(env.user, env.org, first["workflow_id"], 1, first["client_id"])["status"] == "cancelled"
    with pytest.raises(StoreError) as failure:
        env.runs.start(env.user, env.org, first["workflow_id"], 2, first["client_id"])
    assert failure.value.status == 409


def test_oversized_output_fails_dependency_without_truncation(installation):
    env = installation
    run = env.start([llm_node(), compute_node("final", ["reason"], {"text": {"node": "reason", "output": "text"}})])
    action = env.runs.advance(run["id"])
    env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE])
    child = env.inference.claim()
    env.inference.progress(child, "α" * 40000, "fixture")
    env.inference.finish(child, "succeeded", usage=Usage(100, 20, 10))
    env.runs.advance(run["id"])
    view = env.view(run)
    assert view["status"] == "failed"
    assert nodes(view)["final"]["error_code"] == "input_limit"
    assert env.claim() is None


def test_captured_input_integrity_on_read_and_backup(installation, tmp_path):
    from reasoning_ledger.server.backup import BackupError

    env = installation
    run = env.start([approval_node()])
    env.runs.advance(run["id"])
    with env.store.transaction(write=True) as db:
        db.execute("UPDATE workflow_node_attempts SET inputs_json='{} ',inputs_digest=?", ("sha256:" + hashlib.sha256(b"{}").hexdigest(),))
    with pytest.raises(ValueError, match="input integrity"):
        env.runs.inputs(env.user, env.org, run["id"], "approve", 1)
    key = tmp_path / "key"
    create_key(key)
    with pytest.raises(BackupError, match="input integrity"):
        backup(env.store.data_dir, tmp_path / "invalid.rnbk", key)


def test_canonical_input_digest_rejects_changed_data():
    source = '{"answer":"four"}'
    digest = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    assert read_inputs(source, digest) == {"answer": "four"}
    with pytest.raises(ValueError, match="integrity"):
        read_inputs(source.replace("four", "five"), digest)


def test_worker_lock_is_exclusive_and_released_on_stop(installation):
    from reasoning_ledger.server.openrouter import OpenRouter

    first = WorkflowWorker(installation.runs, OpenRouter())
    second = WorkflowWorker(installation.runs, OpenRouter())
    try:
        first.start()
        assert first.running
        with pytest.raises(ValueError, match="another workflow worker"):
            second.start()
    finally:
        first.stop()
    second.start()
    second.stop()
    assert not first.running and not second.running


def test_stopping_during_quote_leaves_pending_without_reservation(installation):
    env = installation
    run = env.start([llm_node()])
    began = asyncio.Event()

    class Provider:
        async def quotes(self, models):
            began.set()
            await asyncio.Future()

    worker = WorkflowWorker(env.runs, Provider())

    async def check():
        task = asyncio.create_task(worker.execute_once(run["id"]))
        await began.wait()
        worker._stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(check())
    assert nodes(env.view(run))["reason"]["status"] == "pending"
    assert env.inference.balance(env.user, env.org)["reserved_microusd"] == 0


def test_beta_admission_and_shared_budget_still_work(installation):
    env = installation
    with env.store.transaction() as db:
        channel = db.execute("SELECT channel_id FROM topics WHERE id=?", (env.topic,)).fetchone()[0]
    beta = env.store.create_entity(env.user, env.org, "topics", "Beta", channel, "beta")["id"]
    env.store.post_message(env.user, env.org, beta, "Explain four", "beta-fixture-message")
    params = env.inference.parameters([QUOTE.model], 128, 1, None)
    children = env.inference.enqueue(env.user, env.org, beta, "beta-request", params, [QUOTE])
    assert env.inference.enqueue(env.user, env.org, beta, "beta-request", params, [QUOTE])[0]["id"] == children[0]["id"]
    with pytest.raises(StoreError, match="beta"):
        env.inference.enqueue(env.user, env.org, env.topic, "not-workflow", params, [QUOTE])
    child = env.inference.claim()
    assert any(message["role"] == "user" and "Explain four" in message["content"] for message in json.loads(child["context_json"]))
    env.inference.finish(child, "failed", no_charge=True, error="fixture-no-charge")
    assert env.inference.balance(env.user, env.org)["reserved_microusd"] == 0


def test_api_runs_approvals_inputs_history_and_csrf(installation, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing
    import httpx
    import starlette.concurrency

    from reasoning_ledger.server.api import Config, create_app
    from reasoning_ledger.server.bot_store import BotStore

    env = installation

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    for module in (fastapi.dependencies.utils, fastapi.routing, starlette.concurrency):
        monkeypatch.setattr(module, "run_in_threadpool", inline)
    origin = "https://chat.example.org"
    app = create_app(Config(env.store.data_dir, origin, inference_enabled=False))
    session = env.store.login("owner@example.org", "workflow fixture password", "fixture")
    saved = env.definitions.create(
        env.user, env.org, env.topic, "api-definition-001", {"format": 1, "name": "Approval", "nodes": [approval_node()]}
    )
    bot = BotStore(env.store).create(env.user, env.org, "Bot", [env.topic], ["messages:read"])
    prefix = "/api/organizations/" + env.org
    path = prefix + "/workflows/" + saved["workflow_id"] + "/runs"
    body = {"client_id": "api-run-request-001", "version": 1}

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            assert (await client.post(path, json=body)).status_code == 401
            client.headers["Authorization"] = "Bearer " + bot["token"]
            assert (await client.post(path, json=body)).status_code == 401
            client.headers["Authorization"] = "Bearer " + session["access_token"]
            assert (await client.post(path, json={**body, "version": True})).status_code == 422
            assert (await client.post(path, json={**body, "extra": "no"})).status_code == 422
            response = await client.post(path, json=body)
            assert response.status_code == 202
            run = response.json()
            resource = prefix + "/workflow-runs/" + run["id"]
            assert (await client.post(path, json=body)).json()["id"] == run["id"]
            app.state.workflow_runs.advance(run["id"])
            assert (await client.get(resource)).json()["status"] == "awaiting_approval"
            assert (await client.get(resource + "/nodes/approve/attempts/1/inputs")).json()["inputs"] == {}
            assert (await client.get(resource + "/nodes/approve/attempts")).json()[0]["attempt"] == 1
            assert (await client.get(prefix + "/topics/" + env.topic + "/workflow-runs")).json()[0]["id"] == run["id"]
            approval = {"client_id": "api-approval-request-001", "attempt": 1, "decision": "approve"}
            client.headers.pop("Authorization")
            client.cookies.set("__Host-reasoning_session", session["access_token"])
            assert (await client.post(resource + "/nodes/approve/approval", json=approval)).status_code == 403
            client.headers.update({"Origin": origin, "X-CSRF-Token": session["csrf_token"]})
            receipt = await client.post(resource + "/nodes/approve/approval", json=approval)
            assert receipt.status_code == 200
            assert (await client.post(resource + "/nodes/approve/approval", json=approval)).json() == receipt.json()
            app.state.workflow_runs.advance(run["id"])
            assert (await client.get(resource)).json()["status"] == "succeeded"
            assert (await client.get(resource + "/events")).json()[-1]["event"]["status"] == "succeeded"
            assert (await client.get(resource + "/events?after=-1")).status_code == 422
            configuration = (await client.get(prefix + "/workflow-configuration")).json()
            assert configuration == {"enabled": True, "worker_running": False, "inference_enabled": False}

    asyncio.run(check())


def test_app_lifespan_starts_and_stops_workflow_reconciler(installation):
    from reasoning_ledger.server.api import Config, create_app

    app = create_app(Config(installation.store.data_dir, "https://chat.example.org", inference_enabled=False))

    async def check():
        async with app.router.lifespan_context(app):
            assert app.state.workflow_worker.running
        assert not app.state.workflow_worker.running

    asyncio.run(check())


def test_stop_failure_still_stops_other_started_workers(installation, monkeypatch):
    from reasoning_ledger.server.api import Config, create_app

    app = create_app(Config(installation.store.data_dir, "https://chat.example.org"))
    stopped = []
    monkeypatch.setattr(app.state.inference_worker, "start", lambda: None)
    monkeypatch.setattr(app.state.workflow_worker, "start", lambda: None)
    monkeypatch.setattr(app.state.inference_worker, "stop", lambda: stopped.append("inference"))

    def fail():
        raise ValueError("synthetic workflow stop failure")

    monkeypatch.setattr(app.state.workflow_worker, "stop", fail)

    async def check():
        with pytest.raises(ValueError, match="stop failure"):
            async with app.router.lifespan_context(app):
                pass

    asyncio.run(check())
    assert stopped == ["inference"]


def test_actual_quote_and_inference_executors_publish_gamma_response(installation):
    from reasoning_ledger.server.inference_worker import InferenceWorker
    from reasoning_ledger.server.openrouter import Chunk

    env = installation
    run = env.start([approval_node(), llm_node(["approve"])])
    env.runs.advance(run["id"])
    env.runs.approve(env.user, env.org, run["id"], "approve", 1, "approve", "executor-approval-001")
    calls = []

    class Provider:
        async def quotes(self, models):
            calls.append(("quotes", models))
            return [QUOTE]

        async def stream(self, key, quote, messages, output_tokens):
            assert key == "secret-never-in-run-state"
            assert quote.model == QUOTE.model and quote.prompt == QUOTE.prompt and output_tokens == 128
            assert quote.routing == {"strategy": "recommended", "zdr": False, "region": "global"}
            assert messages[1]["content"] == "Explain the result"
            calls.append(("stream", quote.model))
            yield Chunk(text="Four.", generation_id="fixture-generation")
            yield Chunk(usage=Usage(125, 15, 5), finish_reason="stop")

    provider = Provider()
    asyncio.run(WorkflowWorker(env.runs, provider).execute_once(run["id"]))
    child = env.inference.claim()
    asyncio.run(InferenceWorker(env.inference, provider)._execute(child))
    env.runs.advance(run["id"])
    assert env.view(run)["status"] == "succeeded"
    messages = env.store.messages(env.user, env.org, env.topic)
    assert messages[-1]["author_kind"] == "assistant"
    assert messages[-1]["content"] == "Four."
    assert messages[-1]["inference_run_id"] == child["id"]
    assert env.inference.balance(env.user, env.org)["spent_microusd"] == 125
    assert calls == [("quotes", [QUOTE.model]), ("stream", QUOTE.model)]


def test_committed_artifact_metadata_is_captured_not_file_contents(installation):
    import base64

    env = installation
    review = approval_node(["compute"])
    review["inputs"] = {"outputs": {"node": "compute", "output": "artifacts"}}
    run = env.start([compute_node(), review])
    env.runs.advance(run["id"])
    child = env.claim()
    args = (env.worker["token"], child["id"], child["attempt_id"], child["lease"])
    body = b"artifact body is not an input value"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    artifact = env.compute.begin_artifact(*args, "result.txt", len(body), digest)
    env.compute.artifact_chunk(*args, artifact["id"], 0, base64.b64encode(body).decode())
    env.compute.commit_artifact(*args, artifact["id"])
    env.finish(child)
    env.runs.advance(run["id"])
    inputs = env.runs.inputs(env.user, env.org, run["id"], "approve", 1)["inputs"]
    assert inputs == {
        "outputs": [{"id": artifact["id"], "job_id": child["id"], "name": "result.txt", "size_bytes": len(body), "digest": digest}]
    }
    assert body.decode() not in json.dumps(inputs)


def test_concurrent_dispatch_and_start_are_exactly_once(installation):
    from concurrent.futures import ThreadPoolExecutor

    env = installation
    run = env.start([llm_node()])
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: env.runs.start(env.user, env.org, run["workflow_id"], 1, run["client_id"]), range(2)))
    assert replies[0]["id"] == replies[1]["id"] == run["id"]
    action = env.runs.advance(run["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: env.runs.advance(run["id"], quote_action=action, quotes=[QUOTE]), range(2)))
    with env.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 1
    assert env.inference.claim() is not None and env.inference.claim() is None
