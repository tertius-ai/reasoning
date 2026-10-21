"""Populated historical ledgers migrate without changing immutable evidence or recovery inputs.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import ast
import hashlib
import inspect
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout

import pytest

from reasoning_ledger.deployment.server_upgrade import MIGRATION_RUNNER
from reasoning_ledger.server import backup, maintenance, schema_operations
from reasoning_ledger.server import store as store_module
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.postgres import migration as postgres_migration
from reasoning_ledger.server.reaction_emoji_schema import SCHEMA as REACTION_EMOJI_SCHEMA
from reasoning_ledger.server.source_statements_schema import SCHEMA as INDEX_SCHEMA
from reasoning_ledger.server.store import Store
from reasoning_ledger.server.workbench_schema import SCHEMA as RECORD_SCHEMA
from reasoning_ledger.server.workbench_tools import MathematicalLedger


def test_embedded_operator_repair_matches_the_canonical_sqlite_operation():
    actual = ast.parse(inspect.getsource(schema_operations.repair_claim_index)).body[0]
    embedded = next(node for node in ast.parse(MIGRATION_RUNNER).body
                    if isinstance(node, ast.FunctionDef) and node.name == "repair_claim_index")
    assert ast.dump(actual) == ast.dump(embedded)
    assert INDEX_SCHEMA == (schema_operations.CLAIM_INDEX,)


def test_repair_preserves_exact_guard_and_atomic_rollback_on_failure():
    with sqlite3.connect(":memory:") as db:
        for statement in RECORD_SCHEMA[:4]:
            db.execute(statement)
        values = ("org", "claims/revision.json", "claim-revision", "ref_old", "clm_original",
                  None, None, '{"statement":"immutable evidence"}', "user", 1, "native")
        db.execute("INSERT INTO mathematical_records VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        # Prove that the published raw SQL fails on populated immutable history.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(schema_operations.CLAIM_INDEX)
        schema_operations.sqlite_migration(db, schema_operations.CLAIM_INDEX)
        row = tuple(db.execute("SELECT * FROM mathematical_records").fetchone())
        assert row[:3] + row[4:] == values[:3] + values[4:] and row[3] == "clm_original"
        assert db.execute("SELECT sql FROM sqlite_schema WHERE name='mathematical_revision_update'").fetchone()[0] == RECORD_SCHEMA[2]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE mathematical_records SET body_json='{}'")
        db.execute("CREATE TRIGGER fixture_failure BEFORE UPDATE ON mathematical_records "
                   "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            schema_operations.sqlite_migration(db, schema_operations.CLAIM_INDEX)
        assert tuple(db.execute("SELECT * FROM mathematical_records").fetchone()) == row
        assert db.execute("SELECT sql FROM sqlite_schema WHERE name='mathematical_revision_update'").fetchone()[0] == RECORD_SCHEMA[2]


def released_operation():
    """Simulate the exact supported schema-operation shape in a retained target archive."""
    source = inspect.getsource(schema_operations.sqlite_migration)
    branch = "    if statement == CLAIM_INDEX:\n        repair_claim_index(db)\n        return\n"
    # Later operations (the reaction emoji check) did not exist in that release either.
    later = ("    if statement == REACTION_EMOJI_DROP:\n        _reaction_check(db, \"\")\n        return\n"
             "    if statement == REACTION_EMOJI_ADD:\n"
             "        _reaction_check(db, statement[statement.index(\" CHECK(\"):])\n        return\n")
    assert branch in source and later in source
    namespace = dict(vars(schema_operations))
    exec(compile(source.replace(branch, "", 1).replace(later, "", 1), "<retained-schema-operation>", "exec"), namespace)
    operation = namespace["sqlite_migration"]
    assert operation.__code__.co_names == ("PROJECT_ARTIFACTS", "PROJECT_COMPUTE", "execute", "fetchone", "ValueError", "_project_compute")
    return operation


@pytest.mark.parametrize("retained_target", [False, True])
def test_schema_52_upgrade_with_populated_cited_claims_keeps_backup_and_resumes(tmp_path, monkeypatch, retained_target):
    root, archive, key = tmp_path / "state", tmp_path / "source.rnbk", tmp_path / "backup.key"
    with monkeypatch.context() as source:
        source.setattr(store_module, "MIGRATIONS", MIGRATIONS[:52])
        source.setattr(backup, "MIGRATIONS", MIGRATIONS[:52])
        store = Store(root)
        owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "populated-ledger-fixture-password", "Research")
        ledger = MathematicalLedger(store)
        user, org = owner["user_id"], owner["organization_id"]
        reference = ledger.execute(user, org, "ledger_reference", {"author": "Author", "title": "Retained source"},
                                   client_id="migration-ref")
        claim = ledger.execute(user, org, "ledger_claim", {"statement": "An existing cited statement",
                               "reference_id": reference["reference_id"],
                               "locator": "Theorem 1"}, client_id="migration-claim")
        ledger.execute(user, org, "ledger_refine", {"claim_id": claim["claim_id"], "statement": "An existing refined statement"},
                       client_id="migration-refine")
        with store.transaction(write=True) as db:
            # Historical writers indexed these rows by their reference rather than their claim.
            db.execute("DROP TRIGGER mathematical_revision_update")
            db.execute("UPDATE mathematical_records SET object_id=? WHERE claim_id=?", (reference["reference_id"], claim["claim_id"]))
            db.execute(RECORD_SCHEMA[2])
            before = [tuple(row) for row in db.execute("SELECT * FROM mathematical_records ORDER BY path")]
        backup.create_key(key)
        original = backup.backup(root, archive, key)
    original_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
    original_key = (root / "encryption.key").read_bytes()
    release, operation = "a" * 64, "b" * 64
    options = {"source_schema": 52, "target_release": release, "operation_id": operation, "source_stopped": True}
    # A retained release that predates later schema operations also predates their migrations.
    target_migrations = MIGRATIONS[:MIGRATIONS.index(REACTION_EMOJI_SCHEMA)] if retained_target else MIGRATIONS
    with monkeypatch.context() as target:
        for module in (store_module, backup, maintenance):
            target.setattr(module, "MIGRATIONS", target_migrations)
        if retained_target:
            old_operation = released_operation()
            for module in (schema_operations, maintenance, backup):
                target.setattr(module, "sqlite_migration", old_operation)
            target.setattr(sys, "argv", ["-c", "--data-dir", str(root), "migrate-offline", "--backup", str(archive),
                                        "--key-file", str(key), "--expected-sha256", original["sha256"], "--source-schema", "52",
                                        "--target-release", release, "--operation-id", operation, "--confirm-source-stopped"])
            with redirect_stdout(io.StringIO()) as output, pytest.raises(SystemExit) as exited:
                exec(compile(MIGRATION_RUNNER, "<verified-migration-runner>", "exec"), {})
            assert exited.value.code == 0
            result = json.loads(output.getvalue())
        else:
            result = maintenance.migrate(root, archive, key, original["sha256"], **options)
        assert result.get("phase") == "migrated", result
        assert maintenance.migrate(root, archive, key, original["sha256"], **options) == result
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_archive
    assert (root / "encryption.key").read_bytes() == original_key
    with sqlite3.connect(root / "server.sqlite3") as db:
        after = db.execute("SELECT * FROM mathematical_records ORDER BY path").fetchall()
        assert len(after) == len(before)
        for old, new in zip(before, after, strict=True):
            assert old[:3] + old[4:] == new[:3] + new[4:]
            assert new[3] == (claim["claim_id"] if old[2] in {"claim-head", "claim-revision"} else old[3])
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(target_migrations)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE mathematical_records SET body_json='{}' WHERE record_type='claim-revision'")


def test_postgresql_index_repair_brackets_the_derived_metadata_update():
    assert postgres_migration(schema_operations.CLAIM_INDEX) == [
        "ALTER TABLE mathematical_records DISABLE TRIGGER mathematical_revision_update", schema_operations.CLAIM_INDEX,
        "ALTER TABLE mathematical_records ENABLE TRIGGER mathematical_revision_update",
    ]
