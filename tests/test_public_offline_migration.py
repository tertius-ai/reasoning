"""Real encrypted snapshots, SQL migrations, recovery journals and local locks."""

import ast
import base64
import errno
import fcntl
import hashlib
import inspect
import io
import json
import linecache
import os
import sqlite3
import sys
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path

import pytest
from test_public_compute_worker import IMAGE

from reasoning_ledger.deployment import host
from reasoning_ledger.deployment.server_upgrade import (
    MIGRATION_RUNNER,
    RETAINED_IDLE_QUERIES,
    RETAINED_STOPPED_WORKER_IDLE,
    STOPPED_WORKER_IDLE,
)
from reasoning_ledger.server import backup as snapshots
from reasoning_ledger.server import cli, maintenance
from reasoning_ledger.server import store as store_module
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.store import Store
from reasoning_ledger.server.workflow_store import WorkflowStore

RELEASE = "a" * 64
OPERATION = "b" * 64


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    key = tmp_path / "backup.key"
    snapshots.create_key(key)
    archive = tmp_path / "backup.rnbk"
    receipt = snapshots.backup(root, archive, key)
    return root, archive, key, receipt, store


def run(source, **changes):
    root, archive, key, receipt, _ = source
    options = {"source_schema": len(MIGRATIONS), "target_release": RELEASE, "operation_id": OPERATION, "source_stopped": True}
    options.update(changes)
    return maintenance.migrate(root, archive, key, receipt["sha256"], **options)


def future(monkeypatch):
    migrations = (
        *MIGRATIONS,
        (
            "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO migration_probe VALUES(1,'verified migration')",
        ),
    )
    monkeypatch.setattr(maintenance, "MIGRATIONS", migrations)
    monkeypatch.setattr(snapshots, "MIGRATIONS", migrations)
    return migrations


@pytest.mark.parametrize("failure,reason", [
    (OSError(errno.ENOSPC, "SECRET private path"), "disk_full"),
    (OSError(errno.EDQUOT, "SECRET private quota"), "disk_full"),
    (PermissionError(errno.EACCES, "SECRET filename"), "permission_denied"),
    (OSError(errno.EROFS, "SECRET mount"), "filesystem_readonly"),
    (maintenance.MaintenanceError("offline migration requires drained workers and settled in-flight execution"), "inflight_execution"),
    (snapshots.BackupError("backup database schema differs from this release"), "schema_mismatch"),
    (ValueError("SECRET credentials and record contents"), "migration_failed"),
    (sqlite3.IntegrityError("SECRET SQL and record contents"), "sqlite_error"),
])
def test_operator_runner_reports_only_fixed_causes_and_restores_cli(monkeypatch, failure, reason):
    def fail(*args, **kwargs):
        raise maintenance.MaintenanceError("SECRET outer diagnostic") from failure

    monkeypatch.setattr(cli, "migrate", fail)
    monkeypatch.setattr(sys, "argv", ["-c", "--data-dir", "/unused", "migrate-offline",
                                    "--backup", "/unused/source.rnbk", "--key-file", "/unused/key",
                                    "--expected-sha256", "a" * 64, "--source-schema", "54",
                                    "--target-release", RELEASE, "--operation-id", OPERATION,
                                    "--confirm-source-stopped"])
    with redirect_stdout(io.StringIO()) as output, pytest.raises(SystemExit) as exited:
        exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})
    assert exited.value.code == 0
    assert json.loads(output.getvalue()) == {"format": 1, "kind": "upgrade_migration_failure", "reason": reason}
    assert "SECRET" not in output.getvalue()
    assert cli.migrate is fail


def test_activation_observation_shares_server_lease_preserves_live_data_and_needs_no_recovery_inputs(source, monkeypatch):
    root, archive, key, _receipt, _store = source
    run(source)
    with maintenance.serving(root, release=RELEASE, operation_id=OPERATION):
        store = Store(root)
        store.bootstrap(store._setup_token, "owner@example.org", "Owner", "observation-fixture-password", "Live organization")
        archive.rename(archive.with_suffix(".retained"))
        key.rename(key.with_suffix(".retained"))
        monkeypatch.setattr(
            maintenance, "state_identity", lambda *_args, **_options: pytest.fail("live observation must not replay prepared state")
        )
        current = maintenance.journal(root)
        observed = maintenance.observe_activation(root, operation_id=OPERATION, target_release=RELEASE)
        assert observed == {
            **current,
            "activation_state_verified": True,
            "database_schema_verified": True,
            "server_started_by_command": False,
            "health_verified_by_command": False,
        }
        assert maintenance.journal(root) == current
        assert store.initialized()
        assert not (root / "maintenance-history").exists()


@pytest.mark.parametrize("change", ["prepared", "operation", "release", "schema-version", "schema-shape", "integrity"])
def test_activation_observation_refuses_unstarted_different_or_invalid_live_schema(source, change):
    root = source[0]
    run(source)
    operation_id, release = OPERATION, RELEASE
    with ExitStack() as leases:
        if change != "prepared":
            leases.enter_context(maintenance.serving(root, release=RELEASE, operation_id=OPERATION))
        if change == "operation":
            operation_id = "f" * 64
        elif change == "release":
            release = "f" * 64
        elif change in {"schema-version", "schema-shape", "integrity"}:
            with sqlite3.connect(root / "server.sqlite3") as database:
                if change == "schema-version":
                    database.execute("PRAGMA user_version=12")
                elif change == "schema-shape":
                    database.execute("CREATE TABLE unexpected_schema(value TEXT)")
                else:
                    database.execute("PRAGMA foreign_keys=OFF")
                    database.execute("INSERT INTO memberships VALUES('missing-org','missing-user','member')")
        with pytest.raises(maintenance.MaintenanceError):
            maintenance.observe_activation(root, operation_id=operation_id, target_release=release)
        assert maintenance.journal(root)["phase"] != "completed"


