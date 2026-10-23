"""Real PostgreSQL parity for organization contracts, keys, rows and migrations."""

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import test_organization_research_workbench as account_tests
import test_public_compute as compute_tests
import test_public_inference as inference_tests
import test_public_inference_ledger as inference_ledger_tests
import test_public_inference_timing as inference_timing_tests
import test_public_invitation_mail as mail_tests
import test_public_research_agent as research_tests
import test_public_server as server_tests
import test_public_topic_agents as topic_agent_tests
import test_public_usage as usage_tests
import test_public_work_contracts as work_tests
import test_public_workflow_runs as workflow_tests
from postgres_fixture import postgres_configuration as postgres_configuration
from test_public_compute import compute as compute
from test_public_server import installation as sqlite_installation

from reasoning_ledger.server.mail import MailStore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.postgres import Database, PostgresConfig, Row, query
from reasoning_ledger.server.schema_operations import sqlite_migration
from reasoning_ledger.server.security import private_file
from reasoning_ledger.server.store import Store

test_postgres_account_versions = account_tests.test_question_account_keeps_exact_candidates_retries_and_conflicts
test_postgres_account_scope = account_tests.test_account_working_scope_preserves_premises_and_allows_explicit_expansion
test_postgres_account_entries = account_tests.test_scoped_entries_use_published_versions_and_keyset_pages
test_postgres_account_review = account_tests.test_account_critique_is_exact_bounded_and_does_not_certify
test_postgres_account_privacy = account_tests.test_scoped_reads_reject_other_projects_alpha_and_revoked_sources
test_postgres_beta_model_policy = server_tests.test_beta_topics_keep_model_selection_independent_of_legacy_catalog_hints
test_postgres_speed_routing = inference_tests.test_speed_routing_is_explicit_and_preserves_privacy_and_regional_constraints
test_postgres_first_token_timing = (
    inference_ledger_tests.test_first_token_timing_is_once_only_and_survives_completion_and_reconciliation
)
test_postgres_wait_attribution = inference_timing_tests.test_live_timing_separates_queue_preparation_provider_and_completed_tools
test_postgres_historical_wait_timing = (
    inference_timing_tests.test_historical_missing_timing_is_unknown_not_reconstructed_from_rounded_seconds
)
test_postgres_parallel_inference = inference_tests.test_default_worker_runs_eight_provider_calls_concurrently_without_duplicate_submissions
test_postgres_background_admission = inference_tests.test_slow_background_admission_does_not_block_already_queued_inference
test_postgres_tool_round_spacing = research_tests.test_tool_rounds_without_trailing_space_remain_separate_and_have_first_token_timing
test_postgres_topic_agent_integration = (
    topic_agent_tests.test_ordinary_beta_request_delegates_and_integrates_without_goal_or_enrollment
)
test_postgres_topic_agent_nested = topic_agent_tests.test_nested_coordinator_waits_for_subordinate_integration
test_postgres_topic_agent_controls = topic_agent_tests.test_pause_resume_and_cancel_are_topic_scoped_and_preserve_queue
test_postgres_topic_agent_queue = topic_agent_tests.test_many_agents_are_queued_without_eight_run_or_twenty_request_ceiling
test_postgres_topic_agent_handoff_pages = topic_agent_tests.test_handoff_pages_do_not_starve_ready_parents_behind_waiting_agents
test_postgres_topic_agent_bot = topic_agent_tests.test_bot_delegate_uses_owner_policy_narrow_tools_and_returns_to_coordinator
test_postgres_topic_agent_compute = (
    topic_agent_tests.test_ordinary_agent_computation_waits_for_approval_and_execution_without_paid_polling
)
test_postgres_topic_agent_review = topic_agent_tests.test_ordinary_agent_review_returns_to_the_conversation_without_a_goal
test_postgres_restart = server_tests.test_durable_hierarchy_messages_and_session_survive_restart
test_postgres_tenants = server_tests.test_tenant_scope_for_all_objects_and_foreign_keys
test_postgres_concurrent_specs = work_tests.test_concurrent_create_and_revision_are_compare_and_set
test_postgres_specification_versions = work_tests.test_create_replay_versions_and_stale_edits
test_postgres_check_kinds = work_tests.test_each_verification_kind_is_distinct
test_postgres_artifact_references = work_tests.test_exact_artifact_and_work_spec_provenance
test_postgres_knowledge_cycles = work_tests.test_knowledge_neighborhoods_allow_cycles_without_proof_or_execution_edges
test_postgres_mathematical_inputs = work_tests.test_mathematical_revision_inputs_never_resolve_mutable_heads
test_postgres_input_policy = work_tests.test_catalogue_hints_do_not_restrict_program_models_or_allow_invalid_utf8
test_postgres_private_projects = work_tests.test_private_projects_and_viewers_do_not_gain_work_write_access
test_postgres_pagination = work_tests.test_progressive_lists_and_role_frontiers
test_postgres_parent_model_envelope = work_tests.test_task_models_use_parent_envelope_not_organization_catalogue_hints
test_postgres_child_resources = work_tests.test_task_envelopes_share_root_authorization
test_postgres_pinned_tasks = work_tests.test_tasks_are_pinned_and_replay_checked
test_postgres_topic_inputs = work_tests.test_topic_scoped_knowledge_is_not_widened_to_a_project
test_postgres_atomic_inference_budget = inference_tests.test_budget_reservations_are_atomic_under_competing_requests
test_postgres_inference_replay = inference_tests.test_replay_is_idempotent_and_cannot_change_models_or_context
test_postgres_charge_refusal = inference_tests.test_explicit_provider_rejection_releases_reservation
test_postgres_research_streaming = research_tests.test_streamed_tool_progress_and_final_answer_are_retained_once_without_rewriting
test_postgres_final_admission_revocation = (
    research_tests.test_final_admission_revocation_is_cancelled_even_without_another_native_callback
)
test_postgres_many_tools = research_tests.test_research_turn_has_no_implicit_24_tool_ceiling
test_postgres_research_goal = research_tests.test_goal_dispatch_and_explicit_completion_use_the_same_agent_loop
test_postgres_research_accounting = research_tests.test_unknown_second_call_does_not_hide_known_first_charge
test_postgres_usage_scenarios = usage_tests.test_profile_snapshot_message_metadata_and_organization_project_totals
test_postgres_usage_reference_energy = usage_tests.test_provider_call_reference_energy_matches_run_and_aggregate_usage
test_postgres_compute_budget = compute_tests.test_concurrent_admission_reserves_quota_atomically
test_postgres_compute_claim = compute_tests.test_concurrent_claims_do_not_duplicate_and_worker_has_one_slot
test_postgres_compute_recovery = compute_tests.test_worker_finish_events_accounting_and_replay_survive_restart
test_postgres_compute_lost_worker = compute_tests.test_lost_worker_never_requeues_and_can_report_same_attempt_late


