"""Authenticated, journaled offline migrations and foreground-server fencing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..deployment.host import InstallError, atomic_write, canonical
from ..deployment.private import protected_parent, read_private
from . import backup as snapshots
from .migrations import MIGRATIONS
from .schema_operations import sqlite_migration

JOURNAL = "server-maintenance.json"
LOCK = "server-maintenance.lock"
MIN_SOURCE_SCHEMA = 13
PHASES = {"validated", "migrating", "migrated", "activation_started", "completed"}
WORKER_LOCKS = ("inference-worker.lock", "workflow-worker.lock", "mail-worker.lock", "webhook-worker.lock", "initialization.lock")
INTENT_FIELDS = {
    "format",
    "operation_id",
    "backup_sha256",
    "backup_key_sha256",
    "source_schema",
    "target_schema",
    "target_release",
    "maximum_bytes",
    "source_state_sha256",
    "target_state_sha256",
}


class MaintenanceError(ValueError):
    """Fixed operator-facing errors without database contents or credentials."""


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _intent(value):
    """Validate the phase-independent identity shared with rollback provenance."""
    if (
        not isinstance(value, dict)
        or set(value) != INTENT_FIELDS
        or type(value["format"]) is not int
        or value["format"] != 1
        or any(
            not _hex(value[key])
            for key in (
                "operation_id",
                "backup_sha256",
                "backup_key_sha256",
                "target_release",
                "source_state_sha256",
                "target_state_sha256",
            )
        )
        or type(value["source_schema"]) is not int
        or type(value["target_schema"]) is not int
        or not MIN_SOURCE_SCHEMA <= value["source_schema"] <= value["target_schema"] <= 1000
        or type(value["maximum_bytes"]) is not int
        or not 1024**2 <= value["maximum_bytes"] <= snapshots.MAX_BYTES
    ):
        raise MaintenanceError("maintenance intent is malformed or differently bound")
    return value


def journal(root: Path):
    path = root / JOURNAL
    if not path.exists() and not path.is_symlink():
        return None
    try:
        body = read_private(path, 8192)
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or set(value) != INTENT_FIELDS | {"phase"}
            or canonical(value) != body
            or not isinstance(value["phase"], str)
            or value["phase"] not in PHASES
        ):
            raise ValueError
        _intent({key: value[key] for key in INTENT_FIELDS})
        return value
    except (OSError, ValueError, TypeError, KeyError, RecursionError, InstallError) as exc:
        raise MaintenanceError("maintenance journal is absent, changed, malformed, or insecure; retain recovery inputs") from exc


def assert_initialization_allowed(root: Path):
    from .rollback import assert_initialization_allowed as rollback_allowed

    rollback_allowed(root)
    value = journal(root)
    from .recovery import assert_maintenance_allowed

    assert_maintenance_allowed(root, value)
    if value is not None and value["phase"] not in {"activation_started", "completed"}:
        raise MaintenanceError("unfinished offline maintenance refuses application initialization; resume the original operation")
    if value is not None and value["target_schema"] != len(MIGRATIONS):
        raise MaintenanceError("a different maintenance target schema refuses implicit application migrations")
    if value is not None and value["phase"] == "completed":
        _history(root, value, publish=False)


@contextmanager
def _lock(root: Path, name: str, *, exclusive: bool, create: bool):
    snapshots._directory(root)
    path = root / name
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size != 0:
            raise MaintenanceError("maintenance requires an original private regular lock")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceError("another server, initializer, or execution worker owns this installation") from exc
        yield
    finally:
        os.close(fd)


def _quote(name):
    # Names are taken only from a verified built-in schema, never from users.
    return '"' + name.replace('"', '""') + '"'


def _logical_digest(db, maximum):
    digest = hashlib.sha256()
    total = 0
    tables = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
    for name, sql in tables:
        columns = db.execute("PRAGMA table_info(" + _quote(name) + ")").fetchall()
        names = [row[1] for row in columns]
        digest.update(canonical({"table": name, "columns": names, "sql": sql}))
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ",".join(map(_quote, primary)) if "WITHOUT ROWID" in sql.upper() else "rowid"
        for row in db.execute("SELECT " + ",".join(map(_quote, names)) + " FROM " + _quote(name) + " ORDER BY " + order):
            values = []
            for value in row:
                if isinstance(value, bytes):
                    if len(value) > 8 * 1024**2:
                        raise MaintenanceError("migration row exceeds its comparison allowance")
                    values.append({"blob": value.hex()})
                elif isinstance(value, float):
                    if not math.isfinite(value):
                        raise MaintenanceError("migration contains nonfinite database values")
                    values.append({"float": value.hex()})
                else:
                    if isinstance(value, str) and len(value) > 8 * 1024**2:
                        raise MaintenanceError("migration row exceeds its comparison allowance")
                    values.append(value)
            body = canonical(values)
            total += len(body)
            if len(body) > 16 * 1024**2 or total > maximum * 4:
                raise MaintenanceError("migration comparison exceeds its byte allowance")
            digest.update(body)
    return digest.hexdigest()


def state_identity(root, schema, maximum):
    chunks = snapshots._validate_state(root, maximum, schema_version=schema)
    with snapshots._connect(root / "server.sqlite3") as db:
        logical = _logical_digest(db, maximum)
    files = {}
    for name in ("encryption.key", "setup-token"):
        files[name] = snapshots._hash_file(root / name, 4096)
    mail = root / "mail.json"
    if mail.exists() or mail.is_symlink():
        files["mail.json"] = snapshots._hash_file(mail, 8192)
    return hashlib.sha256(canonical({"schema": schema, "database": logical, "files": files, "chunks": chunks})).hexdigest()


def _idle(root, *, local_workers_stopped=False):
    # This flag is used only under exclusive maintenance AND worker leases.
    # Persisted local execution labels are evidence, not running processes;
    # normal startup recovery retains uncertain billing without replaying them.
    terminal = "'succeeded','failed','cancelled','interrupted'"
    local_states = terminal + ",'queued','running'" if local_workers_stopped else terminal
    queries = [
        ("compute_workers", "SELECT 1 FROM compute_workers WHERE revoked_at IS NULL AND draining=0 LIMIT 1"),
        ("compute_jobs", "SELECT 1 FROM compute_jobs WHERE status IN ('running','outcome_unknown') LIMIT 1"),
        ("workflow_execution", "SELECT 1 FROM workflow_node_attempts a LEFT JOIN llm_runs r "
         "ON r.organization_id=a.organization_id AND r.id=a.inference_run_id "
         "WHERE a.status IN ('running','outcome_unknown') AND "
         "(a.kind!='llm' OR r.status IS NULL OR r.status NOT IN (" + local_states + ")) LIMIT 1"),
    ]
    if not local_workers_stopped:
        queries += [
            ("inference", "SELECT 1 FROM llm_runs WHERE status='running' LIMIT 1"),
            ("mail", "SELECT 1 FROM invitation_mail WHERE state='sending' LIMIT 1"),
            ("webhooks", "SELECT 1 FROM bot_webhook_deliveries WHERE state='sending' LIMIT 1"),
        ]
    with snapshots._connect(root / "server.sqlite3") as db:
        # Bot-backed model nodes use a different child queue. Terminal bot
        # receipts, including unknown outcomes, fence further effects and are
        # never claimed again. Preserve that evidence rather than settling it.
        if "bot_run_id" in {row[1] for row in db.execute("PRAGMA table_info(workflow_node_attempts)")}:
            queries[2] = (
                "workflow_execution", "SELECT 1 FROM workflow_node_attempts a "
                "LEFT JOIN llm_runs r ON r.organization_id=a.organization_id AND r.id=a.inference_run_id "
                "LEFT JOIN bot_runs b ON b.organization_id=a.organization_id AND b.id=a.bot_run_id "
                "WHERE a.status IN ('running','outcome_unknown') AND (a.kind!='llm' OR a.compute_job_id IS NOT NULL OR NOT ("
                "(a.bot_run_id IS NULL AND a.inference_run_id IS NOT NULL AND COALESCE(r.status,'') IN (" + local_states + ")) OR "
                "(a.inference_run_id IS NULL AND a.bot_run_id IS NOT NULL AND COALESCE(b.status,'') IN "
                "('succeeded','failed','cancelled','outcome_unknown')))) LIMIT 1",
            )
        for category, query in queries:
            if db.execute(query).fetchone():
                raise MaintenanceError("offline migration requires drained workers and settled in-flight execution: " + category)


def _migrate_database(root, source):
    with snapshots._connect(root / "server.sqlite3", writable=True) as db:
        # Rebuilds retain parent keys and check all foreign keys before commit.
        # SQLite requires this connection-local setting before BEGIN.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        snapshots._schema(db, schema_version=source)
        for index in range(source, len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                sqlite_migration(db, statement)
            db.execute(f"PRAGMA user_version={index + 1}")
        snapshots._schema(db)


def _version(root):
    with snapshots._connect(root / "server.sqlite3") as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _retained(archive, key_file, expected, key_digest, maximum):
    if (
        hashlib.sha256(snapshots._key(key_file)).hexdigest() != key_digest
        or snapshots._hash_file(archive, snapshots._archive_limit(maximum))[1] != expected
    ):
        raise MaintenanceError("original backup or key changed; retain original recovery inputs and maintenance journal")


def migrate(
    root: Path,
    archive: Path,
    key_file: Path,
    expected: str,
    *,
    source_schema: int,
    target_release: str,
    operation_id: str,
    source_stopped: bool = False,
    maximum: int = snapshots.DEFAULT_BYTES,
):
    if source_stopped is not True:
        raise MaintenanceError(
            "offline migration requires explicit confirmation that servers, workers, and outbound execution are stopped or fenced"
        )
    if (
        type(source_schema) is not int
        or not MIN_SOURCE_SCHEMA <= source_schema <= len(MIGRATIONS)
        or not all(map(_hex, (expected, target_release, operation_id)))
    ):
        raise MaintenanceError("invalid exact source schema, backup, operation, or target release binding")
    snapshots._allowance(maximum)
    root = root.absolute()
    try:
        with ExitStack() as locks:
            locks.enter_context(_lock(root, LOCK, exclusive=True, create=True))
            for name in WORKER_LOCKS:
                path = root / name
                if path.exists() or path.is_symlink():
                    locks.enter_context(_lock(root, name, exclusive=True, create=False))
            current = journal(root)
            from . import recovery, rollback

            recovery.assert_maintenance_allowed(root, current)

            rollback.preflight_migration(
                root, current, source_schema=source_schema, target_release=target_release, operation_id=operation_id
            )
            if current is not None and current["phase"] == "completed":
                _history(root, current, publish=False)
                if current["operation_id"] == operation_id:
                    raise MaintenanceError("completed migration cannot replay its old snapshot")
                if current["target_schema"] != source_schema or current["target_release"] == target_release:
                    raise MaintenanceError("next migration requires the current completed schema and a different target release")
                current = None
            if current is not None:
                required = {
                    "operation_id": operation_id,
                    "backup_sha256": expected,
                    "source_schema": source_schema,
                    "target_schema": len(MIGRATIONS),
                    "target_release": target_release,
                    "maximum_bytes": maximum,
                }
                if any(current[key] != item for key, item in required.items()):
                    raise MaintenanceError("migration recovery requires its original exact backup, source, operation and release")
                if current["phase"] == "activation_started":
                    raise MaintenanceError("activation may have external effects; offline migration cannot replay or restore old state")
            # Plaintext stays in a service-private bounded scratch tree. No
            # restore fencing/revocation is applied to a normal upgrade clone.
            protected_parent(archive.absolute())
            with tempfile.TemporaryDirectory(prefix=".reasoning-migration-", dir=archive.absolute().parent) as temporary:
                stage = Path(temporary)
                key = snapshots._key(key_file)
                key_digest = hashlib.sha256(key).hexdigest()
                snapshots._unpack(archive.absolute(), key, expected, stage, maximum, schema_version=source_schema)
                # Mail configuration is installation-local and not in archives.
                mail = root / "mail.json"
                if mail.exists() or mail.is_symlink():
                    atomic_write(stage / "mail.json", read_private(mail, 8192))
                _idle(stage, local_workers_stopped=True)
                original = state_identity(stage, source_schema, maximum)
                _migrate_database(stage, source_schema)
                target = state_identity(stage, len(MIGRATIONS), maximum)
                intent = {
                    "format": 1,
                    "operation_id": operation_id,
                    "backup_sha256": expected,
                    "backup_key_sha256": key_digest,
                    "source_schema": source_schema,
                    "target_schema": len(MIGRATIONS),
                    "target_release": target_release,
                    "maximum_bytes": maximum,
                    "source_state_sha256": original,
                    "target_state_sha256": target,
                }
                if current is not None and any(current[key] != value for key, value in intent.items()):
                    raise MaintenanceError("recovered migration source/configuration or dry-run result differs; preserve displaced state")
                version = _version(root)
                identity = state_identity(root, version, maximum)
                _retained(archive, key_file, expected, key_digest, maximum)
                if current is None:
                    if version != source_schema or identity != original:
                        raise MaintenanceError("encrypted snapshot does not match the exact stopped installation state")
                    current = {**intent, "phase": "validated"}
                    atomic_write(root / JOURNAL, canonical(current))
                _idle(root, local_workers_stopped=True)
                if current["phase"] == "validated":
                    if version != source_schema or identity != original:
                        raise MaintenanceError("installation changed after migration validation")
                    current["phase"] = "migrating"
                    atomic_write(root / JOURNAL, canonical(current))
                rollback.supersede_for_migration(root, current)
                if current["phase"] == "migrating":
                    if version == source_schema and identity == original:
                        _migrate_database(root, source_schema)
                    elif version != len(MIGRATIONS) or identity != target:
                        raise MaintenanceError("interrupted migration has unrecognized state; no automatic restoration is allowed")
                    if state_identity(root, len(MIGRATIONS), maximum) != target:
                        raise MaintenanceError("migrated state differs from the authenticated dry run; retain backup and journal")
                    _retained(archive, key_file, expected, key_digest, maximum)
                    current["phase"] = "migrated"
                    atomic_write(root / JOURNAL, canonical(current))
                elif version != len(MIGRATIONS) or identity != target:
                    raise MaintenanceError("prepared migration state changed before activation")
                _retained(archive, key_file, expected, key_digest, maximum)
                return {**current, "backup_retained": True, "server_started": False, "activation_performed": False}
    except (OSError, sqlite3.Error, snapshots.BackupError, InstallError) as exc:
        raise MaintenanceError(
            "offline migration failed; retain original backup/key and maintenance journal; no server was started"
        ) from exc


@contextmanager
def serving(root: Path, *, release: str | None = None, operation_id: str | None = None):
    root = root.absolute()
    # Create only the final private directory, not an unbounded parent tree.
    protected_parent(root, writable=False)
    root.mkdir(mode=0o700, exist_ok=True)
    with _lock(root, LOCK, exclusive=False, create=True):
        from .rollback import assert_serving_allowed

        assert_serving_allowed(root, release)
        database = root / "server.sqlite3"
        if database.exists() or database.is_symlink():
            try:
                with snapshots._input(database, snapshots.MAX_BYTES), snapshots._connect(database) as db:
                    snapshots._schema(db)
            except (snapshots.BackupError, OSError, sqlite3.Error, InstallError) as exc:
                raise MaintenanceError(
                    "foreground startup requires the exact target schema; use explicit initialization or offline migration"
                ) from exc
        current = journal(root)
        from .recovery import assert_maintenance_allowed as recovery_allowed

        recovery_allowed(root, current)
        if current is not None:
            if current["phase"] == "completed":
                if (
                    release != current["target_release"]
                    or current["target_schema"] != len(MIGRATIONS)
                    or operation_id not in {None, current["operation_id"]}
                ):
                    raise MaintenanceError("completed maintenance requires the current exact target release")
                _history(root, current, publish=False)
                yield
                return
            if (
                not _hex(operation_id)
                or not _hex(release)
                or current["operation_id"] != operation_id
                or current["target_release"] != release
                or current["target_schema"] != len(MIGRATIONS)
            ):
                raise MaintenanceError("server startup requires the original maintenance operation and exact target release")
            if current["phase"] == "migrated":
                if state_identity(root, len(MIGRATIONS), current["maximum_bytes"]) != current["target_state_sha256"]:
                    raise MaintenanceError("prepared installation changed before activation; server startup refused")
                # Write the external-effect boundary BEFORE constructing the
                # application or starting model/mail/workflow/webhook workers.
                current["phase"] = "activation_started"
                atomic_write(root / JOURNAL, canonical(current))
            elif current["phase"] != "activation_started":
                raise MaintenanceError("unfinished migration refuses server startup; resume the original operation")
        elif operation_id is not None:
            raise MaintenanceError("server startup cannot invent a missing maintenance journal")
        yield


def _history(root, value, *, publish):
    directory = root / "maintenance-history"
    if publish:
        directory.mkdir(mode=0o700, exist_ok=True)
    snapshots._directory(directory)
    path = directory / (value["operation_id"] + ".json")
    expected = canonical(value)
    if path.exists() or path.is_symlink():
        if read_private(path, 8192) != expected:
            raise MaintenanceError("completed maintenance history differs; refusing to overwrite recovery evidence")
    elif publish:
        atomic_write(path, expected)
    else:
        raise MaintenanceError("completed maintenance history is missing; preserve the original operation")


def verify_prepared(root: Path, *, operation_id: str, target_release: str):
    if not _hex(operation_id) or not _hex(target_release):
        raise MaintenanceError("prepared verification requires exact operation and target release")
    root = root.absolute()
    try:
        with ExitStack() as locks:
            locks.enter_context(_lock(root, LOCK, exclusive=True, create=False))
            for name in WORKER_LOCKS:
                path = root / name
                if path.exists() or path.is_symlink():
                    locks.enter_context(_lock(root, name, exclusive=True, create=False))
            current = journal(root)
            if (
                current is None
                or current["phase"] != "migrated"
                or current["operation_id"] != operation_id
                or current["target_release"] != target_release
                or current["target_schema"] != len(MIGRATIONS)
            ):
                raise MaintenanceError("prepared verification refuses missing, unfinished, activated or differently bound state")
            from .rollback import assert_maintenance_allowed

            assert_maintenance_allowed(root, current)
            from .recovery import assert_maintenance_allowed as recovery_allowed

            if not recovery_allowed(root, current):
                _idle(root, local_workers_stopped=True)
            if state_identity(root, len(MIGRATIONS), current["maximum_bytes"]) != current["target_state_sha256"]:
                raise MaintenanceError("prepared installation changed; preserve original operation and recovery inputs")
            return {**current, "prepared_state_verified": True, "server_started": False}
    except (OSError, sqlite3.Error, snapshots.BackupError, InstallError) as exc:
        raise MaintenanceError("prepared state verification failed; no server was started") from exc


def observe_activation(root: Path, *, operation_id: str, target_release: str):
    if not _hex(operation_id) or not _hex(target_release):
        raise MaintenanceError("activation observation requires exact operation and target release")
    root = root.absolute()
    try:
        with _lock(root, LOCK, exclusive=False, create=False):
            current = journal(root)
            if (
                current is None
                or current["operation_id"] != operation_id
                or current["target_release"] != target_release
                or current["target_schema"] != len(MIGRATIONS)
                or current["phase"] not in {"activation_started", "completed"}
            ):
                raise MaintenanceError("activation observation refuses missing, prepared or differently bound state")
            from .rollback import assert_maintenance_allowed

            assert_maintenance_allowed(root, current)
            from .recovery import assert_maintenance_allowed as recovery_allowed

            recovery_allowed(root, current)
            with snapshots._input(root / "server.sqlite3", snapshots.MAX_BYTES), snapshots._connect(root / "server.sqlite3") as database:
                snapshots._schema(database)
            if current["phase"] == "completed":
                _history(root, current, publish=False)
            return {
                **current,
                "activation_state_verified": True,
                "database_schema_verified": True,
                "server_started_by_command": False,
                "health_verified_by_command": False,
            }
    except (OSError, sqlite3.Error, snapshots.BackupError, InstallError) as exc:
        raise MaintenanceError("activation state observation failed; no server was started") from exc


def finish(root: Path, *, operation_id: str, target_release: str, activation_verified: bool = False):
    if activation_verified is not True or not _hex(operation_id) or not _hex(target_release):
        raise MaintenanceError("completion requires exact operation/release and explicit operator-verified activation")
    root = root.absolute()
    try:
        # This is a metadata acknowledgment, not another migration; a running
        # foreground server retains its shared lease. SQL/data is untouched.
        with _lock(root, LOCK, exclusive=False, create=False):
            current = journal(root)
            if (
                current is None
                or current["operation_id"] != operation_id
                or current["target_release"] != target_release
                or current["target_schema"] != len(MIGRATIONS)
                or current["phase"] not in {"activation_started", "completed"}
            ):
                raise MaintenanceError("completion requires the original target activation, not a prepared or different migration")
            from .rollback import assert_maintenance_allowed

            assert_maintenance_allowed(root, current)
            from .recovery import assert_maintenance_allowed as recovery_allowed

            recovery_allowed(root, current)
            if current["phase"] == "completed":
                _history(root, current, publish=False)
            current["phase"] = "completed"
            _history(root, current, publish=True)
            atomic_write(root / JOURNAL, canonical(current))
            return {
                **current,
                "activation_verified_by_operator": True,
                "health_verified_by_command": False,
                "services_changed": False,
                "backup_retention_verified_by_command": False,
            }
    except (OSError, InstallError, snapshots.BackupError) as exc:
        raise MaintenanceError("maintenance completion failed; retain original operation and recovery history") from exc