def version(root):
    with sqlite3.connect(root / "server.sqlite3") as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_same_schema_preparation_retains_credentials_backup_and_blocks_unconfirmed_startup(source):
    root, archive, key, receipt, _ = source
    original_key = (root / "encryption.key").read_bytes()
    setup = (root / "setup-token").read_bytes()
    result = run(source)
    assert result["phase"] == "migrated" and result["server_started"] is result["activation_performed"] is False
    assert result["source_state_sha256"] == result["target_state_sha256"]
    assert (root / "encryption.key").read_bytes() == original_key and (root / "setup-token").read_bytes() == setup
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["sha256"] and key.exists()
    assert (root / maintenance.JOURNAL).stat().st_mode & 0o777 == 0o600
    with pytest.raises(maintenance.MaintenanceError, match="unfinished"):
        Store(root)
    assert run(source) == result
    with pytest.raises(maintenance.MaintenanceError, match="original maintenance"), maintenance.serving(root):
        pytest.fail("pending migration must not start the server")


def test_real_schema_migration_is_dry_run_verified_and_keeps_source_backup(source, monkeypatch):
    migrations = future(monkeypatch)
    result = run(source)
    assert version(source[0]) == len(migrations)
    with sqlite3.connect(source[0] / "server.sqlite3") as db:
        assert db.execute("SELECT * FROM migration_probe").fetchall() == [(1, "verified migration")]
    assert result["source_state_sha256"] != result["target_state_sha256"] and result["phase"] == "migrated"
    assert run(source) == result
    assert source[1].exists() and source[2].exists()


@pytest.mark.parametrize("source_schema", [26, 36, 39])
@pytest.mark.parametrize("compatibility", [False, True])
def test_release_upgrade_across_sqlite_rebuilds_preserves_populated_artifacts(tmp_path, monkeypatch, source_schema, compatibility):
    root, archive, key = tmp_path / "state", tmp_path / "source.rnbk", tmp_path / "backup.key"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", MIGRATIONS[:source_schema])
        old.setattr(snapshots, "MIGRATIONS", MIGRATIONS[:source_schema])
        store = Store(root)
        owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "migration-fixture-password", "Retained organization")
        user, org = owner["user_id"], owner["organization_id"]
        project = store.create_entity(user, org, "projects", "Retained project")
        channel = store.create_entity(user, org, "channels", "Retained channel", project["id"])
        topic = store.create_entity(user, org, "topics", "Retained topic", channel["id"], topic_kind="beta")
        with store.transaction(write=True) as db:
            db.execute("INSERT INTO research_artifacts VALUES(?,?,?,?,?,1)",
                       ("retained-artifact", org, project["id"], topic["id"], "evidence.txt"))
            text = "Retained evidence 😊"
            checksum = hashlib.sha256(text.encode()).hexdigest()
            db.execute("INSERT INTO research_artifact_versions VALUES(?,?,?,?,?,?,?)",
                       ("retained-artifact", 1, text, checksum, user, None, 1))
        snapshots.create_key(key)
        receipt = snapshots.backup(root, archive, key)
    original_key = (root / "encryption.key").read_bytes()
    if compatibility:
        # Exact raw-SQL executor shape used by already-retained target releases.
        def raw_migrate(root, source):
            with snapshots._connect(root / "server.sqlite3", writable=True) as db:
                db.execute("BEGIN IMMEDIATE")
                snapshots._schema(db, schema_version=source)
                for index in range(source, len(MIGRATIONS)):
                    for statement in MIGRATIONS[index]:
                        db.execute(statement)
                    db.execute(f"PRAGMA user_version={index + 1}")
                snapshots._schema(db)

        with monkeypatch.context() as context, redirect_stdout(io.StringIO()) as output:
            context.setattr(maintenance, "_migrate_database", raw_migrate)
            context.setattr(sys, "argv", ["-c", "--data-dir", str(root), "migrate-offline", "--backup", str(archive),
                                        "--key-file", str(key), "--expected-sha256", receipt["sha256"],
                                        "--source-schema", str(source_schema), "--target-release", RELEASE,
                                        "--operation-id", OPERATION, "--confirm-source-stopped"])
            with pytest.raises(SystemExit) as exited:
                exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})
            assert exited.value.code == 0
        result = json.loads(output.getvalue())
    else:
        result = maintenance.migrate(root, archive, key, receipt["sha256"], source_schema=source_schema,
                                     target_release=RELEASE, operation_id=OPERATION, source_stopped=True)
    assert result["phase"] == "migrated" and version(root) == len(MIGRATIONS)
    assert (root / "encryption.key").read_bytes() == original_key
    with sqlite3.connect(root / "server.sqlite3") as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT text,sha256 FROM research_artifact_versions WHERE artifact_id='retained-artifact'").fetchone() == (
            text, checksum,
        )
        assert db.execute("SELECT topic_id FROM research_artifacts WHERE id='retained-artifact'").fetchone() == (topic["id"],)
    assert maintenance.migrate(root, archive, key, receipt["sha256"], source_schema=source_schema,
                               target_release=RELEASE, operation_id=OPERATION, source_stopped=True) == result
    assert archive.exists() and key.exists()


def test_migration_runner_refuses_an_unrecognized_executor(monkeypatch):
    monkeypatch.setattr(maintenance, "_migrate_database", lambda *_: pytest.fail("must not execute"))
    with pytest.raises(SystemExit, match="unrecognized migration executor"):
        exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})


def test_disabled_foreign_keys_during_rebuild_do_not_allow_invalid_commits(source, monkeypatch):
    migrations = (*MIGRATIONS, ("INSERT INTO memberships VALUES('missing-organization','missing-user','member')",))
    monkeypatch.setattr(maintenance, "MIGRATIONS", migrations)
    monkeypatch.setattr(snapshots, "MIGRATIONS", migrations)
    with pytest.raises(maintenance.MaintenanceError, match="offline migration failed"):
        run(source, source_schema=len(MIGRATIONS))
    assert version(source[0]) == len(MIGRATIONS)
    assert not (source[0] / maintenance.JOURNAL).exists()


@pytest.mark.parametrize("change", ["confirmation", "source-schema", "target-release", "operation", "snapshot-digest", "wrong-key"])
def test_invalid_inputs_do_not_migrate_or_publish_intent(source, change):
    root, archive, key, receipt, _ = source
    options = {}
    if change == "confirmation":
        options["source_stopped"] = False
    elif change == "source-schema":
        options["source_schema"] = True
    elif change == "target-release":
        options["target_release"] = "x" * 64
    elif change == "operation":
        options["operation_id"] = "not-an-operation"
    elif change == "snapshot-digest":
        receipt["sha256"] = "0" * 64
    else:
        key.write_bytes(b"x" * 32)
    with pytest.raises(maintenance.MaintenanceError):
        run(source, **options)
    assert version(root) == len(MIGRATIONS) and not (root / maintenance.JOURNAL).exists()
    assert archive.exists()


