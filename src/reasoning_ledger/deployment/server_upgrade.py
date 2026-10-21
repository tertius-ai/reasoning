"""Root-owned, recoverable preparation and activation of a confirmed upgrade.

Preparation stops/fences the source and migrates offline state. Activation is
separately confirmed and may have external effects. Rollback retains displaced
state and separately activates a matching, explicitly confirmed source restoration.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

try:
    from . import host, server_operations
except ImportError:
    import importlib.util

    installer_specification = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("installer.py"))
    if installer_specification is None or installer_specification.loader is None:
        raise RuntimeError("verified installer tooling is unavailable") from None
    host = importlib.util.module_from_spec(installer_specification)
    sys.modules["installer"] = host
    installer_specification.loader.exec_module(host)

    specification = importlib.util.spec_from_file_location("server_operations", Path(__file__).with_name("server-operations.py"))
    if specification is None or specification.loader is None:
        raise host.InstallError("verified server operations tooling is unavailable") from None
    server_operations = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(server_operations)

MAX_BACKUP = 65 * 1024**3
PHASES = {"preparing", "target_ready", "fencing", "source_stopped", "backup_retained", "migrating", "sealing", "prepared"}
ACTIVATION_PHASES = {"switching", "activating", "ready", "finalizing", "completed"}
ROLLBACK_ACTIVATION_PHASES = {"publishing", "switching", "activating", "ready", "finalizing", "completed"}
FENCE = "var/lib/reasoning-deploy/server-upgrade-fence"
DROPIN = "etc/systemd/system/reasoning-server.service.d/90-reasoning-upgrade.conf"
FENCE_UNIT = host.MANAGED + "[Unit]\nConditionPathExists=!/var/lib/reasoning-deploy/server-upgrade-fence\n"
IDENTITY_FIELDS = ("format", "operation", "source", "target", "source_database_schema", "target_database_schema", "configuration")

# Only fixed diagnoses may cross the SSH boundary. Exception strings, paths,
# SQL, record contents and child stderr are never diagnostic payloads.
MIGRATION_FAILURE_MESSAGES = {
    "offline migration requires drained workers and settled in-flight execution": "inflight_execution",
    "another server, initializer, or execution worker owns this installation": "maintenance_locked",
    "backup database schema differs from this release": "schema_mismatch",
    "backup requires this release's exact database schema; use the matching release": "schema_version_mismatch",
    "backup database integrity check failed": "database_integrity",
    "migration recovery requires its original exact backup, source, operation and release": "retained_input_mismatch",
    "recovered migration source/configuration or dry-run result differs; preserve displaced state": "dry_run_mismatch",
    "encrypted snapshot does not match the exact stopped installation state": "snapshot_state_mismatch",
    "installation changed after migration validation": "source_state_changed",
    "interrupted migration has unrecognized state; no automatic restoration is allowed": "interrupted_state_mismatch",
    "migrated state differs from the authenticated dry run; retain backup and journal": "dry_run_mismatch",
    "claim index repair requires the exact immutable revision guard": "claim_guard_mismatch",
    "migration comparison exceeds its byte allowance": "comparison_allowance",
    "migration row exceeds its comparison allowance": "comparison_allowance",
    "maintenance journal is absent, changed, malformed, or insecure; retain recovery inputs": "maintenance_journal_invalid",
    "rollback state record is malformed, insecure or differently bound; preserve recovery evidence": "rollback_provenance_invalid",
}
MIGRATION_FAILURE_MESSAGES.update({
    "offline migration requires drained workers and settled in-flight execution: " + category: "inflight_" + category
    for category in ("compute_workers", "compute_jobs", "workflow_execution", "inference", "mail", "webhooks")
})
MIGRATION_FAILURE_REASONS = {
    **{code: code.replace("_", " ") for code in MIGRATION_FAILURE_MESSAGES.values()},
    "disk_full": "data filesystem is full (or its quota is exhausted)",
    "permission_denied": "migration cannot access a required protected file",
    "filesystem_readonly": "data filesystem is read-only",
    "sqlite_busy": "database is locked by another process",
    "sqlite_error": "SQLite rejected the migration; no database contents are exposed",
    "migration_failed": "offline migration failed; no more specific safe diagnosis is available",
    "helper_failed": "migration helper exited without a verified result",
    "helper_killed": "migration helper was killed; memory exhaustion is one possible cause",
    "helper_timeout": "migration helper exceeded its execution deadline",
    "inflight_compute_workers": "external compute workers must be drained or fenced before migration",
    "inflight_compute_jobs": "external computation is running or has an unresolved outcome",
    "inflight_workflow_execution": "a workflow has unresolved non-LLM execution or an unverified model child",
}


class MigrationFailure(host.InstallError):
    def __init__(self, reason):
        if not isinstance(reason, str) or reason not in MIGRATION_FAILURE_REASONS:
            raise host.InstallError("invalid migration failure diagnosis")
        self.reason = reason
        super().__init__("offline migration refused: " + MIGRATION_FAILURE_REASONS[reason])


class CommandFailure(host.InstallError):
    def __init__(self, returncode):
        self.returncode = returncode
        message = "upgrade command timed out" if returncode is None else "upgrade command failed"
        super().__init__(message + "; retain original operation and inspect private host diagnostics")


def migration_failure_reason(error):
    if isinstance(error, MigrationFailure):
        return error.reason if isinstance(error.reason, str) and error.reason in MIGRATION_FAILURE_REASONS else None
    if isinstance(error, CommandFailure):
        if error.returncode is None:
            return "helper_timeout"
        return "helper_killed" if error.returncode in {-9, 137} else "helper_failed"
    if isinstance(error, OSError):
        return {errno.ENOSPC: "disk_full", errno.EDQUOT: "disk_full", errno.EACCES: "permission_denied",
                errno.EPERM: "permission_denied", errno.EROFS: "filesystem_readonly"}.get(error.errno)
    return None


RETAINED_IDLE_QUERIES = (
    "SELECT 1 FROM compute_workers WHERE revoked_at IS NULL AND draining=0 LIMIT 1",
    "SELECT 1 FROM compute_jobs WHERE status IN ('running','outcome_unknown') LIMIT 1",
    "SELECT 1 FROM llm_runs WHERE status='running' LIMIT 1",
    "SELECT 1 FROM workflow_node_attempts a LEFT JOIN llm_runs r "
    "ON r.organization_id=a.organization_id AND r.id=a.inference_run_id "
    "WHERE a.status='running' OR (a.status='outcome_unknown' AND "
    "(a.kind!='llm' OR r.status IS NULL OR r.status NOT IN "
    "('succeeded','failed','cancelled','interrupted'))) LIMIT 1",
    "SELECT 1 FROM invitation_mail WHERE state='sending' LIMIT 1",
    "SELECT 1 FROM bot_webhook_deliveries WHERE state='sending' LIMIT 1",
)
RETAINED_STOPPED_WORKER_IDLE = """def _idle(root, *, local_workers_stopped=False):
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
        for category, query in queries:
            if db.execute(query).fetchone():
                raise MaintenanceError("offline migration requires drained workers and settled in-flight execution: " + category)
"""

STOPPED_WORKER_IDLE = """def _idle(root, *, local_workers_stopped=False):
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
"""

# Run inside the verified target venv as the service user. Compatibility recognizes
# raw-SQL and offline-worker executors; all target locks, dry runs, provenance,
# backups and activation guards remain in its original maintenance command.
MIGRATION_RUNNER = """from reasoning_ledger.server import maintenance as maintenance
from reasoning_ledger.server.cli import main
def repair_claim_index(db):
    \"\"\"Repair derived lookup metadata atomically; retain immutable bodies and the exact guard.\"\"\"
    from reasoning_ledger.server.source_statements_schema import SCHEMA as index_schema
    from reasoning_ledger.server.workbench_schema import SCHEMA as records_schema

    repair_sql = ("UPDATE mathematical_records SET object_id=claim_id "
                  "WHERE record_type IN ('claim-head','claim-revision') AND claim_id IS NOT NULL")
    if index_schema != (repair_sql,):
        raise ValueError("unrecognized claim index repair operation")
    expected = records_schema[2].strip()
    guard = db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' "
                       "AND name='mathematical_revision_update' AND tbl_name='mathematical_records'").fetchone()
    if guard is None or guard[0] != expected:
        raise ValueError("claim index repair requires the exact immutable revision guard")
    db.execute("SAVEPOINT reasoning_claim_index")
    try:
        db.execute("DROP TRIGGER mathematical_revision_update")
        db.execute(index_schema[0])
        db.execute(guard[0])
    except BaseException:
        db.execute("ROLLBACK TO reasoning_claim_index")
        raise
    finally:
        db.execute("RELEASE reasoning_claim_index")

index_repair = ("UPDATE mathematical_records SET object_id=claim_id "
                "WHERE record_type IN ('claim-head','claim-revision') AND claim_id IS NOT NULL")
if any(statement == index_repair for migration in maintenance.MIGRATIONS for statement in migration):
    from reasoning_ledger.server import schema_operations
    operation_names = schema_operations.sqlite_migration.__code__.co_names
    if 'repair_claim_index' not in operation_names:
        if operation_names != ('PROJECT_ARTIFACTS', 'PROJECT_COMPUTE', 'execute', 'fetchone', 'ValueError', '_project_compute'):
            raise SystemExit('unrecognized schema-operation executor; retain the pending upgrade')
        original_operation = schema_operations.sqlite_migration
        def checked_operation(db, statement):
            if statement == index_repair:
                repair_claim_index(db)
            else:
                original_operation(db, statement)
        schema_operations.sqlite_migration = checked_operation
        maintenance.sqlite_migration = checked_operation
        maintenance.snapshots.sqlite_migration = checked_operation
names = maintenance._migrate_database.__code__.co_names
rebuilds = {'ALTER TABLE research_artifacts ALTER COLUMN topic_id DROP NOT NULL',
            'ALTER TABLE compute_jobs ALTER COLUMN topic_id DROP NOT NULL'}
if any(statement in rebuilds for migration in maintenance.MIGRATIONS for statement in migration) and 'sqlite_migration' not in names:
    if names != ('snapshots', '_connect', 'execute', '_schema', 'range', 'len', 'MIGRATIONS'):
        raise SystemExit('unrecognized migration executor; retain the pending upgrade')
    from reasoning_ledger.server.schema_operations import sqlite_migration
    def migrate_database(root, source):
        with maintenance.snapshots._connect(root / 'server.sqlite3', writable=True) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            maintenance.snapshots._schema(db, schema_version=source)
            for index in range(source, len(maintenance.MIGRATIONS)):
                for statement in maintenance.MIGRATIONS[index]:
                    sqlite_migration(db, statement)
                db.execute(f'PRAGMA user_version={index + 1}')
            maintenance.snapshots._schema(db)
    maintenance._migrate_database = migrate_database
"""
MIGRATION_RUNNER += "retained_idle_queries = " + repr(RETAINED_IDLE_QUERIES) + "\n"
MIGRATION_RUNNER += "retained_stopped_worker_idle = " + repr(RETAINED_STOPPED_WORKER_IDLE) + "\n"
MIGRATION_RUNNER += "current_stopped_worker_idle = " + repr(STOPPED_WORKER_IDLE) + "\n"
MIGRATION_RUNNER += "import sys\nsnapshots = maintenance.snapshots\nMaintenanceError = maintenance.MaintenanceError\n" + STOPPED_WORKER_IDLE
MIGRATION_RUNNER += """original_idle = maintenance._idle
if 'local_workers_stopped' in original_idle.__code__.co_varnames:
    import ast, inspect
    try:
        original_guard = ast.dump(ast.parse(inspect.getsource(original_idle)))
    except (OSError, TypeError, SyntaxError):
        raise SystemExit('unrecognized offline worker guard; retain the pending upgrade') from None
    if original_guard == ast.dump(ast.parse(retained_stopped_worker_idle)):
        if (not any(mode in sys.argv[1:] for mode in ('migrate-offline', 'maintenance-verify'))
                or not {'_lock', 'LOCK', 'WORKER_LOCKS'} <= set(maintenance.migrate.__code__.co_names)
                or not {'_lock', 'LOCK', 'WORKER_LOCKS'} <= set(maintenance.verify_prepared.__code__.co_names)):
            raise SystemExit('unrecognized offline worker guard; retain the pending upgrade')
        maintenance._idle = _idle
    elif original_guard != ast.dump(ast.parse(current_stopped_worker_idle)):
        raise SystemExit('unrecognized offline worker guard; retain the pending upgrade')