def test_postgres_invitation_batch_dispatch_pacing(installation, monkeypatch):
    _, store, _, owner = installation
    mail = MailStore(store, mail_tests.CONFIG, "https://chat.example.org")
    mail_tests.test_entire_batch_queues_once_and_worker_automatically_paces_across_restart((store, owner, mail), monkeypatch)


def test_postgres_concurrent_mail_dispatch_quota(installation):
    _, store, _, owner = installation
    mail = MailStore(store, mail_tests.CONFIG, "https://chat.example.org")
    mail_tests.test_concurrent_mail_claims_cannot_exceed_dispatch_allowance((store, owner, mail))


def test_postgres_mail_dispatch_quota_is_shared_with_verifications(installation, monkeypatch):
    _, store, _, owner = installation
    mail = MailStore(store, mail_tests.CONFIG, "https://chat.example.org")
    mail_tests.test_delivery_quota_covers_verifications_other_organizations_and_uncertain_attempts((store, owner, mail), monkeypatch)


def test_postgres_inference_recovery(installation):
    from reasoning_ledger.server.inference_store import InferenceStore
    from reasoning_ledger.server.inference_worker import InferenceWorker
    from reasoning_ledger.server.openrouter import Usage

    config, state, _, owner, topics = inference_tests.prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    inference_tests.enqueue(state, owner, topics[1]["id"])
    abandoned = state.claim()
    state.progress(abandoned, "Paid partial result", "gen-abandoned", Usage(11, 20, 3))
    fixture = inference_tests.Fixture()
    recovered = InferenceStore(Store(config.data_dir))
    worker = InferenceWorker(recovered, fixture.provider)
    worker.start()
    try:
        deadline, statuses = time.monotonic() + 30, []
        while time.monotonic() < deadline:
            # Status polling must not repeatedly materialize the complete run
            # accounting/usage view while the executor is making progress.
            with recovered.store.transaction() as db:
                statuses = [dict(row) for row in db.execute(
                    "SELECT status,error_code FROM llm_runs WHERE organization_id=?", (org,)
                )]
            if any(row["status"] == "succeeded" for row in statuses):
                break
            assert worker.running, f"recovery worker stopped: {statuses}"
            time.sleep(0.1)
        else:
            pytest.fail(f"recovery did not complete; retained states: {statuses}; provider requests: {len(fixture.requests)}")
        assert recovered.run(user, org, abandoned["id"])["status"] == "interrupted"
        assert recovered.run(user, org, abandoned["id"])["actual_microusd"] == 11
        assert recovered.balance(user, org)["spent_microusd"] == 19
        assert len(fixture.requests) == 1
    finally:
        worker.stop()