def test_snapshot_must_match_actual_database_not_only_schema_and_key(source):
    root, _, _, _, store = source
    store.bootstrap(store._setup_token, "owner@example.org", "Owner", "very long password fixture", "Research")
    with pytest.raises(maintenance.MaintenanceError, match="exact stopped installation"):
        run(source)
    assert not (root / maintenance.JOURNAL).exists() and store.initialized()


@pytest.mark.parametrize("name", [maintenance.LOCK, *maintenance.WORKER_LOCKS])
def test_active_foreground_or_execution_workers_refuse_migration(source, name):
    path = source[0] / name
    if not path.exists():
        host.atomic_write(path, b"")
    with path.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(maintenance.MaintenanceError, match="owns"):
            run(source)
    assert not (source[0] / maintenance.JOURNAL).exists()


def test_foreground_shared_lease_fences_offline_migration(source):
    with maintenance.serving(source[0]), pytest.raises(maintenance.MaintenanceError, match="owns"):
        run(source)
    assert run(source)["phase"] == "migrated"


@pytest.mark.parametrize("draining,revoked,refused", [(0, None, True), (1, None, False), (0, 123, False)])
def test_registered_worker_drain_is_checked_in_authenticated_snapshot(source, draining, revoked, refused):
    root, archive, key, receipt, store = source
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "very long password fixture", "Research")
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO compute_workers(id,organization_id,name,token_hash,image_ids_json,resources_json,created_at,draining,revoked_at) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            ("c" * 32, owner["organization_id"], "fixture", "d" * 64, "[]", "{}", draining, revoked),
        )
    replacement = archive.parent / "drained.rnbk"
    receipt.update(snapshots.backup(root, replacement, key))
    altered = (root, replacement, key, receipt, store)
    if refused:
        with pytest.raises(maintenance.MaintenanceError, match="drained"):
            run(altered)
        assert not (root / maintenance.JOURNAL).exists()
    else:
        assert run(altered)["phase"] == "migrated"


@pytest.mark.parametrize("boundary", ["validated", "migrating-before-sql", "migrating-after-commit", "migrated"])
def test_crash_recovery_recognizes_exact_source_or_committed_target_without_restoring(source, monkeypatch, boundary):
    future(monkeypatch)
    original_write = maintenance.atomic_write
    original_migrate = maintenance._migrate_database
    raised = False

    def write(path, body, *args):
        nonlocal raised
        if path.name == maintenance.JOURNAL:
            phase = json.loads(body)["phase"]
            if not raised and boundary in {"validated", "migrated"} and phase == boundary:
                original_write(path, body, *args)
                raised = True
                raise RuntimeError("simulated process interruption")
        return original_write(path, body, *args)

    def migrate(root, schema):
        nonlocal raised
        if root == source[0] and not raised:
            if boundary == "migrating-before-sql":
                raised = True
                raise RuntimeError("simulated process interruption")
            if boundary == "migrating-after-commit":
                original_migrate(root, schema)
                raised = True
                raise RuntimeError("simulated lost command response")
        return original_migrate(root, schema)

    monkeypatch.setattr(maintenance, "atomic_write", write)
    monkeypatch.setattr(maintenance, "_migrate_database", migrate)
    with pytest.raises(RuntimeError):
        run(source)
    assert raised and maintenance.journal(source[0])["phase"] != "activation_started"
    result = run(source)
    assert result["phase"] == "migrated" and version(source[0]) == len(MIGRATIONS) + 1


def test_failed_sql_rolls_back_to_original_schema_but_keeps_pending_journal_and_backup(source, monkeypatch):
    migrations = (*MIGRATIONS, ("CREATE TABLE migration_probe(id INTEGER)", "INSERT INTO nonexistent_table VALUES(1)"))
    monkeypatch.setattr(maintenance, "MIGRATIONS", migrations)
    monkeypatch.setattr(snapshots, "MIGRATIONS", migrations)
    with pytest.raises(maintenance.MaintenanceError, match="failed"):
        run(source)
    # Dry-run failure occurs before the live intent or schema changes.
    assert version(source[0]) == len(MIGRATIONS)
    assert not (source[0] / maintenance.JOURNAL).exists() and source[1].exists()


@pytest.mark.parametrize("key,value", [("operation_id", "f" * 64), ("target_release", "f" * 64), ("source_schema", 12)])
def test_recovery_does_not_adopt_a_different_operation_or_release(source, key, value):
    run(source)
    with pytest.raises(maintenance.MaintenanceError):
        run(source, **{key: value})
    assert maintenance.journal(source[0])["phase"] == "migrated"


def test_prepared_mail_configuration_is_bound_and_cannot_drift_before_activation(source):
    path = source[0] / "mail.json"
    host.atomic_write(path, b'{"private":"original"}\n')
    run(source)
    host.atomic_write(path, b'{"private":"changed"}\n')
    with pytest.raises(maintenance.MaintenanceError, match="differs"):
        run(source)
    with (
        pytest.raises(maintenance.MaintenanceError, match="changed"),
        maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION),
    ):
        pytest.fail("changed prepared state must not start")


def test_activation_boundary_precedes_application_start_and_forbids_offline_replay(source):
    result = run(source)
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        assert maintenance.journal(source[0])["phase"] == "activation_started"
        assert Store(source[0]).initialized() is False
    assert maintenance.journal(source[0])["target_state_sha256"] == result["target_state_sha256"]
    with pytest.raises(maintenance.MaintenanceError, match="external effects"):
        run(source)
    # A confirmed target restart is allowed without pretending live data still
    # equals the pre-activation snapshot. Other targets are not allowed.
    Store(source[0]).bootstrap(
        (source[0] / "setup-token").read_text(), "owner@example.org", "Owner", "very long password fixture", "Research"
    )
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        assert Store(source[0]).initialized()
    with pytest.raises(maintenance.MaintenanceError), maintenance.serving(source[0], release="f" * 64, operation_id=OPERATION):
        pytest.fail("different release cannot reuse a pending activation")