else:
    if (not any(mode in sys.argv[1:] for mode in ('migrate-offline', 'maintenance-verify'))
            or original_idle.__code__.co_names != ('snapshots', '_connect', 'execute', 'fetchone', 'MaintenanceError')
            or retained_idle_queries not in original_idle.__code__.co_consts
            or not {'_lock', 'LOCK', 'WORKER_LOCKS'} <= set(maintenance.migrate.__code__.co_names)
            or not {'_lock', 'LOCK', 'WORKER_LOCKS'} <= set(maintenance.verify_prepared.__code__.co_names)):
        raise SystemExit('unrecognized offline worker guard; retain the pending upgrade')
    def stopped_idle(root, *, local_workers_stopped=False):
        return _idle(root, local_workers_stopped=True)
    maintenance._idle = stopped_idle
"""
MIGRATION_RUNNER += "migration_failure_messages = " + repr(MIGRATION_FAILURE_MESSAGES) + "\n"
MIGRATION_RUNNER += """import errno, sqlite3
from reasoning_ledger.server import cli as server_cli
original_migrate = server_cli.migrate
def diagnosed_migrate(*args, **kwargs):
    try:
        return original_migrate(*args, **kwargs)
    except Exception as error:
        reason = 'migration_failed'
        seen = set()
        for _ in range(16):
            if error is None or id(error) in seen:
                break
            seen.add(id(error))
            if isinstance(error, OSError):
                code = {errno.ENOSPC: 'disk_full', errno.EDQUOT: 'disk_full', errno.EACCES: 'permission_denied',
                        errno.EPERM: 'permission_denied', errno.EROFS: 'filesystem_readonly'}.get(error.errno)
                if code:
                    reason = code
                    break
            if isinstance(error, ValueError) and str(error) in migration_failure_messages:
                reason = migration_failure_messages[str(error)]
                break
            if isinstance(error, sqlite3.Error):
                code = getattr(error, 'sqlite_errorcode', None)
                busy = type(code) is int and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                reason = 'sqlite_busy' if busy else 'sqlite_error'
            error = error.__cause__ if error.__cause__ is not None else error.__context__
        return {'format': 1, 'kind': 'upgrade_migration_failure', 'reason': reason}
server_cli.migrate = diagnosed_migrate
try:
    raise SystemExit(main())
finally:
    server_cli.migrate = original_migrate
    maintenance._idle = original_idle