def test_postgres_workflow_inputs(postgres_configuration, tmp_path):
    env = workflow_tests.installation.__wrapped__(tmp_path)
    workflow_tests.test_compute_approval_llm_compute_with_frozen_inputs_and_no_duplicate_dispatch(env)


@pytest.mark.parametrize("check", [
    workflow_tests.test_default_workflow_allowance_accepts_more_than_eight_waiting_runs,
    workflow_tests.test_capacity_inspection_follows_a_run_that_finishes_while_open,
    workflow_tests.test_capacity_names_separates_and_inspects_deleted_topic_runs_without_settling_them,
    workflow_tests.test_capacity_counts_hidden_work_without_exposing_private_or_deleted_topics,
])
def test_postgres_workflow_capacity(postgres_configuration, tmp_path, check):
    env = workflow_tests.installation.__wrapped__(tmp_path)
    check(env)


def test_postgres_workflow_atomic_link(postgres_configuration, tmp_path, monkeypatch):
    env = workflow_tests.installation.__wrapped__(tmp_path)
    workflow_tests.test_child_link_and_budget_reservation_roll_back_together(env, monkeypatch)


def test_postgres_workflow_uncertain(postgres_configuration, tmp_path):
    env = workflow_tests.installation.__wrapped__(tmp_path)
    workflow_tests.test_uncertain_inference_is_not_replayed_before_reconciliation(env)


@pytest.fixture
def installation(postgres_configuration, tmp_path):
    return sqlite_installation.__wrapped__(tmp_path)


def test_portable_rows_preserve_names_scalar_indexes_and_integral_aggregates():
    row = Row(["cost", "energy"], [Decimal("900000000000001"), Decimal("0.015")])
    assert row[0] == row["cost"] == 900000000000001
    assert row[1] == row[-1] == row["energy"] == 0.015
    assert isinstance(row["cost"], int)
    assert dict(row) == {"cost": 900000000000001, "energy": 0.015}


def test_driver_bindings_never_replace_literal_question_marks_or_mix_values_into_sql():
    sql = "SELECT '?' AS question, '100%' AS percent, ? AS value -- ? stays a comment\n"
    assert query(sql, bound=True) == "SELECT '?' AS question, '100%%' AS percent, %s AS value -- ? stays a comment\n"
    assert query("SELECT 'LIKE' AS word, name FROM users WHERE name LIKE ?", bound=True).endswith("name ILIKE %s")
    assert "CAST(reasoning_json_extract(e.data_json,'$.actual_microusd') AS BIGINT)" in query(
        "SELECT SUM(json_extract(e.data_json,'$.actual_microusd')) FROM inference_call_events e"
    )