@pytest.mark.parametrize("change", ["symlink", "hardlink", "public", "malformed", "extra", "duplicate", "unknown-phase"])
def test_insecure_or_malformed_maintenance_journals_do_not_initialize_server(source, change):
    run(source)
    path = source[0] / maintenance.JOURNAL
    original = path.read_bytes()
    if change == "symlink":
        path.unlink()
        path.symlink_to(source[1])
    elif change == "hardlink":
        os.link(path, source[0] / "linked")
    elif change == "public":
        path.chmod(0o644)
    elif change == "malformed":
        host.atomic_write(path, b"invalid")
    elif change == "extra":
        value = json.loads(original)
        value["credential"] = "must-not-appear"
        host.atomic_write(path, host.canonical(value))
    elif change == "duplicate":
        host.atomic_write(path, original.replace(b'"format":1', b'"format":1,"format":1'))
    else:
        value = json.loads(original)
        value["phase"] = "unknown"
        host.atomic_write(path, host.canonical(value))
    with pytest.raises(maintenance.MaintenanceError) as failure:
        Store(source[0])
    assert "must-not-appear" not in str(failure.value)


def test_normal_restore_and_inspection_still_require_matching_release_schema(source, monkeypatch):
    future(monkeypatch)
    with pytest.raises(snapshots.BackupError, match="manifest"):
        snapshots.inspect_backup(source[1], source[2], source[3]["sha256"])
    assert run(source)["phase"] == "migrated"


def test_cli_migration_is_explicit_and_serve_retains_lifetime_lease(source, monkeypatch, capsys):
    arguments = [
        "--data-dir",
        str(source[0]),
        "migrate-offline",
        "--backup",
        str(source[1]),
        "--key-file",
        str(source[2]),
        "--expected-sha256",
        source[3]["sha256"],
        "--source-schema",
        str(len(MIGRATIONS)),
        "--target-release",
        RELEASE,
        "--operation-id",
        OPERATION,
        "--confirm-source-stopped",
    ]
    assert cli.main(arguments) == 0 and json.loads(capsys.readouterr().out)["phase"] == "migrated"
    calls = []

    def start(config):
        assert maintenance.journal(source[0])["phase"] == "activation_started"
        with pytest.raises(maintenance.MaintenanceError, match="owns"):
            run(source)
        return "explicit application fixture"

    monkeypatch.setattr(cli, "create_app", start)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: calls.append((app, options)))
    assert (
        cli.main(
            [
                "--data-dir",
                str(source[0]),
                "serve",
                "--public-url",
                "https://chat.example.org",
                "--deployment-release",
                RELEASE,
                "--maintenance-operation",
                OPERATION,
            ]
        )
        == 0
    )
    assert len(calls) == 1 and calls[0][0] == "explicit application fixture"


def test_managed_unit_maintenance_argument_is_bound_to_exact_release():
    body = host.server_unit(RELEASE, release_identity=True, maintenance_operation=OPERATION)
    assert f"--deployment-release {RELEASE} --maintenance-operation {OPERATION}" in body
    with pytest.raises(host.InstallError):
        host.server_unit(RELEASE, maintenance_operation=OPERATION)


def test_completion_retains_immutable_history_without_changing_live_database(source):
    run(source)
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        store = Store(source[0])
        store.bootstrap(store._setup_token, "owner@example.org", "Owner", "very long password fixture", "Research")
        identity = maintenance.state_identity(source[0], len(MIGRATIONS), snapshots.DEFAULT_BYTES)
        result = maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
        assert result["phase"] == "completed" and result["health_verified_by_command"] is False
        assert result["activation_verified_by_operator"] and result["services_changed"] is False
        assert result["backup_retention_verified_by_command"] is False and "backup_retained" not in result
        assert maintenance.state_identity(source[0], len(MIGRATIONS), snapshots.DEFAULT_BYTES) == identity
    history = source[0] / "maintenance-history" / (OPERATION + ".json")
    assert history.read_bytes() == (source[0] / maintenance.JOURNAL).read_bytes()
    assert history.stat().st_mode & 0o777 == 0o600
    assert maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True) == result
    with maintenance.serving(source[0], release=RELEASE):
        assert Store(source[0]).initialized()
    with pytest.raises(maintenance.MaintenanceError, match="completed migration"):
        run(source)


@pytest.mark.parametrize("change", ["confirmation", "different-operation", "different-release", "not-activated"])
def test_completion_requires_exact_acknowledged_activation(source, change):
    run(source)
    if change != "not-activated":
        with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
            pass
    options = {"operation_id": OPERATION, "target_release": RELEASE, "activation_verified": True}
    if change == "confirmation":
        options["activation_verified"] = False
    elif change == "different-operation":
        options["operation_id"] = "f" * 64
    elif change == "different-release":
        options["target_release"] = "f" * 64
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.finish(source[0], **options)
    assert maintenance.journal(source[0])["phase"] != "completed"


def test_later_upgrade_uses_fresh_backup_and_preserves_completed_operation_history(source):
    run(source)
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        pass
    maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
    history = source[0] / "maintenance-history" / (OPERATION + ".json")
    original = history.read_bytes()
    store = Store(source[0])
    store.bootstrap(store._setup_token, "owner@example.org", "Owner", "very long password fixture", "Research")
    archive = source[1].parent / "next-backup.rnbk"
    receipt = snapshots.backup(source[0], archive, source[2])
    next_source = (source[0], archive, source[2], receipt, store)
    result = run(next_source, operation_id="d" * 64, target_release="e" * 64)
    assert result["phase"] == "migrated" and result["operation_id"] == "d" * 64
    assert history.read_bytes() == original
    with maintenance.serving(source[0], release="e" * 64, operation_id="d" * 64):
        assert Store(source[0]).initialized()


def test_missing_or_changed_completion_history_is_never_recreated_as_success(source):
    run(source)
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        pass
    maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
    history = source[0] / "maintenance-history" / (OPERATION + ".json")
    host.atomic_write(history, b"different receipt")
    with pytest.raises(maintenance.MaintenanceError), maintenance.serving(source[0], release=RELEASE):
        pytest.fail("changed recovery evidence must not be adopted")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
    assert history.read_bytes() == b"different receipt"


def test_store_rechecks_pending_fence_after_initializer_lock_wait(source, monkeypatch):
    run(source)
    path = source[0] / maintenance.JOURNAL
    pending = path.read_bytes()
    path.unlink()
    original = fcntl.flock

    def acquire(fd, flags):
        if flags == fcntl.LOCK_EX:
            host.atomic_write(path, pending)
        return original(fd, flags)

    monkeypatch.setattr(fcntl, "flock", acquire)
    with pytest.raises(maintenance.MaintenanceError, match="unfinished"):
        Store(source[0])