"""


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _pairs(rows):
    value = dict(rows)
    if len(rows) != len(value):
        raise ValueError
    return value


def bounded_command(args, *, check=True, timeout=1200):
    """Never expose child stderr; bound stdout and reap the owned process group."""
    if type(timeout) not in {int, float} or not 0 < timeout <= 1200:
        raise host.InstallError("invalid upgrade command deadline")
    deadline = time.monotonic() + timeout
    body = bytearray()
    with subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=host.ENVIRONMENT,
        umask=0o022,
        start_new_session=True,
    ) as child:
        complete = False
        try:
            assert child.stdout is not None
            os.set_blocking(child.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise CommandFailure(None)
                    for _, _events in selector.select(min(1, max(0, deadline - time.monotonic()))):
                        block = os.read(child.stdout.fileno(), 65536)
                        body.extend(block)
                        if len(body) > 1024**2:
                            raise host.InstallError("upgrade command output exceeds its allowance; retain original operation")
                        if not block:
                            selector.unregister(child.stdout)
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if check and child.returncode:
                raise CommandFailure(child.returncode)
            complete = True
            return subprocess.CompletedProcess(args, child.returncode, stdout=bytes(body))
        finally:
            if not complete:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()


class HostUpgrade:
    def __init__(self, *, root=Path("/"), command=None, health=None, droplet_id=None):
        self.root = root.absolute()
        self.command = command or bounded_command
        self.installer = host.HostInstaller(root=self.root, command=self.command)
        self.inspector = server_operations.ServerInspector(root=self.root, command=self.command, health=health, droplet_id=droplet_id)
        self.directory = self.root / "var/lib/reasoning-deploy"
        self.active = self.directory / "active-server-upgrade.json"

    @contextmanager
    def _input(self, path, maximum, *, uid=None, parent_uid=None):
        self.inspector._directory(path.parent, uid=parent_uid, private=parent_uid is not None)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            owner = os.getuid() if uid is None else uid
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
                raise host.InstallError("upgrade recovery input is absent, insecure, linked or oversized")
            yield stream, info.st_size

    def _hash(self, path, maximum=MAX_BACKUP, *, uid=None, parent_uid=None):
        digest, total = hashlib.sha256(), 0
        with self._input(path, maximum, uid=uid, parent_uid=parent_uid) as (stream, declared):
            while body := stream.read(1024**2):
                total += len(body)
                if total > declared:
                    raise host.InstallError("upgrade recovery input changed while reading")
                digest.update(body)
        if total != declared:
            raise host.InstallError("upgrade recovery input changed while reading")
        return total, digest.hexdigest()

    def _read(self, path):
        with self._input(path, host.MAX_MANIFEST) as (stream, _):
            body = stream.read(host.MAX_MANIFEST + 1)
        value = json.loads(body, object_pairs_hook=_pairs)
        if not isinstance(value, dict) or host.canonical(value) != body:
            raise host.InstallError("upgrade journal must be bounded canonical JSON")
        return value

    def _write(self, path, value):
        self.inspector._directory(path.parent, private=True)
        if path.exists() or path.is_symlink():
            self._hash(path, host.MAX_MANIFEST)
        host.atomic_write(path, host.canonical(value))

    def _copy(self, source, target, expected, *, source_uid=None, target_uid=None):
        parent_uid = source_uid
        self.inspector._directory(target.parent, uid=target_uid, private=True)
        if target.exists() or target.is_symlink():
            if self._hash(target, uid=target_uid, parent_uid=target_uid)[1] != expected:
                raise host.InstallError("retained upgrade input differs; refusing to overwrite evidence")
            return
        fd, temporary = tempfile.mkstemp(prefix=".upgrade-", dir=target.parent)
        try:
            digest, total = hashlib.sha256(), 0
            with (
                os.fdopen(fd, "wb") as output,
                self._input(source, MAX_BACKUP, uid=source_uid, parent_uid=parent_uid) as (stream, declared),
            ):
                while body := stream.read(1024**2):
                    total += len(body)
                    if total > declared:
                        raise host.InstallError("upgrade input changed during retention")
                    digest.update(body)
                    output.write(body)
                if total != declared or digest.hexdigest() != expected:
                    raise host.InstallError("upgrade input differs from its exact retained identity")
                output.flush()
                os.fchmod(output.fileno(), 0o600)
                if target_uid is not None:
                    os.fchown(output.fileno(), target_uid, -1)
                os.fsync(output.fileno())
            os.link(temporary, target, follow_symlinks=False)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)

    def _json(self, args):
        response = self.command(args)
        try:
            if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 32768:
                raise ValueError
            value = json.loads(response.stdout, object_pairs_hook=_pairs)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (AttributeError, ValueError, TypeError, RecursionError) as exc:
            raise host.InstallError("upgrade helper returned an invalid receipt; retain original operation") from exc

    def _configuration(self, state, operation):
        source = state["identity"]["source"]
        if host.canonical(self._read(self.directory / "installation.json")) != host.canonical({**source, "phase": "services_started"}):
            raise host.InstallError("source installation changed during upgrade preparation")
        catalog = self.inspector._file(self.root / "etc/reasoning/compute-images.json", 65536)
        if hashlib.sha256(catalog).hexdigest() != state["identity"]["configuration"]["compute_catalog_sha256"]:
            raise host.InstallError("compute catalog changed during upgrade preparation")
        for name, expected in state["metadata"].items():
            path = self.directory / name
            if expected is None:
                if path.exists() or path.is_symlink():
                    raise host.InstallError("upgrade cannot adopt newly introduced installation metadata")
            else:
                if self._hash(path, host.MAX_MANIFEST)[1] != expected:
                    raise host.InstallError("source installation metadata changed during preparation")
                self._copy(path, operation / ("source-" + name), expected)
        mail = self.directory / "server-mail.json"
        if state["metadata"]["server-mail.json"] is not None:
            binding = self._read(mail)["mail"]
            if binding["input_sha256"] != state["identity"]["configuration"]["mail_input_sha256"]:
                raise host.InstallError("managed mail differs from the confirmed upgrade plan")
            uid = pwd.getpwnam("reasoning").pw_uid
            self.installer._mail_file(binding, uid)
            self._copy(self.root / "var/lib/reasoning/mail.json", operation / "mail.json", binding["input_sha256"], source_uid=uid)
        elif (self.root / "var/lib/reasoning/mail.json").exists() or (self.root / "var/lib/reasoning/mail.json").is_symlink():
            raise host.InstallError("upgrade cannot adopt unbound mail configuration")
        cloud = self.directory / "server-provisioning.json"
        if state["metadata"]["server-provisioning.json"] is not None:
            request = host.provisioning_request(self._read(cloud)["request"])
            marker = self._read(self.root / "etc/reasoning-provisioning.json")
            if (
                marker != {key: request[key] for key in ("plan_id", "deployment_id")}
                or self.inspector.droplet_id() != request["droplet_id"]
            ):
                raise host.InstallError("upgrade host differs from its original DigitalOcean VM identity")
        elif (self.root / "etc/reasoning-provisioning.json").exists() or (self.root / "etc/reasoning-provisioning.json").is_symlink():
            raise host.InstallError("upgrade cannot adopt a newly introduced cloud identity")
        for relative, expected in (
            (
                "etc/systemd/system/reasoning-server.service",
                host.server_unit(source["release_id"], release_identity="server-release-identity-v1" in state["source_features"]).replace(
                    "PUBLIC_ORIGIN", source["public_url"]
                ),
            ),
            ("etc/systemd/system/reasoning-proxy.service", host.proxy_unit()),
            ("etc/reasoning/Caddyfile", host.caddyfile(source["public_url"])),
        ):
            if self.inspector._file(self.root / relative, 32768) != expected.encode():
                raise host.InstallError("source managed configuration changed during preparation")

    def _target(self, manifest, release, archive):
        self.installer._directory("opt/reasoning/releases")
        target = self.root / "opt/reasoning/releases" / release
        if not target.exists() and not target.is_symlink():
            temporary = Path(tempfile.mkdtemp(prefix=".upgrade-release-", dir=target.parent))
            host.extract_bundle(archive, temporary)
            os.chmod(temporary, 0o755)
            os.rename(temporary, target)
        self.inspector._payload(manifest, release)
        venv = target / "venv"
        if venv.exists() or venv.is_symlink():
            self.inspector._directory(venv)
        self.command(["python3", "-m", "venv", str(venv)])
        pip = str(venv / "bin/pip")
        self.command(
            [
                pip,
                "--disable-pip-version-check",
                "install",
                "--no-cache-dir",
                "--require-hashes",
                "--index-url",
                "https://pypi.org/simple",
                "-r",
                str(target / "requirements.txt"),
            ]
        )
        host.install_python_payload(self.command, target, manifest)
        self.inspector._python_sources(manifest, release)
        self.command([str(venv / "bin/reasoning-server"), "--help"])

    def _fence(self, state):
        path = self.root / FENCE
        expected = {"format": 1, "operation_id": state["operation_id"], "plan_id": state["plan_id"]}
        if path.exists() or path.is_symlink():
            if host.canonical(self._read(path)) != host.canonical(expected):
                raise host.InstallError("server restart fence belongs to a different operation")
        else:
            self._write(path, expected)
        dropin = self.root / DROPIN
        self.installer._directory(str(dropin.parent.relative_to(self.root)))
        self.installer._managed(dropin, FENCE_UNIT)
        self.command(["systemctl", "daemon-reload"])
        self.command(["systemctl", "stop", "reasoning-server"])
        self._stopped(state)

    def _stopped(self, state):
        if host.canonical(self._read(self.root / FENCE)) != host.canonical(
            {"format": 1, "operation_id": state["operation_id"], "plan_id": state["plan_id"]}
        ):
            raise host.InstallError("original persistent restart fence changed")
        if self.inspector._file(self.root / DROPIN, 4096) != FENCE_UNIT.encode():
            raise host.InstallError("original persistent restart condition changed")
        properties = ("FragmentPath", "DropInPaths", "User", "Group", "LoadState", "NeedDaemonReload", "ActiveState", "MainPID", "SubState")
        result = self.command(["systemctl", "show", "--no-pager", "--property=" + ",".join(properties), "reasoning-server"])
        try:
            if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 4096:
                raise ValueError
            rows = [line.split("=", 1) for line in result.stdout.decode().splitlines()]
            value = _pairs(rows)
            if set(value) != set(properties) or value != {
                "FragmentPath": "/etc/systemd/system/reasoning-server.service",
                "DropInPaths": "/" + DROPIN,
                "User": "reasoning",
                "Group": "reasoning",
                "LoadState": "loaded",
                "NeedDaemonReload": "no",
                "ActiveState": "inactive",
                "MainPID": "0",
                "SubState": "dead",
            }:
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise host.InstallError("source server is not verified stopped under its exact persistent restart fence") from exc

    def _scratch(self, state, uid):
        self.installer._directory("var/lib/reasoning-upgrade-scratch")
        path = self._scratch_paths(state)[0]
        if not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
            os.chown(path, uid, -1)
        self.inspector._directory(path, uid=uid, private=True)
        return path

    def _scratch_paths(self, state):
        return (
            self.root / "var/lib/reasoning-upgrade-scratch" / state["operation_id"],
            self.root / "run" / ("reasoning-upgrade-" + state["operation_id"]),
        )

    def _helper(self, release, *, data_dir=None, offline=False):
        args = [
            "runuser",
            "-u",
            "reasoning",
            "--",
            str(self.root / "opt/reasoning/releases" / release / "venv/bin/reasoning-server"),
            "--data-dir",
            str(data_dir or self.root / "var/lib/reasoning"),
        ]
        if offline:
            args[4:5] = [str(self.root / "opt/reasoning/releases" / release / "venv/bin/python"), "-I", "-B", "-c", MIGRATION_RUNNER]
        return args

    def _snapshot(self, state, operation, uid):
        key = operation / "backup.key"
        if not key.exists() and not key.is_symlink():
            if state["backup_key_sha256"] is not None:
                raise host.InstallError("original escrowed backup key is missing; refusing to replace it")
            host.atomic_write(key, secrets.token_bytes(32))
        size, digest = self._hash(key, 32)
        if size != 32 or state["backup_key_sha256"] not in {None, digest}:
            raise host.InstallError("original escrowed backup key changed")
        state["backup_key_sha256"] = digest
        self._write(operation / "operation.json", state)
        scratch = self._scratch(state, uid)
        self._copy(key, scratch / "backup.key", digest, target_uid=uid)
        archive = scratch / "source.rnbk"
        source = state["identity"]["source"]["release_id"]
        if not archive.exists() and not archive.is_symlink():
            self._json(
                self._helper(source)
                + [
                    "backup",
                    "--output",
                    str(archive),
                    "--key-file",
                    str(scratch / "backup.key"),
                    "--maximum-bytes",
                    str(state["maximum_bytes"]),
                ]
            )
        size, checksum = self._hash(archive, uid=uid, parent_uid=uid)
        receipt = self._json(
            self._helper(source)
            + [
                "backup-inspect",
                "--backup",
                str(archive),
                "--key-file",
                str(scratch / "backup.key"),
                "--expected-sha256",
                checksum,
                "--maximum-bytes",
                str(state["maximum_bytes"]),
            ]
        )
        if (
            set(receipt) != {"sha256", "format", "schema", "created_at", "files", "payload_bytes", "verified"}
            or receipt["sha256"] != checksum
            or type(receipt["format"]) is not int
            or receipt["format"] != 1
            or type(receipt["schema"]) is not int
            or receipt["schema"] != state["identity"]["source_database_schema"]
            or receipt["verified"] is not True
            or type(receipt["payload_bytes"]) is not int
            or not 0 < receipt["payload_bytes"] <= state["maximum_bytes"]
            or type(receipt["files"]) is not int
            or not 3 <= receipt["files"] <= 262147
            or type(receipt["created_at"]) is not int
            or receipt["created_at"] < 0
        ):
            raise host.InstallError("source backup inspection returned a differently bound receipt")
        self._copy(archive, operation / "source.rnbk", checksum, source_uid=uid)
        state["backup"] = {**receipt, "bytes": size}

    def _retained(self, state, operation):
        if (
            state["backup"] is None
            or state["backup_key_sha256"] is None
            or self._hash(operation / "backup.key", 32) != (32, state["backup_key_sha256"])
            or self._hash(operation / "source.rnbk") != (state["backup"]["bytes"], state["backup"]["sha256"])
        ):
            raise host.InstallError("root-private original backup or key changed; retain all recovery evidence")

    def _migration(self, state, operation, uid):
        scratch = self._scratch(state, uid)
        self._copy(operation / "backup.key", scratch / "backup.key", state["backup_key_sha256"], target_uid=uid)
        self._copy(operation / "source.rnbk", scratch / "source.rnbk", state["backup"]["sha256"], target_uid=uid)
        target = state["identity"]["target"]["release_id"]
        helper = self._helper(target, offline=True)
        receipt = self._json(
            helper
            + [
                "migrate-offline",
                "--backup",
                str(scratch / "source.rnbk"),
                "--key-file",
                str(scratch / "backup.key"),
                "--expected-sha256",
                state["backup"]["sha256"],
                "--source-schema",
                str(state["identity"]["source_database_schema"]),
                "--target-release",
                target,
                "--operation-id",
                state["operation_id"],
                "--maximum-bytes",
                str(state["maximum_bytes"]),
                "--confirm-source-stopped",
            ]
        )
        if receipt.get("kind") == "upgrade_migration_failure":
            if set(receipt) != {"format", "kind", "reason"} or type(receipt["format"]) is not int or receipt["format"] != 1:
                raise host.InstallError("invalid migration failure receipt; retain original operation")
            raise MigrationFailure(receipt["reason"])
        expected = {
            "format": 1,
            "operation_id": state["operation_id"],
            "backup_sha256": state["backup"]["sha256"],
            "backup_key_sha256": state["backup_key_sha256"],
            "source_schema": state["identity"]["source_database_schema"],
            "target_schema": state["identity"]["target_database_schema"],
            "target_release": target,
            "maximum_bytes": state["maximum_bytes"],
            "phase": "migrated",
        }
        if (
            set(receipt)
            != set(expected) | {"source_state_sha256", "target_state_sha256", "backup_retained", "server_started", "activation_performed"}
            or any(type(receipt[key]) is not type(value) or receipt[key] != value for key, value in expected.items())
            or not _hex(receipt["source_state_sha256"])
            or not _hex(receipt["target_state_sha256"])
            or receipt["backup_retained"] is not True
            or receipt["server_started"] is not False
            or receipt["activation_performed"] is not False
        ):
            raise host.InstallError("offline migration returned a differently bound or premature activation receipt")
        state["migration"] = {key: receipt[key] for key in (*expected, "source_state_sha256", "target_state_sha256")}

    def _verify_prepared(self, state):
        target = state["identity"]["target"]["release_id"]
        value = self._json(
            self._helper(target, offline=True) + ["maintenance-verify", "--operation-id", state["operation_id"], "--target-release", target]
        )
        if host.canonical(value) != host.canonical({**state["migration"], "prepared_state_verified": True, "server_started": False}):
            raise host.InstallError("prepared target differs from original migration; no activation was requested")

    def _sealed(self, state):
        for scratch in self._scratch_paths(state):
            if scratch.exists() or scratch.is_symlink():
                self.inspector._directory(scratch, private=True)

    @staticmethod
    def _pointer(state):
        return {"format": 1, "operation_id": state["operation_id"], "plan_id": state["plan_id"]}

    def _remove(self, path, expected, *, private=True):
        # Deletion is restricted to an exact transaction-owned record. Never
        # remove another operation's evidence or adopt changed service files.
        if not path.exists() and not path.is_symlink():
            return
        if self.inspector._file(path, len(expected), private=private) != expected:
            raise host.InstallError("original activation ownership or restart condition changed")
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _transition(self, path, before, after, *, private=True):
        current = self.inspector._file(path, host.MAX_MANIFEST, private=private)
        if current == after:
            return
        if current != before:
            raise host.InstallError("activation refuses changed managed binding or service configuration")
        host.atomic_write(path, after, mode=0o600 if private else 0o644)

    def _activation_inputs(self, operation_id, confirmed_plan, target_release, *, completed_rollback=False, rollback_operation=False):
        pending = self.directory / "active-server-recovery.json"
        if pending.exists() or pending.is_symlink():
            raise host.InstallError("replacement recovery owns this host; resume its original operation")
        operation = self.directory / "server-upgrades" / operation_id
        state = self._read(operation / "operation.json")
        fields = {
            "format",
            "operation_id",
            "plan_id",
            "identity",
            "source_features",
            "phase",
            "maximum_bytes",
            "metadata",
            "backup_key_sha256",
            "backup",
            "migration",
        }
        if (
            set(state) != fields
            or type(state["format"]) is not int
            or state["format"] != 1
            or state["operation_id"] != operation_id
            or state["plan_id"] != confirmed_plan
            or state["phase"] not in {"prepared", *ACTIVATION_PHASES}
            or type(state["maximum_bytes"]) is not int
            or not 1024**2 <= state["maximum_bytes"] <= 64 * 1024**3
            or not isinstance(state["identity"], dict)
            or set(state["identity"]) != set(IDENTITY_FIELDS)
            or hashlib.sha256(host.canonical(state["identity"])).hexdigest() != confirmed_plan
            or not isinstance(state["metadata"], dict)
            or set(state["metadata"]) != {"installation.json", "server-mail.json", "server-provisioning.json"}
            or not _hex(state["metadata"]["installation.json"])
            or any(value is not None and not _hex(value) for value in state["metadata"].values())
            or not _hex(state["backup_key_sha256"])
            or not isinstance(state["backup"], dict)
            or not isinstance(state["migration"], dict)
        ):
            raise host.InstallError("activation requires the original fully prepared upgrade journal")
        identity = state["identity"]
        manifests = []
        for name, binding in (("source", identity["source"]), ("target", identity["target"])):
            if not isinstance(binding, dict):
                raise host.InstallError("activation release binding is invalid")
            archive = operation / (name + ".tar.gz")
            self._hash(archive, host.MAX_BUNDLE)
            manifest, release, checksum = host.inspect_bundle(archive, binding["bundle_sha256"])
            if host.canonical(binding) != host.canonical(
                server_operations.release_binding(manifest, release, checksum, binding["public_url"])
            ):
                raise host.InstallError("retained activation release differs from its original binding")
            self.inspector._payload(manifest, release)
            self.inspector._python_sources(manifest, release)
            manifests.append(manifest)
        source_manifest, target_manifest = manifests
        if not rollback_operation:
            self._rollback_upgrade_support(target_manifest)
        if (
            identity["format"] != 1
            or type(identity["format"]) is not int
            or identity["operation"] != "upgrade-plan"
            or identity["target"]["release_id"] != target_release
            or identity["source"]["release_id"] == target_release
            or identity["source"]["public_url"] != identity["target"]["public_url"]
            or source_manifest["dirty"]
            or target_manifest["dirty"]
            or type(identity["source_database_schema"]) is not int
            or type(identity["target_database_schema"]) is not int
            or identity["source_database_schema"] != source_manifest["database_schema"]
            or identity["target_database_schema"] != target_manifest["database_schema"]
            or not 13 <= source_manifest["database_schema"] <= target_manifest["database_schema"]
            or state["source_features"] != source_manifest.get("installation_features", [])
            or not {"server-maintenance-v1", "server-release-identity-v1", "server-upgrade-activation-v1"}
            <= set(target_manifest.get("installation_features", []))
            or not isinstance(identity["configuration"], dict)
            or set(identity["configuration"]) != {"compute_catalog_sha256", "mail_input_sha256"}
            or not _hex(identity["configuration"]["compute_catalog_sha256"])
            or identity["configuration"]["mail_input_sha256"] is not None
            and not _hex(identity["configuration"]["mail_input_sha256"])
        ):
            raise host.InstallError("activation differs from the original supported release-change plan")
        backup = state["backup"]
        expected = {
            "format": 1,
            "operation_id": operation_id,
            "backup_sha256": backup["sha256"],
            "backup_key_sha256": state["backup_key_sha256"],
            "source_schema": source_manifest["database_schema"],
            "target_schema": target_manifest["database_schema"],
            "target_release": target_release,
            "maximum_bytes": state["maximum_bytes"],
            "phase": "migrated",
        }
        migration = state["migration"]
        if (
            set(backup) != {"sha256", "format", "schema", "created_at", "files", "payload_bytes", "verified", "bytes"}
            or type(backup["format"]) is not int
            or backup["format"] != 1
            or type(backup["schema"]) is not int
            or backup["schema"] != source_manifest["database_schema"]
            or not _hex(backup["sha256"])
            or backup["verified"] is not True
            or any(type(backup[key]) is not int for key in ("created_at", "files", "payload_bytes", "bytes"))
            or backup["created_at"] < 0
            or not 3 <= backup["files"] <= 262147
            or not 0 < backup["payload_bytes"] <= state["maximum_bytes"]
            or not 0 < backup["bytes"] <= MAX_BACKUP
            or set(migration) != set(expected) | {"source_state_sha256", "target_state_sha256"}
            or any(type(migration[key]) is not type(value) or migration[key] != value for key, value in expected.items())
            or not _hex(migration["source_state_sha256"])
            or not _hex(migration["target_state_sha256"])
        ):
            raise host.InstallError("original activation backup or migration identity differs")
        if self.active.exists() or self.active.is_symlink():
            if host.canonical(self._read(self.active)) != host.canonical(self._pointer(state)):
                raise host.InstallError("another upgrade owns the active installation")
        elif state["phase"] != "completed" and not completed_rollback:
            raise host.InstallError("original active upgrade ownership record is missing")
        self._retained(state, operation)
        self._sealed(state)
        if state["phase"] == "completed":
            completion = operation / "completion.json"
            if not completion.exists() and not completion.is_symlink():
                raise host.InstallError("original completed host upgrade history is missing")
            try:
                if host.canonical(self._read(completion)) != host.canonical(self._upgrade_receipt(state, operation)):
                    raise host.InstallError("original completed host upgrade history changed")
            except (OSError, ValueError, TypeError) as exc:
                raise host.InstallError("original completed host upgrade history is invalid") from exc
        return state, operation, target_manifest

    def _rollback_upgrade_support(self, manifest):
        # Older targets remain valid for ordinary installations, but cannot
        # interpret a restored source's provenance or its successor handoff.
        journal = self.root / "var/lib/reasoning/server-rollback.json"
        history = self.root / "var/lib/reasoning/rollback-history"
        present = journal.exists() or journal.is_symlink()
        if not present and not history.exists() and not history.is_symlink():
            return
        if "server-rollback-upgrade-v1" not in manifest.get("installation_features", []):
            raise host.InstallError("upgrading restored state requires target rollback-provenance handoff support")
        if not present:
            raise host.InstallError("original restored-source provenance is missing")
        uid = pwd.getpwnam("reasoning").pw_uid
        with self._input(journal, 8192, uid=uid, parent_uid=uid) as (stream, _):
            body = stream.read(8193)
        value = json.loads(body, object_pairs_hook=_pairs)
        if (
            not isinstance(value, dict)
            or len(body) > 8192
            or host.canonical(value) != body
            or not isinstance(value.get("phase"), str)
            or value.get("phase") not in {"released", "superseded"}
            or not _hex(value.get("operation_id"))
            or not _hex(value.get("rollback_id"))
            or (value["phase"] == "superseded") != ("superseded_by" in value)
            or value["phase"] == "superseded"
            and not isinstance(value["superseded_by"], dict)
        ):
            raise host.InstallError("source rollback provenance is malformed or not released")
        previous = self.directory / "server-upgrades" / value["operation_id"]
        state, retained = self._read(previous / "operation.json"), self._read(previous / "rollback.json")
        self._rollback_record(state, value["rollback_id"], retained)
        if retained["phase"] != "completed" or host.canonical(self._read(previous / "rollback-completion.json")) != host.canonical(
            self._rollback_receipt(state, retained, previous)
        ):
            raise host.InstallError("original completed source rollback ownership is missing or changed")
        original = {key: item for key, item in value.items() if key != "superseded_by"}
        original["phase"] = "released"
        if host.canonical(original) != host.canonical({**retained["restoration"], "phase": "released"}):
            raise host.InstallError("source rollback provenance differs from its protected completion history")

    @staticmethod
    def _upgrade_receipt(state, operation):
        return {
            "format": 1,
            "operation": "upgrade-activate",
            "operation_id": state["operation_id"],
            "plan_id": state["plan_id"],
            "source": state["identity"]["source"],
            "target": state["identity"]["target"],
            "phase": "completed",
            "server_restart_fenced": False,
            "root_private_backup_retained": True,
            "activation_performed": True,
            "upgrade_completed": True,
            "https_release_verified": True,
            "database_schema_verified": True,
            "runtime_dependency_attestation": False,
            "recovery_directory": str(operation),
        }

    def _activation_configuration(self, state, operation):
        source, target = state["identity"]["source"], state["identity"]["target"]
        phase = state["phase"]
        originals, replacements = {}, {}
        for name, checksum in state["metadata"].items():
            path = self.directory / name
            if checksum is None:
                if path.exists() or path.is_symlink():
                    raise host.InstallError("activation cannot adopt newly introduced installation metadata")
                continue
            retained = operation / ("source-" + name)
            if self._hash(retained, host.MAX_MANIFEST)[1] != checksum:
                raise host.InstallError("original installation binding evidence changed")
            original = self._read(retained)
            if name == "installation.json":
                if host.canonical(original) != host.canonical({**source, "phase": "services_started"}):
                    raise host.InstallError("original source installation binding differs")
                replacement = {**target, "phase": "packages"}
            elif name == "server-mail.json":
                if set(original) != {"installation", "mail"} or host.canonical(original["installation"]) != host.canonical(source):
                    raise host.InstallError("original mail installation binding differs")
                replacement = {**original, "installation": target}
            else:
                if (
                    set(original) != {"format", "binding", "request"}
                    or type(original["format"]) is not int
                    or original["format"] != 1
                    or host.canonical(original["binding"]) != host.canonical(source)
                ):
                    raise host.InstallError("original cloud installation binding differs")
                replacement = {**original, "binding": target}
            originals[name], replacements[name] = original, replacement
            allowed = [original] if phase == "prepared" else [original, replacement] if phase == "switching" else [replacement]
            if name == "installation.json" and phase in {"ready", "finalizing", "completed"}:
                started = {**target, "phase": "services_started"}
                allowed = [replacement, started] if phase == "ready" else [started]
            current = host.canonical(self._read(path))
            if current not in [host.canonical(value) for value in allowed]:
                raise host.InstallError("managed installation bindings differ from the original activation phase")
        configuration = state["identity"]["configuration"]
        catalog = self.inspector._file(self.root / "etc/reasoning/compute-images.json", 65536)
        if hashlib.sha256(catalog).hexdigest() != configuration["compute_catalog_sha256"]:
            raise host.InstallError("activation compute catalog changed")
        if "server-mail.json" in originals:
            mail = originals["server-mail.json"]["mail"]
            if (
                mail["input_sha256"] != configuration["mail_input_sha256"]
                or self._hash(operation / "mail.json", host.MAX_MAIL_INPUT)[1] != mail["input_sha256"]
            ):
                raise host.InstallError("original activation mail input differs")
            self.installer._mail_file(mail, pwd.getpwnam("reasoning").pw_uid)
        elif (
            configuration["mail_input_sha256"] is not None
            or (self.root / "var/lib/reasoning/mail.json").exists()
            or (self.root / "var/lib/reasoning/mail.json").is_symlink()
        ):
            raise host.InstallError("activation refuses unbound mail configuration")
        marker = self.root / "etc/reasoning-provisioning.json"
        if "server-provisioning.json" in originals:
            request = host.provisioning_request(originals["server-provisioning.json"]["request"])
            if (
                host.canonical(self._read(marker)) != host.canonical({key: request[key] for key in ("plan_id", "deployment_id")})
                or self.inspector.droplet_id() != request["droplet_id"]
            ):
                raise host.InstallError("activation host differs from the original DigitalOcean VM")
        elif marker.exists() or marker.is_symlink():
            raise host.InstallError("activation cannot adopt newly introduced cloud identity")
        for relative, expected in (
            ("etc/systemd/system/reasoning-proxy.service", host.proxy_unit()),
            ("etc/reasoning/Caddyfile", host.caddyfile(target["public_url"])),
        ):
            if self.inspector._file(self.root / relative, 32768) != expected.encode():
                raise host.InstallError("activation managed proxy configuration changed")
        unit_path = self.root / "etc/systemd/system/reasoning-server.service"
        old_unit = (
            host.server_unit(source["release_id"], release_identity="server-release-identity-v1" in state["source_features"])
            .replace("PUBLIC_ORIGIN", source["public_url"])
            .encode()
        )
        activation_unit = (
            host.server_unit(target["release_id"], release_identity=True, maintenance_operation=state["operation_id"])
            .replace("PUBLIC_ORIGIN", target["public_url"])
            .encode()
        )
        final_unit = host.server_unit(target["release_id"], release_identity=True).replace("PUBLIC_ORIGIN", target["public_url"]).encode()
        units = [old_unit] if phase == "prepared" else [old_unit, activation_unit] if phase == "switching" else [activation_unit]
        if phase == "finalizing":
            units = [activation_unit, final_unit]
        elif phase == "completed":
            units = [final_unit]
        if self.inspector._file(unit_path, 32768) not in units:
            raise host.InstallError("activation server unit differs from its exact managed phase")
        fence, dropin = self.root / FENCE, self.root / DROPIN
        if fence.exists() or fence.is_symlink():
            if phase not in {"prepared", "switching", "activating"} or host.canonical(self._read(fence)) != host.canonical(
                self._pointer(state)
            ):
                raise host.InstallError("original activation restart fence changed")
        elif phase in {"prepared", "switching"}:
            raise host.InstallError("original activation restart fence is missing")
        if dropin.exists() or dropin.is_symlink():
            if phase == "completed" or self.inspector._file(dropin, 4096) != FENCE_UNIT.encode():
                raise host.InstallError("original activation restart condition changed")
        elif phase not in {"finalizing", "completed"}:
            raise host.InstallError("original activation restart condition is missing")
        return originals, replacements, unit_path, old_unit, activation_unit, final_unit

    def _running_target(self):
        properties = ("FragmentPath", "DropInPaths", "User", "Group", "LoadState", "NeedDaemonReload", "ActiveState", "MainPID", "SubState")
        result = self.command(["systemctl", "show", "--no-pager", "--property=" + ",".join(properties), "reasoning-server"])
        try:
            if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 4096:
                raise ValueError
            value = _pairs([line.split("=", 1) for line in result.stdout.decode().splitlines()])
            expected = {
                "FragmentPath": "/etc/systemd/system/reasoning-server.service",
                "DropInPaths": "/" + DROPIN if (self.root / DROPIN).exists() else "",
                "User": "reasoning",
                "Group": "reasoning",
                "LoadState": "loaded",
                "NeedDaemonReload": "no",
                "ActiveState": "active",
                "SubState": "running",
            }
            if (
                set(value) != set(properties)
                or any(value[key] != item for key, item in expected.items())
                or re.fullmatch(r"[1-9][0-9]{0,9}", value["MainPID"]) is None
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise host.InstallError("target server is not running under its exact loaded managed unit") from exc

    def _observe(self, state, *, completed=False):
        target = state["identity"]["target"]["release_id"]
        value = self._json(
            self._helper(target) + ["maintenance-observe", "--operation-id", state["operation_id"], "--target-release", target]
        )
        phase = value.get("phase")
        expected = {
            **state["migration"],
            "phase": phase,
            "activation_state_verified": True,
            "database_schema_verified": True,
            "server_started_by_command": False,
            "health_verified_by_command": False,
        }
        if phase not in ({"completed"} if completed else {"activation_started", "completed"}) or host.canonical(value) != host.canonical(
            expected
        ):
            raise host.InstallError("target activation observation differs from the original migration")

    def _readiness(self, state, *, completed=False):
        self._running_target()
        target = state["identity"]["target"]
        result = self.inspector.health(
            target["public_url"],
            timeout=60,
            require_mail=state["identity"]["configuration"]["mail_input_sha256"] is not None,
            expected_release=target["release_id"],
        )
        if (
            not isinstance(result, dict)
            or result.get("https_ready") is not True
            or result.get("public_url") != target["public_url"]
            or type(result.get("initialized")) is not bool
        ):
            raise host.InstallError("target HTTPS release readiness is unverified")
        self._observe(state, completed=completed)

    def _finish_application(self, state):
        target = state["identity"]["target"]["release_id"]
        value = self._json(
            self._helper(target)
            + ["maintenance-finish", "--operation-id", state["operation_id"], "--target-release", target, "--confirm-activation-verified"]
        )
        expected = {
            **state["migration"],
            "phase": "completed",
            "activation_verified_by_operator": True,
            "health_verified_by_command": False,
            "services_changed": False,
            "backup_retention_verified_by_command": False,
        }
        if host.canonical(value) != host.canonical(expected):
            raise host.InstallError("target completion receipt differs from the original migration")

    def activate(self, *, operation_id, confirmed_plan, target_release, activation_confirmed=False, skip_platform=False):
        if activation_confirmed is not True or not all(map(_hex, (operation_id, confirmed_plan, target_release))):
            raise host.InstallError("activation requires exact original operation/plan/target and explicit external-effect confirmation")
        self._platform(skip_platform)
        with self._bootstrap() as fd:
            rollback = self.directory / "server-upgrades" / operation_id / "rollback.json"
            if rollback.exists() or rollback.is_symlink():
                raise host.InstallError("rollback owns this operation; target activation cannot replay it")
            if not skip_platform and pwd.getpwnam("reasoning").pw_uid == os.getuid():
                raise host.InstallError("the application service account must not own root recovery material")
            state, operation, manifest = self._activation_inputs(operation_id, confirmed_plan, target_release)
            originals, replacements, unit_path, old_unit, activation_unit, final_unit = self._activation_configuration(state, operation)
            path = operation / "operation.json"

            def advance(phase):
                state["phase"] = phase
                self._write(path, state)

            if state["phase"] == "prepared":
                self._stopped(state)
                self._verify_prepared(state)
                advance("switching")
            if state["phase"] == "switching":
                # Recover a unit write whose daemon-reload acknowledgment was
                # lost, still under the original persistent startup fence.
                self.command(["systemctl", "daemon-reload"])
                self._stopped(state)
                self._verify_prepared(state)
                for name, replacement in replacements.items():
                    self._transition(self.directory / name, host.canonical(originals[name]), host.canonical(replacement))
                self._transition(unit_path, old_unit, activation_unit, private=False)
                self.command(["systemctl", "daemon-reload"])
                self._stopped(state)
                # Removing the sentinel permits reboot/service activation.
                # Persist this effect boundary BEFORE it can be removed.
                advance("activating")
            if state["phase"] == "activating":
                self._remove(self.root / FENCE, host.canonical(self._pointer(state)))
                self.command(["systemctl", "start", "reasoning-server"])
                self._readiness(state)
                advance("ready")
            if state["phase"] == "ready":
                self._readiness(state)
                self._finish_application(state)
                self._transition(
                    self.directory / "installation.json",
                    host.canonical(replacements["installation.json"]),
                    host.canonical({**state["identity"]["target"], "phase": "services_started"}),
                )
                advance("finalizing")
            if state["phase"] == "finalizing":
                self._observe(state, completed=True)
                self._transition(unit_path, activation_unit, final_unit, private=False)
                self._remove(self.root / DROPIN, FENCE_UNIT.encode(), private=False)
                self.command(["systemctl", "daemon-reload"])
            self._activation_configuration(state, operation)
            self._retained(state, operation)
            self._readiness(state, completed=True)
            target = state["identity"]["target"]
            report = self.inspector.inspect(manifest, target_release, target["bundle_sha256"], target["public_url"], owned_bootstrap_fd=fd)
            if report["all_checks_passed"] is not True or host.canonical(report["configuration"]) != host.canonical(
                state["identity"]["configuration"]
            ):
                raise host.InstallError("final target installation diagnosis failed; preserve original upgrade and live data")
            receipt = self._upgrade_receipt(state, operation)
            completion = operation / "completion.json"
            if completion.exists() or completion.is_symlink():
                if host.canonical(self._read(completion)) != host.canonical(receipt):
                    raise host.InstallError("original completed host upgrade history changed")
            elif state["phase"] == "completed":
                raise host.InstallError("original completed host upgrade history is missing")
            else:
                self._write(completion, receipt)
            if state["phase"] != "completed":
                advance("completed")
            self._remove(self.active, host.canonical(self._pointer(state)))
            return receipt

    @staticmethod
    def _rename(source, target):
        try:
            host.atomic_rename(source, target)
        except FileExistsError as exc:
            raise host.InstallError("rollback refuses an existing displaced-state destination") from exc
        except OSError as exc:
            raise host.InstallError("rollback requires same-filesystem atomic no-replace state displacement") from exc
        for parent in {source.parent, target.parent}:
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _exchange(source, target):
        # Both paths must already be owned and verified. Exchange avoids an
        # absent canonical data directory in which an old service could initialize.
        try:
            host.atomic_rename(source, target, exchange=True)
        except OSError as exc:
            raise host.InstallError("rollback requires same-filesystem atomic source/placeholder exchange") from exc
        for parent in {source.parent, target.parent}:
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def _state_leases(self, data, uid):
        self.inspector._directory(data, uid=uid, private=True)
        with ExitStack() as leases:
            for name in (
                "server-maintenance.lock",
                "initialization.lock",
                "inference-worker.lock",
                "workflow-worker.lock",
                "mail-worker.lock",
                "webhook-worker.lock",
            ):
                path = data / name
                if not path.exists() and not path.is_symlink():
                    if name == "server-maintenance.lock":
                        raise host.InstallError("original target maintenance lease is missing")
                    continue
                if self._hash(path, 0, uid=uid, parent_uid=uid)[0] != 0:
                    raise host.InstallError("original target execution lease changed")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                leases.callback(os.close, fd)
                info, original = os.fstat(fd), path.lstat()
                if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
                    raise host.InstallError("original target execution lease changed during acquisition")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _inventory(self, data, uid):
        # Retain failed/corrupt target state without relying on target SQL or
        # executable health. Bind every regular file, including WAL/history.
        self.inspector._directory(data, uid=data.lstat().st_uid, private=True)
        digest, files, total, count = hashlib.sha256(), 0, 0, 0
        for current, directories, names in os.walk(data, followlinks=False):
            directories.sort()
            names.sort()
            for name in (*directories, *names):
                path = Path(current) / name
                relative = path.relative_to(data).as_posix()
                info = path.lstat()
                count += 1
                if (
                    count > 262247
                    or len(relative.encode()) > 1024
                    or len(path.relative_to(data).parts) > 32
                    or info.st_uid not in {uid, os.getuid()}
                ):
                    raise host.InstallError("displaced state exceeds its bounded private inventory")
                if stat.S_ISDIR(info.st_mode):
                    row = [relative, "directory", info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)]
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:

                        def identity(row):
                            return (
                                row.st_dev,
                                row.st_ino,
                                row.st_mode,
                                row.st_uid,
                                row.st_nlink,
                                row.st_size,
                                row.st_mtime_ns,
                                row.st_ctime_ns,
                            )

                        if identity(os.fstat(stream.fileno())) != identity(info):
                            raise host.InstallError("displaced state changed while opening")
                        checksum, size = hashlib.sha256(), 0
                        while block := stream.read(1024**2):
                            checksum.update(block)
                            size += len(block)
                            total += len(block)
                            if size > info.st_size or total > 2 * MAX_BACKUP:
                                raise host.InstallError("displaced state changed or exceeds its byte allowance")
                        if size != info.st_size or identity(os.fstat(stream.fileno())) != identity(info):
                            raise host.InstallError("displaced state changed while retaining it")
                    files += 1
                    row = [relative, "file", info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), size, checksum.hexdigest()]
                else:
                    raise host.InstallError("displaced state contains linked or non-regular evidence; refusing automatic adoption")
                digest.update(host.canonical(row))
        return {"sha256": digest.hexdigest(), "files": files, "bytes": total}

    def _rollback_bindings(self, state, operation, value):
        phase = value["phase"]
        source = state["identity"]["source"]
        replacements, before = {}, {}
        for name, checksum in value["before"].items():
            if name == "unit":
                current = self.root / "etc/systemd/system/reasoning-server.service"
                retained = operation / "rollback-unit.service"
                body = self.inspector._file(current, 32768)
                if self._hash(retained, 32768)[1] != checksum:
                    raise host.InstallError("original rollback server unit changed")
                old_unit = retained.read_bytes()
                source_unit = (
                    host.server_unit(source["release_id"], release_identity=True).replace("PUBLIC_ORIGIN", source["public_url"]).encode()
                )
                allowed = (
                    [old_unit, source_unit]
                    if phase == "switching"
                    else ([source_unit] if phase in {"activating", "ready", "finalizing", "completed"} else [old_unit])
                )
                if body not in allowed:
                    raise host.InstallError("rollback server unit differs from its managed phase")
            elif checksum is None:
                current = self.directory / name
                if state["metadata"][name] is not None or current.exists() or current.is_symlink():
                    raise host.InstallError("rollback cannot adopt new installation bindings")
            else:
                retained = operation / ("rollback-" + name)
                original = operation / ("source-" + name)
                if (
                    self._hash(retained, host.MAX_MANIFEST)[1] != checksum
                    or self._hash(original, host.MAX_MANIFEST)[1] != state["metadata"][name]
                ):
                    raise host.InstallError("original rollback installation binding changed")
                before[name] = retained.read_bytes()
                replacement = self._read(original)
                if name == "installation.json":
                    if host.canonical(replacement) != host.canonical({**source, "phase": "services_started"}):
                        raise host.InstallError("original rollback source installation differs")
                    replacement = {**source, "phase": "packages"}
                elif name == "server-mail.json":
                    if set(replacement) != {"installation", "mail"} or host.canonical(replacement["installation"]) != host.canonical(
                        source
                    ):
                        raise host.InstallError("original rollback source mail binding differs")
                elif (
                    set(replacement) != {"format", "binding", "request"}
                    or type(replacement["format"]) is not int
                    or replacement["format"] != 1
                    or host.canonical(replacement["binding"]) != host.canonical(source)
                ):
                    raise host.InstallError("original rollback source cloud binding differs")
                replacements[name] = replacement
                allowed = (
                    [before[name], host.canonical(replacement)]
                    if phase == "switching"
                    else ([host.canonical(replacement)] if phase in {"activating", "ready", "finalizing", "completed"} else [before[name]])
                )
                if name == "installation.json" and phase in {"ready", "finalizing", "completed"}:
                    started = host.canonical({**source, "phase": "services_started"})
                    allowed = [host.canonical(replacement), started] if phase == "ready" else [started]
                if host.canonical(self._read(self.directory / name)) not in allowed:
                    raise host.InstallError("rollback installation binding differs from its managed phase")
        configuration = state["identity"]["configuration"]
        if (
            hashlib.sha256(self.inspector._file(self.root / "etc/reasoning/compute-images.json", 65536)).hexdigest()
            != configuration["compute_catalog_sha256"]
        ):
            raise host.InstallError("rollback compute catalog changed")
        origin = state["identity"]["source"]["public_url"]
        for relative, body in (
            ("etc/systemd/system/reasoning-proxy.service", host.proxy_unit()),
            ("etc/reasoning/Caddyfile", host.caddyfile(origin)),
        ):
            if self.inspector._file(self.root / relative, 32768) != body.encode():
                raise host.InstallError("rollback proxy configuration changed")
        if (
            state["metadata"]["server-mail.json"] is not None
            and self._hash(operation / "mail.json", host.MAX_MAIL_INPUT)[1] != configuration["mail_input_sha256"]
        ):
            raise host.InstallError("original rollback mail credentials changed")
        cloud = self.directory / "server-provisioning.json"
        marker = self.root / "etc/reasoning-provisioning.json"
        if state["metadata"]["server-provisioning.json"] is not None:
            request = host.provisioning_request(self._read(cloud)["request"])
            if (
                host.canonical(self._read(marker)) != host.canonical({key: request[key] for key in ("plan_id", "deployment_id")})
                or self.inspector.droplet_id() != request["droplet_id"]
            ):
                raise host.InstallError("rollback differs from the original DigitalOcean VM identity")
        elif marker.exists() or marker.is_symlink():
            raise host.InstallError("rollback cannot adopt new cloud identity")
        return before, replacements, self.root / "etc/systemd/system/reasoning-server.service", old_unit, source_unit

    def _rollback_placeholder(self, state, value, *, create=False, location=None):
        live = self.root / "var/lib/reasoning" if location is None else location
        self.inspector._directory(live, private=True)
        marker = live / "server-rollback-blocked.json"
        expected = host.canonical({**self._pointer(state), "rollback_id": value["rollback_id"], "phase": "placeholder"})
        if create and not marker.exists() and not marker.is_symlink():
            if list(live.iterdir()):
                raise host.InstallError("rollback refuses new live state after displacement")
            host.atomic_write(marker, expected)
        if sorted(path.name for path in live.iterdir()) != [marker.name] or self.inspector._file(marker, 8192, private=True) != expected:
            raise host.InstallError("rollback's original live-state placeholder changed")

    @staticmethod
    def _restoration_receipt(state, rollback_id, receipt):
        expected = {
            "format": 1,
            "operation_id": state["operation_id"],
            "rollback_id": rollback_id,
            "plan_id": state["plan_id"],
            "source_release": state["identity"]["source"]["release_id"],
            "source_schema": state["identity"]["source_database_schema"],
            "backup_sha256": state["backup"]["sha256"],
            "backup_key_sha256": state["backup_key_sha256"],
            "mail_sha256": state["identity"]["configuration"]["mail_input_sha256"],
            "maximum_bytes": state["maximum_bytes"],
            "phase": "prepared",
            "prepared_state_verified": True,
            "server_started": False,
        }
        recovery = receipt.get("recovery")
        counts = {
            "sessions_revoked",
            "invitations_revoked",
            "workers_revoked",
            "bots_revoked",
            "webhooks_revoked",
            "scalers_revoked",
            "inference_runs_held",
            "compute_jobs_held",
        }
        if (
            set(receipt) != set(expected) | {"state_sha256", "recovery"}
            or any(type(receipt[key]) is not type(item) or receipt[key] != item for key, item in expected.items())
            or not _hex(receipt["state_sha256"])
            or not isinstance(recovery, dict)
            or not counts <= set(recovery) <= counts | {"workflow_runs_held", "work_workers_revoked"}
            or any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in recovery.values())
        ):
            raise host.InstallError("source rollback restoration returned a differently bound or unsafe receipt")
        return {key: item for key, item in receipt.items() if key not in {"prepared_state_verified", "server_started"}}

    def _rollback_record(self, state, rollback_id, value):
        if (
            set(value)
            != {"format", "operation_id", "rollback_id", "plan_id", "upgrade_sha256", "phase", "before", "restoration", "displaced"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["operation_id"] != state["operation_id"]
            or value["rollback_id"] != rollback_id
            or value["plan_id"] != state["plan_id"]
            or value["upgrade_sha256"] != hashlib.sha256(host.canonical(state)).hexdigest()
            or not isinstance(value["phase"], str)
            or value["phase"]
            not in {"fencing", "target_stopped", "restoring", "sealing", "restored", "displacing", "prepared", *ROLLBACK_ACTIVATION_PHASES}
            or not isinstance(value["before"], dict)
            or set(value["before"]) != {"installation.json", "server-mail.json", "server-provisioning.json", "unit"}
            or not _hex(value["before"]["installation.json"])
            or not _hex(value["before"]["unit"])
            or any(checksum is not None and not _hex(checksum) for checksum in value["before"].values())
            or (value["restoration"] is None) != (value["phase"] in {"fencing", "target_stopped", "restoring"})
            or value["restoration"] is not None
            and not isinstance(value["restoration"], dict)
            or (value["displaced"] is None) != (value["phase"] not in {"displacing", "prepared", *ROLLBACK_ACTIVATION_PHASES})
            or value["displaced"] is not None
            and (
                not isinstance(value["displaced"], dict)
                or set(value["displaced"]) != {"sha256", "files", "bytes"}
                or not _hex(value["displaced"]["sha256"])
                or type(value["displaced"]["files"]) is not int
                or not 1 <= value["displaced"]["files"] <= 262247
                or type(value["displaced"]["bytes"]) is not int
                or not 0 <= value["displaced"]["bytes"] <= 2 * MAX_BACKUP
            )
        ):
            raise host.InstallError("rollback journal differs from its original exact operation")
        if value["restoration"] is not None:
            self._restoration_receipt(
                state, rollback_id, {**value["restoration"], "prepared_state_verified": True, "server_started": False}
            )

    def rollback_prepare(
        self,
        *,
        operation_id,
        rollback_id,
        confirmed_plan,
        source_release,
        target_release,
        workers_fenced=False,
        data_loss_confirmed=False,
        skip_platform=False,
    ):
        if (
            workers_fenced is not True
            or data_loss_confirmed is not True
            or not all(map(_hex, (operation_id, rollback_id, confirmed_plan, source_release, target_release)))
        ):
            raise host.InstallError(
                "rollback requires exact original identities, physical execution fencing and explicit newer-data-loss confirmation"
            )
        self._platform(skip_platform)
        with self._bootstrap():
            uid = pwd.getpwnam("reasoning").pw_uid
            if not skip_platform and uid == os.getuid():
                raise host.InstallError("the application account must not own root rollback evidence")
            operation = self.directory / "server-upgrades" / operation_id
            path = operation / "rollback.json"
            value = self._read(path) if path.exists() or path.is_symlink() else None
            if value is not None and isinstance(value.get("phase"), str) and value["phase"] in ROLLBACK_ACTIVATION_PHASES:
                raise host.InstallError("rollback activation may have live effects; preparation cannot replay it")
            state, _, _ = self._activation_inputs(operation_id, confirmed_plan, target_release, rollback_operation=True)
            source = state["identity"]["source"]
            if source_release != source["release_id"] or "server-rollback-state-v1" not in state["source_features"]:
                raise host.InstallError("rollback requires the exact matching source release and fenced restoration support")
            stage = self.root / "var/lib" / ("reasoning-rollback-" + rollback_id)
            candidate = stage / "state"
            self.installer._directory("run")
            scratch = self.root / "run" / ("reasoning-rollback-" + rollback_id)
            live, displaced = self.root / "var/lib/reasoning", operation / "displaced-state"
            if value is None:
                if (
                    stage.exists()
                    or stage.is_symlink()
                    or scratch.exists()
                    or scratch.is_symlink()
                    or displaced.exists()
                    or displaced.is_symlink()
                ):
                    raise host.InstallError("rollback cannot adopt existing staged or displaced state without its original journal")
                self._activation_configuration(state, operation)
                before = {}
                for name in ("installation.json", "server-mail.json", "server-provisioning.json"):
                    current = self.directory / name
                    checksum = self._hash(current, host.MAX_MANIFEST)[1] if current.exists() or current.is_symlink() else None
                    before[name] = checksum
                    if checksum is not None:
                        self._copy(current, operation / ("rollback-" + name), checksum)
                body = self.inspector._file(self.root / "etc/systemd/system/reasoning-server.service", 32768)
                before["unit"] = hashlib.sha256(body).hexdigest()
                retained = operation / "rollback-unit.service"
                if retained.exists() or retained.is_symlink():
                    if self._hash(retained, 32768)[1] != before["unit"]:
                        raise host.InstallError("retained rollback before-image changed")
                else:
                    host.atomic_write(retained, body)
                value = {
                    "format": 1,
                    "operation_id": operation_id,
                    "rollback_id": rollback_id,
                    "plan_id": confirmed_plan,
                    "upgrade_sha256": hashlib.sha256(host.canonical(state)).hexdigest(),
                    "phase": "fencing",
                    "before": before,
                    "restoration": None,
                    "displaced": None,
                }
                self._write(path, value)
            self._rollback_record(state, rollback_id, value)
            self._rollback_bindings(state, operation, value)
            pointer = self._pointer(state)
            if not self.active.exists() and not self.active.is_symlink():
                if value["phase"] != "fencing":
                    raise host.InstallError("original rollback active ownership is missing")
                self._write(self.active, pointer)

            def advance(phase):
                value["phase"] = phase
                self._write(path, value)

            if value["phase"] == "fencing":
                self._fence(state)
                advance("target_stopped")
            self._stopped(state)
            self._retained(state, operation)
            if value["phase"] == "target_stopped":
                advance("restoring")
            if value["phase"] == "restoring":
                for directory in (stage, scratch):
                    if not directory.exists() and not directory.is_symlink():
                        self.inspector._directory(directory.parent)
                        directory.mkdir(mode=0o700)
                        os.chown(directory, uid, -1)
                    self.inspector._directory(directory, uid=uid, private=True)
                self._copy(operation / "backup.key", scratch / "backup.key", state["backup_key_sha256"], target_uid=uid)
                self._copy(operation / "source.rnbk", scratch / "source.rnbk", state["backup"]["sha256"], target_uid=uid)
                mail_options = []
                if state["metadata"]["server-mail.json"] is not None:
                    mail_sha256 = state["identity"]["configuration"]["mail_input_sha256"]
                    self._copy(operation / "mail.json", scratch / "mail.json", mail_sha256, target_uid=uid)
                    mail_options = ["--mail-file", str(scratch / "mail.json"), "--expected-mail-sha256", mail_sha256]
                receipt = self._json(
                    self._helper(source_release, data_dir=candidate)
                    + [
                        "rollback-restore",
                        "--backup",
                        str(scratch / "source.rnbk"),
                        "--key-file",
                        str(scratch / "backup.key"),
                        "--expected-sha256",
                        state["backup"]["sha256"],
                        "--operation-id",
                        operation_id,
                        "--rollback-id",
                        rollback_id,
                        "--plan-id",
                        confirmed_plan,
                        "--source-release",
                        source_release,
                        "--maximum-bytes",
                        str(state["maximum_bytes"]),
                        "--confirm-source-stopped",
                        "--confirm-newer-data-loss",
                    ]
                    + mail_options
                )
                value["restoration"] = self._restoration_receipt(state, rollback_id, receipt)
                advance("sealing")
            if value["phase"] == "sealing":
                if scratch.exists() or scratch.is_symlink():
                    self.inspector._directory(scratch, uid=scratch.lstat().st_uid, private=True)
                    if scratch.lstat().st_uid not in {uid, os.getuid()}:
                        raise host.InstallError("rollback scratch belongs to an unexpected account")
                    os.chown(scratch, os.getuid(), -1)
                    os.chmod(scratch, 0o700)
                    fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                advance("restored")
            if scratch.exists() or scratch.is_symlink():
                self.inspector._directory(scratch, private=True)
            receipt = self._json(
                self._helper(source_release, data_dir=candidate)
                + ["rollback-verify", "--rollback-id", rollback_id, "--source-release", source_release]
            )
            if host.canonical(receipt) != host.canonical(
                {**value["restoration"], "prepared_state_verified": True, "server_started": False}
            ):
                raise host.InstallError("original prepared rollback source changed")
            if value["phase"] == "restored":
                with self._state_leases(live, uid):
                    marker = live / "server-rollback-blocked.json"
                    expected = host.canonical({**pointer, "rollback_id": rollback_id, "phase": "fenced"})
                    if marker.exists() or marker.is_symlink():
                        if self._hash(marker, 8192, parent_uid=uid)[1] != hashlib.sha256(expected).hexdigest():
                            raise host.InstallError("original target rollback application fence changed")
                    else:
                        host.atomic_write(marker, expected)
                    value["displaced"] = self._inventory(live, uid)
                    advance("displacing")
                    self._rename(live, displaced)
            if value["phase"] == "displacing":
                if not displaced.exists() and not displaced.is_symlink():
                    with self._state_leases(live, uid):
                        if self._inventory(live, uid) != value["displaced"]:
                            raise host.InstallError("original target changed before displacement")
                        self._rename(live, displaced)
                self.inspector._directory(displaced, uid=displaced.lstat().st_uid, private=True)
                if displaced.lstat().st_uid not in {uid, os.getuid()} or self._inventory(displaced, uid) != value["displaced"]:
                    raise host.InstallError("original displaced target evidence changed")
                os.chown(displaced, os.getuid(), -1)
                os.chmod(displaced, 0o700)
                fd = os.open(displaced, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if not live.exists() and not live.is_symlink():
                    live.mkdir(mode=0o700)
                self.inspector._directory(live, private=True)
                self._rollback_placeholder(state, value, create=True)
                fd = os.open(live.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                advance("prepared")
            self.inspector._directory(displaced, private=True)
            if self._inventory(displaced, uid) != value["displaced"]:
                raise host.InstallError("sealed displaced target evidence changed")
            self._rollback_placeholder(state, value)
            self._stopped(state)
            self._retained(state, operation)
            return {
                "format": 1,
                "operation": "rollback-prepare",
                "operation_id": operation_id,
                "rollback_id": rollback_id,
                "plan_id": confirmed_plan,
                "source": source,
                "target": state["identity"]["target"],
                "phase": "prepared",
                "newer_data_loss_confirmed": True,
                "source_activation_performed": False,
                "rollback_completed": False,
                "server_restart_fenced": True,
                "root_private_backup_retained": True,
                "displaced_state_retained": True,
                "displaced_database_validated": False,
                "runtime_dependency_attestation": False,
                "restoration": value["restoration"],
                "displaced": value["displaced"],
                "recovery_directory": str(operation),
            }

    def _rollback_source(self, state, value, command, *, data_dir=None):
        source = state["identity"]["source"]["release_id"]
        args = self._helper(source, data_dir=data_dir) + [command, "--rollback-id", value["rollback_id"], "--source-release", source]
        expected = dict(value["restoration"])
        if command == "rollback-verify":
            expected.update(prepared_state_verified=True, server_started=False)
        elif command == "rollback-release":
            args.append("--confirm-activation")
            expected.update(phase="released", activation_permitted=True, server_started=False, health_verified=False)
        elif command == "rollback-observe":
            expected.update(phase="released", database_schema_verified=True, server_started=False, health_verified=False)
        else:
            raise host.InstallError("unsupported rollback source observation")
        if host.canonical(self._json(args)) != host.canonical(expected):
            raise host.InstallError("rollback source receipt differs from its original restoration")

    def _rollback_activation_configuration(self, state, operation, value):
        result = self._rollback_bindings(state, operation, value)
        phase = value["phase"]
        fence, dropin = self.root / FENCE, self.root / DROPIN
        if fence.exists() or fence.is_symlink():
            if phase not in {"prepared", "publishing", "switching", "activating"} or host.canonical(self._read(fence)) != host.canonical(
                self._pointer(state)
            ):
                raise host.InstallError("original rollback restart fence changed")
        elif phase in {"prepared", "publishing", "switching"}:
            raise host.InstallError("original rollback restart fence is missing")
        if dropin.exists() or dropin.is_symlink():
            if phase == "completed" or self.inspector._file(dropin, 4096) != FENCE_UNIT.encode():
                raise host.InstallError("original rollback restart condition changed")
        elif phase not in {"finalizing", "completed"}:
            raise host.InstallError("original rollback restart condition is missing")
        if phase in {"switching", "activating", "ready", "finalizing", "completed"}:
            if "server-mail.json" in result[1]:
                mail = result[1]["server-mail.json"]["mail"]
                if mail["input_sha256"] != state["identity"]["configuration"]["mail_input_sha256"]:
                    raise host.InstallError("original rollback mail binding changed")
                self.installer._mail_file(mail, pwd.getpwnam("reasoning").pw_uid)
            elif (self.root / "var/lib/reasoning/mail.json").exists() or (self.root / "var/lib/reasoning/mail.json").is_symlink():
                raise host.InstallError("rollback refuses unbound source mail configuration")
        return result

    def _rollback_custody(self, state, operation, value, uid):
        self._retained(state, operation)
        displaced = operation / "displaced-state"
        self.inspector._directory(displaced, private=True)
        if self._inventory(displaced, uid) != value["displaced"]:
            raise host.InstallError("sealed displaced target evidence changed")
        scratch = self.root / "run" / ("reasoning-rollback-" + value["rollback_id"])
        if scratch.exists() or scratch.is_symlink():
            self.inspector._directory(scratch, private=True)

    def _rollback_readiness(self, state, value):
        self._running_target()
        source = state["identity"]["source"]
        result = self.inspector.health(
            source["public_url"],
            timeout=60,
            require_mail=state["identity"]["configuration"]["mail_input_sha256"] is not None,
            expected_release=source["release_id"],
        )
        if (
            not isinstance(result, dict)
            or result.get("https_ready") is not True
            or result.get("public_url") != source["public_url"]
            or type(result.get("initialized")) is not bool
        ):
            raise host.InstallError("source HTTPS release readiness is unverified")
        self._rollback_source(state, value, "rollback-observe")

    @staticmethod
    def _rollback_receipt(state, value, operation):
        return {
            "format": 1,
            "operation": "rollback-activate",
            "operation_id": state["operation_id"],
            "rollback_id": value["rollback_id"],
            "plan_id": state["plan_id"],
            "source": state["identity"]["source"],
            "target": state["identity"]["target"],
            "phase": "completed",
            "source_activation_performed": True,
            "rollback_completed": True,
            "newer_data_loss_confirmed": True,
            "server_restart_fenced": False,
            "https_release_verified": True,
            "database_schema_verified": True,
            "root_private_backup_retained": True,
            "displaced_state_retained": True,
            "displaced_database_validated": False,
            "runtime_dependency_attestation": False,
            "restoration": value["restoration"],
            "displaced": value["displaced"],
            "recovery_directory": str(operation),
        }

    def rollback_activate(
        self,
        *,
        operation_id,
        rollback_id,
        confirmed_plan,
        source_release,
        target_release,
        activation_confirmed=False,
        skip_platform=False,
    ):
        if activation_confirmed is not True or not all(
            map(_hex, (operation_id, rollback_id, confirmed_plan, source_release, target_release))
        ):
            raise host.InstallError(
                "source activation requires exact original rollback identities and explicit external-effect confirmation"
            )
        self._platform(skip_platform)
        with self._bootstrap() as fd:
            uid = pwd.getpwnam("reasoning").pw_uid
            if not skip_platform and uid == os.getuid():
                raise host.InstallError("the application account must not own root rollback evidence")
            operation = self.directory / "server-upgrades" / operation_id
            path = operation / "rollback.json"
            value = self._read(path)
            state, _, manifest = self._activation_inputs(
                operation_id, confirmed_plan, target_release, completed_rollback=value.get("phase") == "completed", rollback_operation=True
            )
            self._rollback_record(state, rollback_id, value)
            source = state["identity"]["source"]
            if (
                value["phase"] not in {"prepared", *ROLLBACK_ACTIVATION_PHASES}
                or source_release != source["release_id"]
                or not {"server-rollback-state-v1", "server-release-identity-v1"} <= set(state["source_features"])
                or "server-rollback-activation-v1" not in manifest.get("installation_features", [])
            ):
                raise host.InstallError("source activation requires the original fully prepared, supported rollback")
            if value["phase"] != "completed" and not self.active.exists() and not self.active.is_symlink():
                raise host.InstallError("original rollback active ownership is missing")
            self._rollback_custody(state, operation, value, uid)
            before, replacements, unit_path, old_unit, source_unit = self._rollback_activation_configuration(state, operation, value)
            stage = self.root / "var/lib" / ("reasoning-rollback-" + rollback_id)
            candidate, live = stage / "state", self.root / "var/lib/reasoning"

            def advance(phase):
                value["phase"] = phase
                self._write(path, value)

            if value["phase"] == "prepared":
                self.inspector._directory(stage, uid=uid, private=True)
                self._stopped(state)
                self._rollback_placeholder(state, value)
                self._rollback_source(state, value, "rollback-verify", data_dir=candidate)
                advance("publishing")
            if value["phase"] == "publishing":
                self._stopped(state)
                info = stage.lstat()
                self.inspector._directory(stage, uid=info.st_uid)
                if (
                    info.st_uid not in {uid, os.getuid()}
                    or stat.S_IMODE(info.st_mode) not in {0o700, 0o711}
                    or (stat.S_IMODE(info.st_mode) == 0o711 and info.st_uid != os.getuid())
                    or sorted(item.name for item in stage.iterdir()) != ["state"]
                ):
                    raise host.InstallError("rollback publication stage has an unexpected owner")
                if (candidate / "server-rollback-blocked.json").exists() or (candidate / "server-rollback-blocked.json").is_symlink():
                    # A lost exchange response is recognized by BOTH locations,
                    # never by an absent candidate or by unowned live state.
                    self._rollback_placeholder(state, value, location=candidate)
                    self._rollback_source(state, value, "rollback-verify")
                else:
                    # Root takes custody of the parent before publication.
                    # Traverse-only access lets the matching service CLI verify
                    # its private child, but cannot rename/replace that child.
                    os.chown(stage, os.getuid(), -1)
                    os.chmod(stage, 0o711)
                    stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(stage_fd)
                    finally:
                        os.close(stage_fd)
                    self._rollback_placeholder(state, value)
                    self._rollback_source(state, value, "rollback-verify", data_dir=candidate)
                    with self._state_leases(candidate, uid):
                        self._exchange(candidate, live)
                    self._rollback_placeholder(state, value, location=candidate)
                    self._rollback_source(state, value, "rollback-verify")
                os.chown(stage, os.getuid(), -1)
                os.chmod(stage, 0o700)
                stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
                advance("switching")
            self.inspector._directory(stage, private=True)
            self._rollback_placeholder(state, value, location=candidate)
            if value["phase"] == "switching":
                self.command(["systemctl", "daemon-reload"])
                self._stopped(state)
                self._rollback_source(state, value, "rollback-verify")
                for name, replacement in replacements.items():
                    self._transition(self.directory / name, before[name], host.canonical(replacement))
                self._transition(unit_path, old_unit, source_unit, private=False)
                self.command(["systemctl", "daemon-reload"])
                self._stopped(state)
                advance("activating")
            if value["phase"] == "activating":
                if (self.root / FENCE).exists() or (self.root / FENCE).is_symlink():
                    self._stopped(state)
                    # This acknowledgment may be lost after the source guard
                    # is released. Under the persistent fence, replay is safe.
                    self._rollback_source(state, value, "rollback-release")
                    self._remove(self.root / FENCE, host.canonical(self._pointer(state)))
                else:
                    # A running source holds a shared lease. Never reacquire
                    # an exclusive release lease or compare its old fingerprint.
                    self._rollback_source(state, value, "rollback-observe")
                self.command(["systemctl", "start", "reasoning-server"])
                self._rollback_readiness(state, value)
                advance("ready")
            if value["phase"] == "ready":
                self._rollback_readiness(state, value)
                self._transition(
                    self.directory / "installation.json",
                    host.canonical(replacements["installation.json"]),
                    host.canonical({**source, "phase": "services_started"}),
                )
                advance("finalizing")
            if value["phase"] == "finalizing":
                self._rollback_source(state, value, "rollback-observe")
                self._remove(self.root / DROPIN, FENCE_UNIT.encode(), private=False)
                self.command(["systemctl", "daemon-reload"])
            self._rollback_activation_configuration(state, operation, value)
            self._rollback_custody(state, operation, value, uid)
            self._rollback_readiness(state, value)
            source_manifest, _, _ = host.inspect_bundle(operation / "source.tar.gz", source["bundle_sha256"])
            report = self.inspector.inspect(
                source_manifest, source_release, source["bundle_sha256"], source["public_url"], owned_bootstrap_fd=fd
            )
            if report["all_checks_passed"] is not True or host.canonical(report["configuration"]) != host.canonical(
                state["identity"]["configuration"]
            ):
                raise host.InstallError("final source installation diagnosis failed; preserve rollback and live data")
            receipt = self._rollback_receipt(state, value, operation)
            completion = operation / "rollback-completion.json"
            if completion.exists() or completion.is_symlink():
                if host.canonical(self._read(completion)) != host.canonical(receipt):
                    raise host.InstallError("original completed rollback history changed")
            elif value["phase"] == "completed":
                raise host.InstallError("original completed rollback history is missing")
            else:
                self._write(completion, receipt)
            if value["phase"] != "completed":
                advance("completed")
            self._remove(self.active, host.canonical(self._pointer(state)))
            return receipt

    def prepare(
        self,
        source_bundle,
        target_bundle,
        source_checksum,
        target_checksum,
        origin,
        *,
        operation_id,
        confirmed_plan,
        workers_fenced=False,
        maximum=8 * 1024**3,
        skip_platform=False,
    ):
        if workers_fenced is not True or not all(map(_hex, (operation_id, confirmed_plan, source_checksum, target_checksum))):
            raise host.InstallError(
                "upgrade preparation requires exact identities and explicit worker/controller/outbound fencing confirmation"
            )
        if type(maximum) is not int or not 1024**2 <= maximum <= 64 * 1024**3:
            raise host.InstallError("invalid retained backup byte allowance")
        self._platform(skip_platform)
        with self._bootstrap() as fd:
            return self._prepare(
                source_bundle,
                target_bundle,
                source_checksum,
                target_checksum,
                origin,
                operation_id,
                confirmed_plan,
                maximum,
                fd,
                skip_platform,
            )

    def _platform(self, skip_platform):
        if not skip_platform:
            if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() not in {"amd64", "x86_64"}:
                raise host.InstallError("managed release changes require root on Linux amd64")
            fields = dict(line.split("=", 1) for line in (self.root / "etc/os-release").read_text().splitlines() if "=" in line)
            if fields.get("ID", "").strip('"') != "ubuntu" or fields.get("VERSION_ID", "").strip('"') != "24.04":
                raise host.InstallError("managed release changes support Ubuntu 24.04")

    @contextmanager
    def _bootstrap(self):
        self.inspector._directory(self.directory, private=True)
        self.inspector._file(self.directory / "bootstrap.lock", 0, private=True)
        fd = os.open(self.directory / "bootstrap.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            original = (self.directory / "bootstrap.lock").lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size != 0
                or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)
            ):
                raise host.InstallError("original protected bootstrap lock changed")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield fd
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError) as exc:
            raise host.InstallError("release change failed; retain original operation, releases, backup and recovery evidence") from exc
        finally:
            os.close(fd)

    def _prepare(
        self,
        source_bundle,
        target_bundle,
        source_checksum,
        target_checksum,
        origin,
        operation_id,
        confirmed_plan,
        maximum,
        fd,
        skip_platform,
    ):
        pending = self.directory / "active-server-recovery.json"
        if pending.exists() or pending.is_symlink():
            raise host.InstallError("replacement recovery owns this host; resume its original operation")
        operation = self.directory / "server-upgrades" / operation_id
        if (operation / "rollback.json").exists() or (operation / "rollback.json").is_symlink():
            raise host.InstallError("rollback owns this operation; upgrade preparation cannot replay it")
        path = operation / "operation.json"
        state = self._read(path) if path.exists() or path.is_symlink() else None
        if state is not None and state.get("phase") in ACTIVATION_PHASES:
            raise host.InstallError("activation may have external effects; preparation cannot replay the old snapshot")
        pointer = {"format": 1, "operation_id": operation_id, "plan_id": confirmed_plan}
        if self.active.exists() or self.active.is_symlink():
            if host.canonical(self._read(self.active)) != host.canonical(pointer) or state is None:
                raise host.InstallError("another or missing original upgrade owns this installation")
        elif state is not None and state.get("phase") != "preparing":
            raise host.InstallError("original upgrade ownership record is missing; preserve recovery evidence")
        archives = []
        for name, supplied, expected in (
            ("source.tar.gz", source_bundle, source_checksum),
            ("target.tar.gz", target_bundle, target_checksum),
        ):
            retained = operation / name
            archive = retained if retained.exists() or retained.is_symlink() else supplied.absolute()
            self.inspector._file(archive, host.MAX_BUNDLE)
            manifest, release, checksum = host.inspect_bundle(archive, expected)
            archives.append((archive, manifest, release, checksum))
        (source_archive, source_manifest, source_release, _), (target_archive, target_manifest, target_release, _) = archives
        source_binding = server_operations.release_binding(source_manifest, source_release, source_checksum, origin)
        target_binding = server_operations.release_binding(target_manifest, target_release, target_checksum, origin)
        uid = pwd.getpwnam("reasoning").pw_uid
        if not skip_platform and uid == os.getuid():
            raise host.InstallError("the application service account must not own root upgrade recovery material")
        self._rollback_upgrade_support(target_manifest)
        if state is None:
            target_path = self.root / "opt/reasoning/releases" / target_release
            if target_path.exists() or target_path.is_symlink():
                raise host.InstallError("upgrade cannot adopt an existing target release without its original operation")
            report = self.inspector.inspect(source_manifest, source_release, source_checksum, origin, owned_bootstrap_fd=fd)
            plan = server_operations.upgrade_plan(report, source_manifest, target_manifest, target_release, target_checksum)
            if (
                not plan["preflight_checks_passed"]
                or plan["plan_id"] != confirmed_plan
                or source_manifest["database_schema"] < 13
                or not {"server-maintenance-v1", "server-upgrade-preparation-v1"} <= set(target_manifest.get("installation_features", []))
            ):
                raise host.InstallError("confirmed upgrade preflight differs or target offline maintenance is unsupported")
            metadata = {}
            for name in ("installation.json", "server-mail.json", "server-provisioning.json"):
                current = self.directory / name
                metadata[name] = self._hash(current, host.MAX_MANIFEST)[1] if current.exists() or current.is_symlink() else None
            self.installer._directory(str(operation.relative_to(self.root)), 0o700)
            state = {
                "format": 1,
                "operation_id": operation_id,
                "plan_id": confirmed_plan,
                "identity": {key: plan[key] for key in IDENTITY_FIELDS},
                "source_features": source_manifest.get("installation_features", []),
                "phase": "preparing",
                "maximum_bytes": maximum,
                "metadata": metadata,
                "backup_key_sha256": None,
                "backup": None,
                "migration": None,
            }
            self._write(path, state)
        if (
            set(state)
            != {
                "format",
                "operation_id",
                "plan_id",
                "identity",
                "source_features",
                "phase",
                "maximum_bytes",
                "metadata",
                "backup_key_sha256",
                "backup",
                "migration",
            }
            or type(state["format"]) is not int
            or state["format"] != 1
            or state["operation_id"] != operation_id
            or state["plan_id"] != confirmed_plan
            or state["phase"] not in PHASES
            or type(state["maximum_bytes"]) is not int
            or state["maximum_bytes"] != maximum
            or state["source_features"] != source_manifest.get("installation_features", [])
            or set(state["identity"]) != set(IDENTITY_FIELDS)
            or hashlib.sha256(host.canonical(state["identity"])).hexdigest() != confirmed_plan
            or state["identity"]["source"] != source_binding
            or state["identity"]["target"] != target_binding
            or state["identity"]["source_database_schema"] != source_manifest["database_schema"]
            or state["identity"]["target_database_schema"] != target_manifest["database_schema"]
            or set(state["metadata"]) != {"installation.json", "server-mail.json", "server-provisioning.json"}
            or any(value is not None and not _hex(value) for value in state["metadata"].values())
        ):
            raise host.InstallError("upgrade journal differs from original exact inputs")
        self._write(self.active, pointer)
        self._copy(source_archive, operation / "source.tar.gz", source_checksum)
        self._copy(target_archive, operation / "target.tar.gz", target_checksum)
        self.inspector._payload(source_manifest, source_release)
        self.inspector._python_sources(source_manifest, source_release)
        self._configuration(state, operation)

        def advance(phase):
            state["phase"] = phase
            self._write(path, state)

        if state["phase"] == "preparing":
            self._target(target_manifest, target_release, operation / "target.tar.gz")
            advance("target_ready")
        self.inspector._payload(target_manifest, target_release)
        self.inspector._python_sources(target_manifest, target_release)
        if state["phase"] == "target_ready":
            advance("fencing")
        if state["phase"] == "fencing":
            self._fence(state)
            advance("source_stopped")
        self._stopped(state)
        if state["phase"] == "source_stopped":
            self._snapshot(state, operation, uid)
            advance("backup_retained")
        self._retained(state, operation)
        if state["phase"] == "backup_retained":
            advance("migrating")
        if state["phase"] == "migrating":
            self._migration(state, operation, uid)
            self._retained(state, operation)
            advance("sealing")
        if state["phase"] == "sealing":
            self._verify_prepared(state)
            for scratch in self._scratch_paths(state):
                if scratch.exists() or scratch.is_symlink():
                    self.inspector._directory(scratch, uid=scratch.lstat().st_uid, private=True)
                    if scratch.lstat().st_uid not in {uid, os.getuid()}:
                        raise host.InstallError("upgrade scratch belongs to an unexpected account")
                    os.chown(scratch, os.getuid(), -1)
                    os.chmod(scratch, 0o700)
                    directory_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            advance("prepared")
        self._sealed(state)
        self._verify_prepared(state)
        self._retained(state, operation)
        self._stopped(state)
        return {
            "format": 1,
            "operation": "upgrade-prepare",
            "operation_id": operation_id,
            "plan_id": confirmed_plan,
            "source": source_binding,
            "target": target_binding,
            "phase": "prepared",
            "server_restart_fenced": True,
            "root_private_backup_retained": True,
            "activation_performed": False,
            "upgrade_completed": False,
            "runtime_dependency_attestation": False,
            "recovery_directory": str(operation),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path)
    parser.add_argument("--target-bundle", type=Path)
    parser.add_argument("--source-sha256")
    parser.add_argument("--target-sha256")
    parser.add_argument("--public-url")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--confirm-plan", required=True)
    parser.add_argument("--confirm-workers-fenced", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--activate", action="store_true", help="activate the retained original prepared target; external effects may occur")
    modes.add_argument(
        "--rollback-prepare", action="store_true", help="prepare a fenced source restoration and retain displaced target state"
    )
    modes.add_argument(
        "--rollback-activate", action="store_true", help="activate the original prepared source restoration; external effects may occur"
    )
    parser.add_argument("--rollback-id")
    parser.add_argument("--confirm-source-release")
    parser.add_argument("--confirm-newer-data-loss", action="store_true")
    parser.add_argument("--confirm-activation", action="store_true")
    parser.add_argument("--confirm-target-release")
    parser.add_argument("--maximum-bytes", type=int, help="preparation-only retained backup byte allowance (default: 8589934592)")
    args = parser.parse_args(argv)
    preparation_inputs = (args.source_bundle, args.target_bundle, args.source_sha256, args.target_sha256, args.public_url)
    rollback_options = args.rollback_id is not None or args.confirm_source_release is not None or args.confirm_newer_data_loss
    if args.activate:
        if (
            any(value is not None for value in preparation_inputs)
            or args.confirm_workers_fenced
            or args.maximum_bytes is not None
            or rollback_options
        ):
            parser.error("activation uses retained original inputs; omit preparation-only options")
        if not args.confirm_activation or args.confirm_target_release is None:
            parser.error("activation requires --confirm-activation and --confirm-target-release")
    elif args.rollback_activate:
        if (
            any(value is not None for value in preparation_inputs)
            or args.maximum_bytes is not None
            or args.confirm_workers_fenced
            or args.confirm_newer_data_loss
        ):
            parser.error("source activation uses retained original rollback inputs; omit preparation-only options")
        if (
            args.rollback_id is None
            or args.confirm_source_release is None
            or args.confirm_target_release is None
            or not args.confirm_activation
        ):
            parser.error("source activation requires exact rollback/source/target identities and --confirm-activation")
    elif args.rollback_prepare:
        if any(value is not None for value in preparation_inputs) or args.confirm_activation or args.maximum_bytes is not None:
            parser.error("rollback uses retained original inputs; omit upgrade preparation/activation options")
        if (
            args.rollback_id is None
            or args.confirm_source_release is None
            or args.confirm_target_release is None
            or not args.confirm_newer_data_loss
            or not args.confirm_workers_fenced
        ):
            parser.error(
                "rollback requires exact rollback/source/target identities and both execution-fencing/newer-data-loss acknowledgments"
            )
    elif (
        any(value is None for value in preparation_inputs)
        or args.confirm_activation
        or args.confirm_target_release is not None
        or rollback_options
    ):
        parser.error("preparation requires source/target bundles, hashes and URL; activation options require --activate")
    try:
        engine = HostUpgrade()
        if args.activate:
            receipt = engine.activate(
                operation_id=args.operation_id,
                confirmed_plan=args.confirm_plan,
                target_release=args.confirm_target_release,
                activation_confirmed=args.confirm_activation,
            )
        elif args.rollback_prepare:
            receipt = engine.rollback_prepare(
                operation_id=args.operation_id,
                rollback_id=args.rollback_id,
                confirmed_plan=args.confirm_plan,
                source_release=args.confirm_source_release,
                target_release=args.confirm_target_release,
                workers_fenced=args.confirm_workers_fenced,
                data_loss_confirmed=args.confirm_newer_data_loss,
            )
        elif args.rollback_activate:
            receipt = engine.rollback_activate(
                operation_id=args.operation_id,
                rollback_id=args.rollback_id,
                confirmed_plan=args.confirm_plan,
                source_release=args.confirm_source_release,
                target_release=args.confirm_target_release,
                activation_confirmed=args.confirm_activation,
            )
        else:
            receipt = engine.prepare(
                args.source_bundle,
                args.target_bundle,
                args.source_sha256,
                args.target_sha256,
                args.public_url,
                operation_id=args.operation_id,
                confirmed_plan=args.confirm_plan,
                workers_fenced=args.confirm_workers_fenced,
                maximum=8 * 1024**3 if args.maximum_bytes is None else args.maximum_bytes,
            )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (host.InstallError, OSError, ValueError) as exc:
        message = (
            str(exc) if isinstance(exc, host.InstallError) else "release change failed; preserve original recovery inputs and live data"
        )
        parser.exit(2, "reasoning-deploy: " + message + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