@pytest.mark.parametrize("settings", [
    {"dsn": "host=remote.example password=PRIVATE", "schema": "reasoning"},
    {"dsn": "host=remote.example sslmode=require password=PRIVATE", "schema": "reasoning"},
    {"dsn": "host=localhost", "schema": "pg_catalog"},
    {"dsn": "host=localhost", "schema": "x; DROP DATABASE postgres"},
    {"dsn": "host=localhost service=elsewhere", "schema": "reasoning"},
    {"dsn": "dbname=postgres", "schema": "reasoning"},
    {"dsn": "bad PRIVATE", "schema": "reasoning"},
])
def test_configuration_refuses_unbound_or_unverified_remote_connections(tmp_path, settings):
    pytest.importorskip("psycopg")
    path = tmp_path / "postgresql.json"
    private_file(path, json.dumps(settings).encode())
    with pytest.raises(ValueError) as caught:
        PostgresConfig.read(path)
    assert "PRIVATE" not in str(caught.value)


def test_configurations_refuse_exposed_files_and_never_echo_credentials(tmp_path):
    pytest.importorskip("psycopg")
    path = tmp_path / "postgresql.json"
    private_file(path, b'{"dsn":"host=localhost password=PRIVATE","schema":"reasoning"}')
    assert "PRIVATE" not in repr(PostgresConfig.read(path))
    path.chmod(0o644)
    with pytest.raises(ValueError) as caught:
        PostgresConfig.read(path)
    assert "PRIVATE" not in str(caught.value)


def test_all_canonical_schema_tables_and_history_triggers_are_migrated(postgres_configuration):
    store = Store(postgres_configuration.parent)
    config = PostgresConfig.read(postgres_configuration)
    with store.transaction() as db:
        assert db.schema_version() == len(MIGRATIONS)
        actual = {row[0] for row in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=? AND table_name<>'reasoning_schema'", (config.schema,)
        )}
        guards = {row[0] for row in db.execute(
            "SELECT DISTINCT trigger_name FROM information_schema.triggers WHERE trigger_schema=?", (config.schema,)
        )}
    with sqlite3.connect(":memory:") as db:
        for statements in MIGRATIONS:
            for statement in statements:
                sqlite_migration(db, statement)
        expected = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'sqlite_sequence'")}
        expected_guards = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert actual == expected
    assert guards == expected_guards
    assert not (store.data_dir / "server.sqlite3").exists()


def test_populated_claim_index_migration_preserves_evidence_and_history_guard(postgres_configuration, monkeypatch):
    from reasoning_ledger.server import store as store_module
    from reasoning_ledger.server.workbench_tools import MathematicalLedger

    with monkeypatch.context() as source:
        source.setattr(store_module, "MIGRATIONS", MIGRATIONS[:52])
        store = Store(postgres_configuration.parent)
        owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "populated-ledger-fixture-password", "Research")
        ledger = MathematicalLedger(store)
        user, org = owner["user_id"], owner["organization_id"]
        reference = ledger.execute(user, org, "ledger_reference", {"author": "Author", "title": "Retained source"},
                                   client_id="migration-ref")
        claim = ledger.execute(user, org, "ledger_claim", {"statement": "An existing cited statement",
                               "reference_id": reference["reference_id"], "locator": "Theorem 1"}, client_id="migration-claim")
        with store.transaction(write=True) as db:
            db.execute("ALTER TABLE mathematical_records DISABLE TRIGGER mathematical_revision_update")
            db.execute("UPDATE mathematical_records SET object_id=? WHERE claim_id=?", (reference["reference_id"], claim["claim_id"]))
            db.execute("ALTER TABLE mathematical_records ENABLE TRIGGER mathematical_revision_update")
            before = [tuple(row) for row in db.execute("SELECT * FROM mathematical_records ORDER BY path")]
    migrated = Store(postgres_configuration.parent)
    with migrated.transaction() as db:
        assert db.schema_version() == len(MIGRATIONS)
        after = [tuple(row) for row in db.execute("SELECT * FROM mathematical_records ORDER BY path")]
        for old, new in zip(before, after, strict=True):
            assert old[:3] + old[4:] == new[:3] + new[4:]
            assert new[3] == (claim["claim_id"] if old[2] in {"claim-head", "claim-revision"} else old[3])
    with pytest.raises(Exception, match="mathematical revisions are immutable"), migrated.transaction(write=True) as db:
        db.execute("UPDATE mathematical_records SET body_json='{}' WHERE record_type='claim-revision'")