def test_live_sql_failure_is_atomic_and_recovers_under_original_journal(source, monkeypatch):
    future(monkeypatch)
    original = maintenance._migrate_database

    def fail_once(root, schema):
        if root == source[0]:
            with snapshots._connect(root / "server.sqlite3", writable=True) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE abandoned_migration(id INTEGER)")
                raise sqlite3.OperationalError("deliberate migration fixture failure")
        return original(root, schema)

    monkeypatch.setattr(maintenance, "_migrate_database", fail_once)
    with pytest.raises(maintenance.MaintenanceError, match="failed"):
        run(source)
    assert maintenance.journal(source[0])["phase"] == "migrating"
    assert version(source[0]) == len(MIGRATIONS)
    with sqlite3.connect(source[0] / "server.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='abandoned_migration'").fetchone()
    monkeypatch.setattr(maintenance, "_migrate_database", original)
    assert run(source)["phase"] == "migrated"


@pytest.mark.parametrize("changed", ["archive", "key"])
@pytest.mark.parametrize("boundary", ["dry-run", "live-commit"])
def test_recovery_inputs_are_rechecked_before_intent_and_after_live_commit(source, monkeypatch, changed, boundary):
    future(monkeypatch)
    original = maintenance._migrate_database
    path = source[1] if changed == "archive" else source[2]
    body = path.read_bytes()

    def replace_after_migration(root, schema):
        result = original(root, schema)
        if (root == source[0]) == (boundary == "live-commit"):
            host.atomic_write(path, b"x" * len(body))
        return result

    monkeypatch.setattr(maintenance, "_migrate_database", replace_after_migration)
    with pytest.raises(maintenance.MaintenanceError, match="original backup or key changed"):
        run(source)
    if boundary == "dry-run":
        assert not (source[0] / maintenance.JOURNAL).exists()
        assert version(source[0]) == len(MIGRATIONS)
    else:
        assert maintenance.journal(source[0])["phase"] == "migrating"
        assert version(source[0]) == len(MIGRATIONS) + 1
    host.atomic_write(path, body)
    monkeypatch.setattr(maintenance, "_migrate_database", original)
    assert run(source)["phase"] == "migrated"


def test_foreground_refuses_implicit_migration_of_an_older_schema(source, monkeypatch):
    future(monkeypatch)
    with pytest.raises(maintenance.MaintenanceError, match="exact target schema"), maintenance.serving(source[0]):
        pytest.fail("startup must not implicitly migrate existing state")
    assert version(source[0]) == len(MIGRATIONS)
    assert not (source[0] / maintenance.JOURNAL).exists()


def test_deleted_completed_history_cannot_be_recreated_by_completion_or_initialization(source):
    run(source)
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        pass
    maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
    history = source[0] / "maintenance-history" / (OPERATION + ".json")
    history.unlink()
    with pytest.raises(maintenance.MaintenanceError, match="missing"):
        maintenance.finish(source[0], operation_id=OPERATION, target_release=RELEASE, activation_verified=True)
    with pytest.raises(maintenance.MaintenanceError, match="missing"):
        Store(source[0])
    assert not history.exists()


@pytest.fixture
def organization(source):
    root, archive, key, receipt, store = source
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "very long password fixture", "Research")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Project")
    channel = store.create_entity(user, org, "channels", "Channel", project["id"])
    topic = store.create_entity(user, org, "topics", "Compute", channel["id"], "gamma")
    provider = store.add_key(user, org, "openrouter", "Key", "provider-secret-not-public")
    compute = ComputeStore(store, {"fixture": ComputeImage.parse(IMAGE)})
    compute.set_policy(user, org, ["fixture"], 10000, 2)
    worker = compute.register_worker(user, org, "Worker", ["fixture"], DEFAULT_RESOURCES)
    return source, owner, topic["id"], provider, compute, worker


def stopped_snapshot(organization):
    source, *_ = organization
    root, archive, key, receipt, store = source
    archive = archive.parent / "stopped-state.rnbk"
    receipt = snapshots.backup(root, archive, key)
    return root, archive, key, receipt, store


def test_real_sql_migration_preserves_accounts_sessions_secrets_queue_and_artifacts(organization, monkeypatch):
    source, owner, topic, provider, compute, worker = organization
    root, _, _, _, store = source
    user, org = owner["user_id"], owner["organization_id"]
    invite = store.invite(user, org, "invited@example.org", "member", 3600)
    store.post_message(user, org, topic, "retained private chat", "original-message")
    parameters = compute.parameters("fixture", "python", "print('not executed')", DEFAULT_RESOURCES)
    done = compute.enqueue(user, org, topic, "completed-program", parameters)
    claim = compute.claim(worker["token"], "c" * 32)
    args = worker["token"], done["id"], claim["attempt_id"], claim["lease"]
    body = b"retained algebra result\n"
    artifact = compute.begin_artifact(*args, "output.txt", len(body), "sha256:" + hashlib.sha256(body).hexdigest())
    compute.artifact_chunk(*args, artifact["id"], 0, base64.b64encode(body).decode())
    compute.commit_artifact(*args, artifact["id"])
    compute.finish(*args, "succeeded", 10, 0, None)
    queued = compute.enqueue(user, org, topic, "queued-program", parameters)
    compute.drain_self(worker["token"])
    with store.transaction() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        rows = {table: db.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables}
    stopped = stopped_snapshot(organization)
    migrations = future(monkeypatch)
    monkeypatch.setattr(store_module, "MIGRATIONS", migrations)
    assert run(stopped)["phase"] == "migrated"
    with maintenance.serving(root, release=RELEASE, operation_id=OPERATION):
        recovered = Store(root)
        assert recovered.session(owner["access_token"])["user_id"] == user
        with recovered.transaction() as db:
            assert {table: db.execute('SELECT * FROM "' + table + '"').fetchall() for table in tables} == rows
            assert db.execute("SELECT revoked_at FROM invitations WHERE id=?", (invite["id"],)).fetchone()[0] is None
            value = db.execute("SELECT * FROM provider_keys WHERE id=?", (provider["id"],)).fetchone()
            assert (
                recovered._box.open(value["ciphertext"], organization_id=org, provider="openrouter", key_id=provider["id"])
                == "provider-secret-not-public"
            )
        restored_compute = ComputeStore(recovered, {"fixture": ComputeImage.parse(IMAGE)})
        assert restored_compute.job(user, org, queued["id"])["status"] == "queued"
        assert restored_compute.job(user, org, done["id"])["status"] == "succeeded"
        assert restored_compute._read_chunk(artifact["id"], 0) == body


@pytest.mark.parametrize("status", ["running", "outcome_unknown"])
def test_draining_does_not_authorize_migration_with_unsettled_compute(organization, status):
    source, owner, topic, _, compute, worker = organization
    job = compute.enqueue(
        owner["user_id"], owner["organization_id"], topic, "in-flight", compute.parameters("fixture", "python", "pass", DEFAULT_RESOURCES)
    )
    compute.claim(worker["token"], "d" * 32)
    compute.drain_self(worker["token"])
    with source[4].transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET status=? WHERE id=?", (status, job["id"]))
    stopped = stopped_snapshot(organization)
    with pytest.raises(maintenance.MaintenanceError, match="settled in-flight"):
        run(stopped)
    assert not (source[0] / maintenance.JOURNAL).exists()


@pytest.mark.parametrize("retained", [False, True])
def test_stopped_running_model_migrates_and_recovers_without_replay_or_losing_reservation(organization, monkeypatch, retained):
    source, owner, topic, provider, compute, worker = organization
    compute.drain_self(worker["token"])
    with source[4].transaction(write=True) as db:
        db.execute(
            "INSERT INTO llm_requests VALUES(?,?,?,?,?,?,?,?,?)",
            ("request", owner["organization_id"], topic, owner["user_id"], "request", "{}", "[]", 0, 1),
        )
        db.execute(
            "INSERT INTO llm_runs(id,organization_id,request_id,model,key_id,quote_json,status,reserved_microusd,accounting_state) "
            "VALUES(?,?,?,?,?,?, 'running',200,'reserved')",
            ("run", owner["organization_id"], "request", "fixture/model", provider["id"], "{}"),
        )
    stopped = stopped_snapshot(organization)
    archive_before = stopped[1].read_bytes()
    if retained:
        namespace = {"snapshots": snapshots, "MaintenanceError": maintenance.MaintenanceError}
        code = ("def _idle(root):\n"
                "    with snapshots._connect(root / 'server.sqlite3') as db:\n"
                "        for query in " + repr(RETAINED_IDLE_QUERIES) + ":\n"
                "            if db.execute(query).fetchone():\n"
                "                raise MaintenanceError('offline migration requires drained workers and settled in-flight execution')\n")
        exec(compile(code, "<retained-worker-guard>", "exec"), namespace)
        old_idle = namespace["_idle"]
        with pytest.raises(maintenance.MaintenanceError, match="settled in-flight"):
            old_idle(source[0])
        monkeypatch.setattr(maintenance, "_idle", old_idle)

        def helper(command, arguments):
            monkeypatch.setattr(sys, "argv", ["-c", "--data-dir", str(source[0]), command, *arguments])
            with redirect_stdout(io.StringIO()) as output, pytest.raises(SystemExit) as exited:
                exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})
            assert exited.value.code == 0
            assert maintenance._idle is old_idle
            return json.loads(output.getvalue())

        result = helper("migrate-offline", ["--backup", str(stopped[1]), "--key-file", str(stopped[2]),
                        "--expected-sha256", stopped[3]["sha256"], "--source-schema", str(len(MIGRATIONS)),
                        "--target-release", RELEASE, "--operation-id", OPERATION, "--confirm-source-stopped"])
        assert helper("maintenance-verify", ["--operation-id", OPERATION, "--target-release", RELEASE])["prepared_state_verified"]
    else:
        result = run(stopped)
        assert maintenance.verify_prepared(source[0], operation_id=OPERATION, target_release=RELEASE)["prepared_state_verified"]
    assert result["phase"] == "migrated" and stopped[1].read_bytes() == archive_before
    with sqlite3.connect(source[0] / "server.sqlite3") as db:
        assert db.execute("SELECT status,reserved_microusd,actual_microusd,accounting_state FROM llm_runs").fetchone() == (
            "running", 200, None, "reserved")
    with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
        recovered = InferenceStore(Store(source[0]))
        recovered.recover()
        with recovered.store.transaction() as db:
            assert tuple(db.execute("SELECT status,reserved_microusd,actual_microusd,accounting_state FROM llm_runs").fetchone()) == (
                "interrupted", 200, None, "uncertain")
        assert recovered.claim() is None


def test_embedded_stopped_worker_guard_matches_canonical_guard():
    native = ast.parse(inspect.getsource(maintenance._idle)).body[0]
    embedded = ast.parse(STOPPED_WORKER_IDLE).body[0]
    assert ast.dump(native) == ast.dump(embedded)


def test_retained_runner_refuses_unrecognized_worker_guard_before_any_migration(monkeypatch):
    monkeypatch.setattr(maintenance, "_idle", lambda root: None)
    monkeypatch.setattr(sys, "argv", ["-c", "--data-dir", "/unused", "migrate-offline"])
    with pytest.raises(SystemExit, match="unrecognized offline worker guard"):
        exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})


def test_retained_runner_refuses_changed_stopped_worker_guard(tmp_path, monkeypatch):
    path = tmp_path / "guard.py"
    path.write_text(RETAINED_STOPPED_WORKER_IDLE.replace("a.kind!='llm'", "a.kind!='compute'"))
    namespace = {"snapshots": snapshots, "MaintenanceError": maintenance.MaintenanceError}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    monkeypatch.setattr(maintenance, "_idle", namespace["_idle"])
    monkeypatch.setattr(sys, "argv", ["-c", "--data-dir", "/unused", "migrate-offline"])
    with pytest.raises(SystemExit, match="unrecognized offline worker guard"):
        exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})


@pytest.mark.parametrize("name", ["inference-worker.lock", "workflow-worker.lock", "mail-worker.lock", "webhook-worker.lock"])
def test_stopped_record_policy_never_waives_a_live_worker_lease(source, name):
    lock = source[0] / name
    host.atomic_write(lock, b"")
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(maintenance.MaintenanceError, match="another server"):
            run(source)
    assert not (source[0] / maintenance.JOURNAL).exists()