def test_installation_key_is_bound_even_without_provider_keys(postgres_configuration, tmp_path):
    original = Store(postgres_configuration.parent)
    other = tmp_path / "second-server"
    other.mkdir(mode=0o700)
    private_file(other / "postgresql.json", postgres_configuration.read_bytes())
    with pytest.raises(ValueError, match="original encryption.key"):
        Store(other)
    # Enrolling a trusted API node requires the same installation key.
    (other / "encryption.key").unlink()
    private_file(other / "encryption.key", (original.data_dir / "encryption.key").read_bytes())
    second = Store(other)
    assert second._postgres is not None


def test_migrations_serialize_across_independent_server_process_roots(postgres_configuration, tmp_path):
    key = b"x" * 32
    roots = []
    for index in range(4):
        path = tmp_path / f"node-{index}"
        path.mkdir(mode=0o700)
        private_file(path / "postgresql.json", postgres_configuration.read_bytes())
        private_file(path / "encryption.key", key)
        roots.append(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(Store, roots))
    with stores[0].transaction() as db:
        assert db.schema_version() == len(MIGRATIONS)


def test_native_json_queries_preserve_numeric_and_array_semantics(postgres_configuration):
    store = Store(postgres_configuration.parent)
    document = json.dumps({"actual_microusd": 900000000000001, "status": "running", "nodes": [{"kind": "llm"}]})
    with store.transaction() as db:
        row = db.execute("SELECT json_extract(?,'$.actual_microusd') AS cost,json_extract(?,'$.status') AS status",
                         (document, document)).fetchone()
        assert dict(row) == {"cost": 900000000000001, "status": "running"}
        assert db.execute("SELECT json_extract(n.value,'$.kind') FROM json_each(?,'$.nodes') n", (document,)).fetchone()[0] == "llm"
        assert db.execute("SELECT json_type(?,'$.actual_microusd')", (document,)).fetchone()[0] == "integer"
        assert db.execute("SELECT json_extract(?,'$[0]')", ('["example/model"]',)).fetchone()[0] == "example/model"
        assert db.execute("SELECT json_extract(?,'$.missing')", (document,)).fetchone()[0] is None


def test_failed_migration_does_not_publish_a_partial_schema(postgres_configuration):
    from psycopg.errors import SyntaxError as PostgresSyntaxError

    config = PostgresConfig.read(postgres_configuration)
    database = Database(config)
    with pytest.raises(PostgresSyntaxError):
        database.initialize(hashlib.sha256(b"key").hexdigest(), (("CREATE TABLE partial(id TEXT)", "NOT VALID SQL"),))
    with database.connect() as db:
        count = db.execute("SELECT COUNT(*) AS count FROM information_schema.tables WHERE table_schema=%s", (config.schema,)).fetchone()[0]
        assert count == 0


def test_postgres_snapshot_uses_existing_encrypted_recovery_and_retains_work_versions(installation, tmp_path):
    from reasoning_ledger.server.backup import backup, create_key, inspect_backup, restore
    from reasoning_ledger.server.work_store import WorkStore

    _, store, client, owner = installation
    _, work, project, _ = work_tests.program(installation)
    store.add_key(owner["user_id"], owner["organization_id"], "openrouter", "Fixture", "protected-provider-key")
    encryption = tmp_path / "backup-key"
    create_key(encryption)
    archive = tmp_path / "archive.rnbk"
    result = backup(store.data_dir, archive, encryption)
    inspected = inspect_backup(archive, encryption, result["sha256"])
    assert inspected["schema"] == len(MIGRATIONS)
    recovered_path = tmp_path / "recovered"
    restore(archive, encryption, result["sha256"], recovered_path, source_stopped=True)
    recovered = Store(recovered_path)
    actual = WorkStore(recovered).read(owner["user_id"], owner["organization_id"], project["id"], work["id"])
    assert actual["version"] == work["version"]
    assert not (recovered_path / "postgresql.json").exists()
    with recovered.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM provider_keys").fetchone()[0] == 1