@pytest.mark.parametrize("kind,node_status,child_status,refused", [
    ("llm", "outcome_unknown", "failed", False),
    ("llm", "outcome_unknown", "succeeded", False),
    ("llm", "outcome_unknown", "cancelled", False),
    ("llm", "outcome_unknown", "interrupted", False),
    ("llm", "outcome_unknown", "queued", True),
    ("llm", "outcome_unknown", "running", True),
    ("llm", "outcome_unknown", None, True),
    ("llm", "outcome_unknown", "foreign_org_terminal", True),
    ("llm", "running", "failed", False),
    ("compute", "outcome_unknown", None, True),
    ("approval", "outcome_unknown", None, True),
])
@pytest.mark.parametrize("local_workers_stopped", [False, True])
def test_idle_distinguishes_terminal_llm_evidence_from_unsettled_execution(
    monkeypatch, kind, node_status, child_status, refused, local_workers_stopped,
):
    if local_workers_stopped and kind == "llm" and child_status in {"queued", "running"}:
        refused = False
    with sqlite3.connect(":memory:") as db:
        for statement in (
            "CREATE TABLE compute_workers(revoked_at,draining)",
            "CREATE TABLE compute_jobs(status)",
            "CREATE TABLE llm_runs(organization_id,id,status)",
            "CREATE TABLE workflow_node_attempts(organization_id,inference_run_id,kind,status)",
            "CREATE TABLE invitation_mail(state)",
            "CREATE TABLE bot_webhook_deliveries(state)",
        ):
            db.execute(statement)
        if child_status is not None:
            child_org = "another-org" if child_status == "foreign_org_terminal" else "org"
            actual_status = "failed" if child_status == "foreign_org_terminal" else child_status
            db.execute("INSERT INTO llm_runs VALUES(?,'child',?)", (child_org, actual_status))
        db.execute("INSERT INTO workflow_node_attempts VALUES('org','child',?,?)", (kind, node_status))

        @contextmanager
        def connection(_path):
            yield db

        monkeypatch.setattr(snapshots, "_connect", connection)
        if refused:
            with pytest.raises(maintenance.MaintenanceError, match="settled in-flight"):
                maintenance._idle(Path("unused"), local_workers_stopped=local_workers_stopped)
        else:
            maintenance._idle(Path("unused"), local_workers_stopped=local_workers_stopped)


def test_real_migration_retains_unknown_workflow_outcome_and_billing_reservation(organization, monkeypatch):
    source, owner, topic, provider, compute, worker = organization
    root, _, _, _, store = source
    compute.drain_self(worker["token"])
    user, org = owner["user_id"], owner["organization_id"]
    saved = WorkflowStore(store).create(user, org, topic, "maintenance-uncertain-workflow", {
        "format": 1, "name": "Uncertain inference", "nodes": [{"id": "reason", "kind": "llm", "depends_on": [],
        "inputs": {}, "parameters": {"model": "fixture/model", "prompt": "A research question", "max_completion_tokens": 128}}],
    })
    with store.transaction(write=True) as db:
        db.execute("INSERT INTO llm_requests VALUES(?,?,?,?,?,?,?,?,?)",
                   ("request", org, topic, user, "request", "{}", "[]", 0, 1))
        db.execute(
            "INSERT INTO llm_runs(id,organization_id,request_id,model,key_id,quote_json,status,reserved_microusd,accounting_state) "
            "VALUES(?,?,?,?,?,?,'failed',200,'uncertain')", ("child", org, "request", "fixture/model", provider["id"], "{}"))
        db.execute(
            "INSERT INTO workflow_runs(id,organization_id,topic_id,workflow_id,definition_version,requested_by,client_id,"
            "created_at,status,images_json) VALUES('workflow-run',?,?,?,?,?,'run',1,'outcome_unknown','{}')",
            (org, topic, saved["workflow_id"], saved["version"], user))
        db.execute(
            "INSERT INTO workflow_node_attempts(organization_id,run_id,node_id,attempt,kind,status,created_at,inference_run_id) "
            "VALUES(?,'workflow-run','reason',1,'llm','outcome_unknown',1,'child')", (org,))
        rows = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                for table in ("workflow_runs", "workflow_node_attempts", "llm_runs")}
    stopped = stopped_snapshot(organization)
    future(monkeypatch)
    assert run(stopped)["phase"] == "migrated"
    with sqlite3.connect(root / "server.sqlite3") as db:
        assert {table: db.execute("SELECT * FROM " + table).fetchall() for table in rows} == rows
        assert db.execute("SELECT reserved_microusd,actual_microusd,accounting_state FROM llm_runs").fetchone() == (200, None, "uncertain")


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "outcome_unknown",
                                  "queued", "running", "waiting_approval", "missing", "foreign_org"])
@pytest.mark.parametrize("node_status", ["running", "outcome_unknown"])
@pytest.mark.parametrize("stopped", [False, True])
def test_bot_model_child_must_be_same_organization_and_terminal(monkeypatch, status, node_status, stopped):
    with sqlite3.connect(":memory:") as db:
        for statement in (
            "CREATE TABLE compute_workers(revoked_at,draining)", "CREATE TABLE compute_jobs(status)",
            "CREATE TABLE llm_runs(organization_id,id,status)", "CREATE TABLE bot_runs(organization_id,id,status)",
            "CREATE TABLE workflow_node_attempts(organization_id,inference_run_id,bot_run_id,compute_job_id,kind,status)",
            "CREATE TABLE invitation_mail(state)", "CREATE TABLE bot_webhook_deliveries(state)",
        ):
            db.execute(statement)
        if status != "missing":
            db.execute("INSERT INTO bot_runs VALUES(?,'child',?)",
                       ("other" if status == "foreign_org" else "org", "failed" if status == "foreign_org" else status))
        db.execute("INSERT INTO workflow_node_attempts VALUES('org',NULL,'child',NULL,'llm',?)", (node_status,))

        @contextmanager
        def connection(_path):
            yield db

        monkeypatch.setattr(snapshots, "_connect", connection)
        if status in {"succeeded", "failed", "cancelled", "outcome_unknown"}:
            maintenance._idle(Path("unused"), local_workers_stopped=stopped)
        else:
            with pytest.raises(maintenance.MaintenanceError, match="workflow_execution"):
                maintenance._idle(Path("unused"), local_workers_stopped=stopped)


@pytest.mark.parametrize("retained", [False, True])
def test_cancelled_bot_workflow_migrates_unchanged_and_cannot_replay(organization, monkeypatch, retained):
    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.bot_store import BotStore
    from reasoning_ledger.server.store import StoreError
    from reasoning_ledger.server.workflow_runs import WorkflowRuns

    source, owner, topic, _, compute, worker = organization
    root, _, _, _, store = source
    user, org = owner["user_id"], owner["organization_id"]
    compute.drain_self(worker["token"])
    bots = BotStore(store)
    bot = bots.create(user, org, "Fixture", [topic], ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    definitions = WorkflowStore(store)
    saved = definitions.create(user, org, topic, "cancelled-bot-definition", {
        "format": 1, "name": "Cancelled bot", "nodes": [{"id": "model_1", "kind": "llm", "depends_on": [],
        "inputs": {}, "parameters": {"model": f"bot/{bot['id']}/default", "prompt": "Research", "max_completion_tokens": 128}}],
    })
    workflows = WorkflowRuns(definitions, compute, InferenceStore(store))
    workflow = workflows.start(user, org, saved["workflow_id"], saved["version"], "cancelled-bot-run")
    workflows.advance(workflow["id"])
    claim = runtime.claim(bot["token"])
    assert claim is not None
    workflows.cancel(user, org, workflow["id"], "cancel-before-upgrade")
    tables = ("bot_runs", "workflow_runs", "workflow_node_attempts", "workflow_run_actions", "workflow_run_events")
    with store.transaction() as db:
        assert db.execute("SELECT status FROM bot_runs").fetchone()[0] == "outcome_unknown"
        before = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in tables}
    stopped = stopped_snapshot(organization)
    archive_before = stopped[1].read_bytes()
    future(monkeypatch)
    if retained:
        namespace = {"snapshots": snapshots, "MaintenanceError": maintenance.MaintenanceError}
        # Comments, line wrapping and future flags may change bytecode while
        # retaining exactly the verified source guard's syntax tree.
        retained_source = "from __future__ import annotations\n" + RETAINED_STOPPED_WORKER_IDLE.replace(
            "    terminal =", "    # Protected target source.\n    terminal =", 1)
        monkeypatch.setitem(linecache.cache, "<retained-guard>",
                            (len(retained_source), None, retained_source.splitlines(True), "<retained-guard>"))
        exec(compile(retained_source, "<retained-guard>", "exec"), namespace)
        old_idle = namespace["_idle"]
        with pytest.raises(maintenance.MaintenanceError, match="workflow_execution"):
            old_idle(root, local_workers_stopped=True)
        monkeypatch.setattr(maintenance, "_idle", old_idle)

        def helper(command, arguments):
            monkeypatch.setattr(sys, "argv", ["-c", "--data-dir", str(root), command, *arguments])
            with redirect_stdout(io.StringIO()) as output, pytest.raises(SystemExit) as exited:
                exec(compile(MIGRATION_RUNNER, "<verified-runner>", "exec"), {})
            assert exited.value.code == 0 and maintenance._idle is old_idle
            return json.loads(output.getvalue())

        result = helper("migrate-offline", ["--backup", str(stopped[1]), "--key-file", str(stopped[2]),
                        "--expected-sha256", stopped[3]["sha256"], "--source-schema", str(len(MIGRATIONS)),
                        "--target-release", RELEASE, "--operation-id", OPERATION, "--confirm-source-stopped"])
        assert helper("maintenance-verify", ["--operation-id", OPERATION, "--target-release", RELEASE])["prepared_state_verified"]
    else:
        result = run(stopped)
        assert maintenance.verify_prepared(root, operation_id=OPERATION, target_release=RELEASE)["prepared_state_verified"]
    assert result["phase"] == "migrated" and stopped[1].read_bytes() == archive_before
    with sqlite3.connect(root / "server.sqlite3") as db:
        assert {table: db.execute("SELECT * FROM " + table).fetchall() for table in tables} == before
    migrations = maintenance.MIGRATIONS
    monkeypatch.setattr(store_module, "MIGRATIONS", migrations)
    with maintenance.serving(root, release=RELEASE, operation_id=OPERATION):
        recovered = Store(root)
        runtime = BotRuntime(BotStore(recovered))
        assert runtime.claim(bot["token"]) is None
        with pytest.raises(StoreError, match="lease is no longer active"):
            runtime.progress(bot["token"], claim["id"], claim["lease"], "thinking")
        # A late receipt only confirms the runner stopped; it settles the cancel and is never posted.
        assert runtime.finish(bot["token"], claim["id"], claim["lease"], "succeeded", "late answer")["status"] == "cancelled"
        with recovered.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM messages WHERE content='late answer'").fetchone()[0] == 0


def test_prepared_verification_needs_no_backup_key_and_never_advances_activation(source):
    result = run(source)
    source[1].unlink()
    source[2].unlink()
    before = (source[0] / maintenance.JOURNAL).read_bytes()
    verified = maintenance.verify_prepared(source[0], operation_id=OPERATION, target_release=RELEASE)
    expected = {key: value for key, value in result.items() if key not in {"backup_retained", "server_started", "activation_performed"}}
    assert verified == {**expected, "prepared_state_verified": True, "server_started": False}
    assert (source[0] / maintenance.JOURNAL).read_bytes() == before
    assert maintenance.journal(source[0])["phase"] == "migrated"


@pytest.mark.parametrize("change", ["operation", "release", "activated", "changed-data", "foreground-lease"])
def test_prepared_verification_refuses_differently_bound_activated_changed_or_owned_state(source, change):
    run(source)
    options = {"operation_id": OPERATION, "target_release": RELEASE}
    if change in {"operation", "release"}:
        options["operation_id" if change == "operation" else "target_release"] = "d" * 64
    elif change == "activated":
        with maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION):
            pass
    elif change == "changed-data":
        with sqlite3.connect(source[0] / "server.sqlite3") as db:
            db.execute(
                "INSERT INTO audit_events(organization_id,actor_id,action,target_id,created_at) VALUES(NULL,NULL,'changed','fixture',1)"
            )
    else:
        with (
            maintenance.serving(source[0], release=RELEASE, operation_id=OPERATION),
            pytest.raises(maintenance.MaintenanceError, match="owns"),
        ):
            maintenance.verify_prepared(source[0], **options)
        return
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.verify_prepared(source[0], **options)
