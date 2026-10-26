"""Authenticated, bounded backups of SQLite state and referenced immutable chunks."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import struct
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..deployment.host import InstallError, atomic_rename, canonical
from ..deployment.private import protected_parent, read_private
from .migrations import MIGRATIONS
from .schema_operations import sqlite_migration
from .security import SecretBox, token
from .workflow_inputs import read_inputs
from .workflow_store import read_definition

MAGIC = b"RNBK0001"
FRAME = 1024 * 1024
MANIFEST_LIMIT = 32 * FRAME
FILE_LIMIT = 262_147
DEFAULT_BYTES = 8 * 1024**3
MAX_BYTES = 64 * 1024**3
CHUNK_BYTES = 32_768
BASE_FILES = {"server.sqlite3", "encryption.key", "setup-token"}
CHUNK_NAME = re.compile(r"artifacts/([a-f0-9]{32})/(0|[1-9][0-9]{0,4})\Z")
CLOUD_NAME = re.compile(
    r"cloud-bots/[a-f0-9]{32}/[a-f0-9]{32}/[a-f0-9]{32}/(?:job\.json|"
    r"artifact/reasoning_ledger-[A-Za-z0-9_.-]{1,160}\.whl|host/(?:"
    r"plan\.json|operator\.json|operator-key|digitalocean\.json|digitalocean-ssh\.json|"
    r"digitalocean-ssh-access\.json|installation\.json|install-input\.json))\Z")
CLOUD_BYTES = 256 * 1024
CLOUD_WHEEL_BYTES = 32 * 1024**2
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class BackupError(ValueError):
    """An operator-facing failure without plaintext secrets or SQL values."""


def _cloud_files(root: Path) -> dict[str, tuple[int, str]]:
    """Only bounded private owner journals, never arbitrary filesystem data."""
    base = root / "cloud-bots"
    if not base.exists() and not base.is_symlink():
        return {}
    _directory(base)
    files = {}
    for path in base.rglob("*"):
        if path.is_symlink():
            raise BackupError("cloud bot state must not contain symlinks")
        if path.is_dir():
            _directory(path)
            continue
        if path.name == "provision.lock":
            with _input(path, 0):
                pass
            continue
        name = path.relative_to(root).as_posix()
        if not CLOUD_NAME.fullmatch(name):
            raise BackupError("cloud bot state contains an unsupported file")
        files[name] = _hash_file(path, CLOUD_WHEEL_BYTES if "/artifact/" in name else CLOUD_BYTES)
        if len(files) > 10000:
            raise BackupError("cloud bot state exceeds its file allowance")
    return files


def _allowance(maximum: int) -> None:
    if type(maximum) is not int or not 1024 * 1024 <= maximum <= MAX_BYTES:
        raise BackupError("backup byte allowance must be between 1 MiB and 64 GiB")


def _directory(path: Path) -> None:
    protected_parent(path.absolute(), writable=False)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("backup state must be an owner-owned private directory, not a symlink")


@contextmanager
def _input(path: Path, maximum: int):
    protected_parent(path.absolute(), writable=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise BackupError("backup inputs must be bounded owner-owned private regular files")
        yield stream, info.st_size


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_file(source: Path, target: Path) -> None:
    # Publish a complete fsynced file, never truncate or replace an existing path.
    os.link(source, target, follow_symlinks=False)
    source.unlink()
    _sync_directory(target.parent)


def create_key(output: Path) -> dict:
    output = output.absolute()
    try:
        protected_parent(output)
        with tempfile.TemporaryDirectory(prefix=".reasoning-backup-", dir=output.parent) as temporary:
            path = Path(temporary) / "key"
            _write_new(path, secrets.token_bytes(32))
            _publish_file(path, output)
        return {"key_file": str(output), "keep_off_host": True}
    except (OSError, InstallError) as exc:
        raise BackupError("cannot publish backup key; use a new filename in a protected directory") from exc


def _key(path: Path) -> bytes:
    key = read_private(path, 32)
    if len(key) != 32:
        raise BackupError("backup key must contain exactly 32 random bytes; passphrases are not supported")
    return key


def _box(key: bytes, header: bytes) -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=header[8:24], info=b"reasoning-backup-v1").derive(key)
    return AESGCM(derived)


class _Encryptor:
    def __init__(self, stream, key: bytes):
        self.stream = stream
        self.header = MAGIC + secrets.token_bytes(16) + secrets.token_bytes(4)
        self.box = _box(key, self.header)
        self.counter = 0
        self.buffer = bytearray()
        self.stream.write(self.header)

    def _frame(self, body: bytes) -> None:
        sequence = struct.pack(">Q", self.counter)
        size = struct.pack(">I", len(body))
        self.stream.write(size + self.box.encrypt(self.header[24:] + sequence, body, self.header + sequence + size))
        self.counter += 1

    def write(self, body: bytes) -> None:
        self.buffer.extend(body)
        while len(self.buffer) >= FRAME:
            self._frame(bytes(self.buffer[:FRAME]))
            del self.buffer[:FRAME]

    def finish(self) -> None:
        if self.buffer:
            self._frame(bytes(self.buffer))
            self.buffer.clear()
        # Authentication covers ordering, lengths, header, and a mandatory EOF marker.
        self._frame(b"")


def _exact(stream, size: int) -> bytes:
    body = stream.read(size)
    if len(body) != size:
        raise BackupError("backup is truncated")
    return body


class _Decryptor:
    def __init__(self, stream, key: bytes):
        self.stream = stream
        self.header = _exact(stream, 28)
        if self.header[:8] != MAGIC:
            raise BackupError("unsupported backup format")
        self.box = _box(key, self.header)
        self.counter = 0
        self.buffer = bytearray()
        self.finished = False

    def read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            if self.buffer:
                count = min(size - len(result), len(self.buffer))
                result.extend(self.buffer[:count])
                del self.buffer[:count]
            elif self.finished:
                break
            else:
                packed = _exact(self.stream, 4)
                length = struct.unpack(">I", packed)[0]
                if length > FRAME:
                    raise BackupError("backup frame exceeds its byte allowance")
                sequence = struct.pack(">Q", self.counter)
                try:
                    self.buffer.extend(
                        self.box.decrypt(self.header[24:] + sequence, _exact(self.stream, length + 16), self.header + sequence + packed)
                    )
                except InvalidTag as exc:
                    raise BackupError("backup authentication failed; check key and archive integrity") from exc
                self.counter += 1
                if length == 0:
                    self.finished = True
                    if self.stream.read(1):
                        raise BackupError("backup contains trailing data")
        return bytes(result)


@contextmanager
def _connect(path: Path, *, writable: bool = False):
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=15)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        if not writable:
            db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 300
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        with db:
            yield db
    finally:
        db.close()


def _schema(db, *, schema_version: int | None = None) -> None:
    version = len(MIGRATIONS) if schema_version is None else schema_version
    if type(version) is not int or not 1 <= version <= len(MIGRATIONS):
        raise BackupError("unsupported database schema verification")
    if db.execute("PRAGMA user_version").fetchone()[0] != version:
        raise BackupError("backup requires this release's exact database schema; use the matching release")
    with closing(sqlite3.connect(":memory:")) as expected:
        for migration in MIGRATIONS[:version]:
            for statement in migration:
                sqlite_migration(expected, statement)
        query = "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name"
        if db.execute(query).fetchall() != expected.execute(query).fetchall():
            raise BackupError("backup database schema differs from this release")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
        raise BackupError("backup database integrity check failed")


def _chunks(db) -> dict[str, tuple[int, str]]:
    chunks = {}
    for artifact, index, size, digest in db.execute("SELECT artifact_id,chunk_index,size_bytes,digest FROM compute_artifact_chunks"):
        name = f"artifacts/{artifact}/{index}"
        if (
            not CHUNK_NAME.fullmatch(name)
            or type(index) is not int
            or not 0 <= index < 32_768
            or type(size) is not int
            or not 0 <= size <= CHUNK_BYTES
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not SHA256.fullmatch(digest[7:])
            or len(chunks) >= FILE_LIMIT - 3
        ):
            raise BackupError("invalid or excessive database artifact references")
        chunks[name] = (size, digest[7:])
    return chunks


def _hash_file(path: Path, maximum: int, consume=None) -> tuple[int, str]:
    with _input(path, maximum) as (stream, declared):
        digest, total = hashlib.sha256(), 0
        while body := stream.read(FRAME):
            total += len(body)
            if total > declared:
                raise BackupError("backup input changed while reading")
            digest.update(body)
            if consume:
                consume(body)
        if total != declared:
            raise BackupError("backup input changed while reading")
        return total, digest.hexdigest()


def _validate_state(root: Path, maximum: int, *, schema_version: int | None = None) -> dict[str, tuple[int, str]]:
    _directory(root)
    installation_key = read_private(root / "encryption.key", 32)
    if len(installation_key) != 32:
        raise BackupError("invalid installation encryption key")
    box = SecretBox(installation_key)
    setup = read_private(root / "setup-token", 128)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43}", setup):
        raise BackupError("invalid installation setup credential")
    with _input(root / "server.sqlite3", maximum):
        pass
    with _connect(root / "server.sqlite3") as db:
        _schema(db, schema_version=schema_version)
        for source, definition_digest in db.execute("SELECT definition_json,definition_digest FROM workflow_versions"):
            try:
                read_definition(source, definition_digest)
            except ValueError as exc:
                raise BackupError("backup workflow definition integrity check failed") from exc
        for source, inputs_digest in db.execute("SELECT inputs_json,inputs_digest FROM workflow_node_attempts"):
            if source is None and inputs_digest is None:
                continue
            try:
                read_inputs(source, inputs_digest)
            except ValueError as exc:
                raise BackupError("backup workflow input integrity check failed") from exc
        for key_id, org, provider, ciphertext in db.execute("SELECT id,organization_id,provider,ciphertext FROM provider_keys"):
            try:
                box.open(ciphertext, organization_id=org, provider=provider, key_id=key_id)
            except (InvalidTag, ValueError, TypeError) as exc:
                raise BackupError("backup cannot decrypt stored provider credentials") from exc
        for invitation_id, org, ciphertext in db.execute(
            "SELECT invitation_id,organization_id,ciphertext FROM invitation_mail WHERE ciphertext IS NOT NULL"
        ):
            try:
                box.open_mail(ciphertext, organization_id=org, invitation_id=invitation_id)
            except (InvalidTag, ValueError, TypeError) as exc:
                raise BackupError("backup cannot decrypt stored invitation email") from exc
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_verifications'").fetchone():
            for identity, user, ciphertext in db.execute(
                "SELECT id,user_id,ciphertext FROM email_verifications WHERE ciphertext IS NOT NULL"
            ):
                try:
                    box.open_mail(ciphertext, organization_id=user, invitation_id=identity)
                except (InvalidTag, ValueError, TypeError) as exc:
                    raise BackupError("backup cannot decrypt stored email verification") from exc
        for identity, org, ciphertext in db.execute("SELECT id,organization_id,ciphertext FROM bot_webhooks WHERE ciphertext IS NOT NULL"):
            try:
                box.open_webhook(ciphertext, organization_id=org, webhook_id=identity)
            except (InvalidTag, ValueError, TypeError) as exc:
                raise BackupError("backup cannot decrypt stored webhook secrets") from exc
        from .webhook_store import captured_body

        # _connect uses tuple rows for schema validation; preserve that contract.
        columns = [row[1] for row in db.execute("PRAGMA table_info(bot_webhook_deliveries)")]
        for values in db.execute("SELECT * FROM bot_webhook_deliveries WHERE state IN ('queued','sending')"):
            row = dict(zip(columns, values, strict=True))
            try:
                captured_body(db, row)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise BackupError("backup webhook capture integrity check failed") from exc
        chunks = _chunks(db)
        by_artifact: dict[str, set[str]] = {}
        for name, expected in chunks.items():
            by_artifact.setdefault(name.split("/")[1], set()).add(name)
            _directory(root / "artifacts")
            _directory((root / name).parent)
            if _hash_file(root / name, CHUNK_BYTES) != expected:
                raise BackupError("backup artifact chunk integrity check failed")
        for artifact, size, digest, committed in db.execute("SELECT id,size_bytes,digest,committed_at FROM compute_artifacts"):
            if committed is None:
                continue
            if (
                type(size) is not int
                or not 0 <= size <= 1024**3
                or not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            ):
                raise BackupError("invalid committed artifact metadata")
            expected_names = [f"artifacts/{artifact}/{index}" for index in range((size + CHUNK_BYTES - 1) // CHUNK_BYTES)]
            actual_names = by_artifact.get(artifact, set())
            if actual_names != set(expected_names):
                raise BackupError("committed artifact is missing chunks or has excess chunks")
            full, remaining = hashlib.sha256(), size
            for name in expected_names:
                length, _ = _hash_file(root / name, CHUNK_BYTES, full.update)
                if length != min(CHUNK_BYTES, remaining):
                    raise BackupError("committed artifact chunk has an invalid length")
                remaining -= length
            if digest != "sha256:" + full.hexdigest():
                raise BackupError("committed artifact integrity check failed")
        return chunks


def _snapshot(root: Path, destination: Path, maximum: int) -> None:
    _directory(root)
    configuration = root / "postgresql.json"
    if configuration.exists() or configuration.is_symlink():
        from .postgres import Database, PostgresConfig

        _write_new(destination / "server.sqlite3", b"")
        key = read_private(root / "encryption.key", 32)
        fingerprint = hmac.digest(key, b"reasoning-postgres-installation-v1", "sha256").hex()
        try:
            Database(PostgresConfig.read(configuration)).snapshot(destination / "server.sqlite3", fingerprint, MIGRATIONS, maximum)
        except ValueError as exc:
            raise BackupError(str(exc)) from None
        for name, limit in (("encryption.key", 32), ("setup-token", 128)):
            _write_new(destination / name, read_private(root / name, limit))
        return
    with _input(root / "server.sqlite3", maximum):
        pass
    for suffix in ("-wal", "-shm"):
        sibling = root / ("server.sqlite3" + suffix)
        if sibling.exists() or sibling.is_symlink():
            with _input(sibling, MAX_BYTES):
                pass
    _write_new(destination / "server.sqlite3", b"")
    deadline = time.monotonic() + 300

    def progress(status, remaining, total):
        if time.monotonic() > deadline or total * page_size > maximum:
            raise BackupError("SQLite backup exceeds its time or byte allowance")

    with _connect(root / "server.sqlite3") as source, closing(sqlite3.connect(destination / "server.sqlite3")) as target:
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        source.backup(target, pages=256, progress=progress, sleep=0.05)
        # A standalone snapshot must not depend on omitted WAL files.
        target.execute("PRAGMA journal_mode=DELETE")
    for name, limit in (("encryption.key", 32), ("setup-token", 128)):
        _write_new(destination / name, read_private(root / name, limit))


def backup(root: Path, output: Path, key_file: Path, *, maximum: int = DEFAULT_BYTES) -> dict:
    root, output = root.absolute(), output.absolute()
    _allowance(maximum)
    try:
        protected_parent(output)
        if output.exists() or output.is_symlink():
            raise BackupError("backup destination already exists; choose a new filename")
        key = _key(key_file)
        # Initialization can change schema/key state; ordinary transactions remain online.
        with _input(root / "initialization.lock", 0) as (lock, _):
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with tempfile.TemporaryDirectory(prefix=".reasoning-backup-", dir=output.parent) as temporary:
                stage = Path(temporary)
                _snapshot(root, stage, maximum)
                with _connect(stage / "server.sqlite3") as db:
                    _schema(db)
                    chunks = _chunks(db)
                cloud = _cloud_files(root)
                files = []
                for name in sorted(BASE_FILES | chunks.keys() | cloud.keys()):
                    path = stage / name if name in BASE_FILES else root / name
                    if name in chunks:
                        _directory(root / "artifacts")
                        _directory(path.parent)
                    limit = (maximum if name == "server.sqlite3" else CLOUD_WHEEL_BYTES if name in cloud and "/artifact/" in name
                             else CLOUD_BYTES if name in cloud else CHUNK_BYTES)
                    size, digest = _hash_file(path, limit)
                    if name in cloud and (size, digest) != cloud[name]:
                        raise BackupError("cloud bot journal changed during backup; retry after setup finishes")
                    if name in chunks and (size, digest) != chunks[name]:
                        raise BackupError("source artifact chunk integrity check failed")
                    files.append({"name": name, "size": size, "sha256": digest})
                manifest = {"format": 1, "schema": len(MIGRATIONS), "created_at": int(time.time()), "files": files}
                body = canonical(manifest)
                if len(body) > MANIFEST_LIMIT or sum(file["size"] for file in files) > maximum:
                    raise BackupError("backup payload exceeds its byte allowance")
                archive = stage / "archive"
                with archive.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    encrypted = _Encryptor(stream, key)
                    encrypted.write(struct.pack(">I", len(body)))
                    encrypted.write(body)
                    for file in files:
                        name = file["name"]
                        path = stage / name if name in BASE_FILES else root / name
                        if _hash_file(path, file["size"], encrypted.write) != (file["size"], file["sha256"]):
                            raise BackupError("backup source changed before encryption completed")
                    encrypted.finish()
                    stream.flush()
                    os.fsync(stream.fileno())
                # Verify the actual encrypted output and cross-file invariants before publication.
                size, digest = _hash_file(archive, _archive_limit(maximum))
                with tempfile.TemporaryDirectory(prefix="verify-", dir=stage) as verification:
                    _unpack(archive, key, digest, Path(verification), maximum)
                _publish_file(archive, output)
                return {"backup_file": str(output), "sha256": digest, "bytes": size, **_summary(manifest)}
    except (OSError, InstallError, sqlite3.Error) as exc:
        raise BackupError("backup failed; check protected paths, available space, and database state") from exc


def _archive_limit(maximum: int) -> int:
    payload = maximum + MANIFEST_LIMIT + 4
    return payload + (payload // FRAME + 2) * 20 + 28


def _manifest(body: bytes, maximum: int, *, schema_version: int | None = None) -> dict:
    try:
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or canonical(value) != body
            or set(value) != {"format", "schema", "created_at", "files"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or type(value["schema"]) is not int
            or value["schema"] != (len(MIGRATIONS) if schema_version is None else schema_version)
            or type(value["created_at"]) is not int
            or value["created_at"] < 0
            or not isinstance(value["files"], list)
            or not 3 <= len(value["files"]) <= FILE_LIMIT
        ):
            raise ValueError
        names, total = [], 0
        for file in value["files"]:
            if not isinstance(file, dict) or set(file) != {"name", "size", "sha256"}:
                raise ValueError
            name, size, digest = file["name"], file["size"], file["sha256"]
            if (
                not isinstance(name, str)
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or not SHA256.fullmatch(digest)
            ):
                raise ValueError
            if name not in BASE_FILES:
                match = CHUNK_NAME.fullmatch(name)
                valid_chunk = match and int(match[2]) < 32_768 and size <= CHUNK_BYTES
                cloud_limit = CLOUD_WHEEL_BYTES if "/artifact/" in name else CLOUD_BYTES
                if not valid_chunk and not (CLOUD_NAME.fullmatch(name) and size <= cloud_limit):
                    raise ValueError
            if name == "encryption.key" and size != 32 or name == "setup-token" and size != 43:
                raise ValueError
            names.append(name)
            total += size
        if total > maximum or names != sorted(set(names)) or not set(names) >= BASE_FILES:
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise BackupError("invalid, noncanonical, unsupported, or oversized backup manifest") from exc


def _unpack(archive: Path, key: bytes, expected: str, stage: Path, maximum: int, *, schema_version: int | None = None) -> dict:
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise BackupError("expected backup SHA-256 must be 64 lowercase hexadecimal characters")
    with _input(archive, _archive_limit(maximum)) as (stream, size):
        digest, count = hashlib.sha256(), 0
        while body := stream.read(FRAME):
            count += len(body)
            if count > size:
                raise BackupError("backup archive changed while reading")
            digest.update(body)
        if count != size or digest.hexdigest() != expected:
            raise BackupError("backup archive SHA-256 does not match the expected identity")
        stream.seek(0)
        authenticated_stream = _HashedStream(stream, size)
        encrypted = _Decryptor(authenticated_stream, key)
        length = struct.unpack(">I", _exact(encrypted, 4))[0]
        if not 0 < length <= MANIFEST_LIMIT:
            raise BackupError("backup manifest exceeds its byte allowance")
        manifest = _manifest(_exact(encrypted, length), maximum, schema_version=schema_version)
        for file in manifest["files"]:
            path = stage / file["name"]
            if file["name"] not in BASE_FILES:
                # mkdir(parents=True) does not apply mode to intermediate directories.
                missing = []
                parent = path.parent
                while parent != stage and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir(mode=0o700)
            digest, remaining = hashlib.sha256(), file["size"]
            with path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                while remaining:
                    body = _exact(encrypted, min(FRAME, remaining))
                    output.write(body)
                    digest.update(body)
                    remaining -= len(body)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != file["sha256"]:
                raise BackupError("backup payload file SHA-256 does not match its manifest")
        if encrypted.read(1):
            raise BackupError("backup contains unlisted plaintext data")
        if authenticated_stream.count != size or authenticated_stream.digest.hexdigest() != expected:
            raise BackupError("backup archive changed during decryption")
    chunks = _validate_state(stage, maximum, schema_version=schema_version)
    if {file["name"] for file in manifest["files"]} != BASE_FILES | chunks.keys() | _cloud_files(stage).keys():
        raise BackupError("backup file manifest differs from database artifact references")
    return manifest


class _HashedStream:
    def __init__(self, stream, maximum):
        self.stream, self.maximum = stream, maximum
        self.digest, self.count = hashlib.sha256(), 0

    def read(self, size):
        body = self.stream.read(size)
        self.count += len(body)
        if self.count > self.maximum:
            raise BackupError("backup archive grew during decryption")
        self.digest.update(body)
        return body


def _summary(manifest: dict) -> dict:
    return {
        "format": manifest["format"],
        "schema": manifest["schema"],
        "created_at": manifest["created_at"],
        "files": len(manifest["files"]),
        "payload_bytes": sum(file["size"] for file in manifest["files"]),
        "verified": True,
    }


def inspect_backup(archive: Path, key_file: Path, expected: str, *, maximum: int = DEFAULT_BYTES) -> dict:
    _allowance(maximum)
    try:
        # Private plaintext is temporary, next to the protected archive, and removed on every exit.
        protected_parent(archive.absolute())
        with tempfile.TemporaryDirectory(prefix=".reasoning-backup-", dir=archive.absolute().parent) as temporary:
            manifest = _unpack(archive.absolute(), _key(key_file), expected, Path(temporary), maximum)
        return {"sha256": expected, **_summary(manifest)}
    except (OSError, InstallError, sqlite3.Error) as exc:
        raise BackupError("backup verification failed; check protected paths, space, and database integrity") from exc


def _fence_restored_state(root: Path) -> dict:
    now, counts = int(time.time()), {}
    with _connect(root / "server.sqlite3", writable=True) as db:
        counts["sessions_revoked"] = db.execute("DELETE FROM sessions").rowcount
        counts["invitations_revoked"] = db.execute(
            "UPDATE invitations SET revoked_at=? WHERE accepted_at IS NULL AND revoked_at IS NULL", (now,)
        ).rowcount
        db.execute(
            "UPDATE invitation_mail SET ciphertext=NULL,state=CASE WHEN state='queued' THEN 'cancelled' "
            "WHEN state='sending' THEN 'unknown' ELSE state END,error_code=CASE WHEN state IN ('queued','sending') "
            "THEN 'backup_restored' ELSE error_code END"
        )
        counts["workers_revoked"] = db.execute(
            "UPDATE compute_workers SET revoked_at=?,draining=1 WHERE revoked_at IS NULL", (now,)
        ).rowcount
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_verifications'").fetchone():
            db.execute("UPDATE email_verifications SET ciphertext=NULL,expires_at=?,state=CASE WHEN state='sending' "
                       "THEN 'unknown' WHEN state='queued' THEN 'cancelled' ELSE state END", (now,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_mail'").fetchone():
            db.execute("UPDATE account_mail SET ciphertext=NULL,expires_at=?,consumed_at=?,state=CASE WHEN state='sending' "
                       "THEN 'unknown' WHEN state='queued' THEN 'cancelled' ELSE state END", (now, now))
        counts["work_workers_revoked"] = db.execute("UPDATE work_workers SET revoked_at=? WHERE revoked_at IS NULL", (now,)).rowcount
        db.execute("UPDATE work_effects SET state='unknown' WHERE state='intent'")
        db.execute("UPDATE work_provider_calls SET state='failed',error_code='backup_restored',finished_at=? WHERE state='running'", (now,))
        db.execute(
            "UPDATE work_jobs SET status=CASE WHEN status='leased' THEN 'outcome_unknown' ELSE 'cancelled' END, "
            "cancel_requested=1,lease_hash=NULL WHERE status IN ('queued','leased')"
        )
        db.execute(
            "UPDATE work_tasks SET status='outcome_unknown' WHERE id IN (SELECT task_id FROM work_jobs WHERE status='outcome_unknown')"
        )
        db.execute(
            "UPDATE work_tasks SET status='cancelled' WHERE id IN "
            "(SELECT task_id FROM work_jobs WHERE status='cancelled') AND status IN ('ready','running')"
        )
        db.execute("UPDATE work_programs SET status='paused' WHERE status='active'")
        db.execute("UPDATE topic_agent_threads SET status='paused' WHERE status='active'")
        counts["bots_revoked"] = db.execute("UPDATE bots SET revoked_at=? WHERE revoked_at IS NULL", (now,)).rowcount
        db.execute(
            "UPDATE bot_runs SET status='outcome_unknown',error_code='runner_lost',lease_hash=NULL,updated_at=? "
            "WHERE status IN ('queued','running','waiting_approval')",
            (now,),
        )
        db.execute("UPDATE bot_runtimes SET last_seen_at=0")
        db.execute("UPDATE bot_execution_approvals SET status='denied',decided_at=? WHERE status IN ('pending','approved')", (now,))
        db.execute("UPDATE bot_compute_approvals SET status='denied' WHERE status='pending'")
        counts["webhooks_revoked"] = db.execute(
            "UPDATE bot_webhooks SET disabled_at=?,ciphertext=NULL WHERE disabled_at IS NULL", (now,)
        ).rowcount
        db.execute(
            "UPDATE bot_webhook_deliveries SET state=CASE WHEN attempts>0 THEN 'unknown' ELSE 'cancelled' END,"
            "payload_json=NULL,finished_at=?,error_code='backup_restored' WHERE state IN ('queued','sending')",
            (now,),
        )
        counts["scalers_revoked"] = db.execute("UPDATE compute_scalers SET revoked_at=? WHERE revoked_at IS NULL", (now,)).rowcount
        workflows = db.execute("SELECT id,organization_id FROM workflow_runs WHERE finished_at IS NULL").fetchall()
        db.execute("UPDATE workflow_runs SET restore_fenced=1")
        if workflows:
            counts["workflow_runs_held"] = len(workflows)
            db.execute("UPDATE workflow_runs SET status='outcome_unknown',cancel_requested=1,revision=revision+1 WHERE finished_at IS NULL")
            for run_id, org in workflows:
                db.execute(
                    "INSERT INTO workflow_run_events(organization_id,run_id,event_json,created_at) VALUES(?,?,?,?)",
                    (org, run_id, canonical({"type": "backup_restored", "status": "outcome_unknown"}).decode(), now),
                )
        db.execute("DELETE FROM compute_scaler_requests")
        db.execute("DELETE FROM bot_requests")
        db.execute("DELETE FROM bot_pairings")
        db.execute("DELETE FROM bot_webhook_requests")
        runs = db.execute("SELECT id,organization_id FROM llm_runs WHERE status IN ('queued','running')").fetchall()
        counts["inference_runs_held"] = len(runs)
        db.execute(
            "UPDATE llm_runs SET status='interrupted',cancel_requested=1,error_code='backup_restored',finished_at=?,"
            "accounting_state='uncertain',settled_at=NULL WHERE status IN ('queued','running')",
            (now,),
        )
        for run, org in runs:
            db.execute(
                "INSERT INTO llm_events(organization_id,run_id,event_json,created_at) VALUES(?,?,?,?)",
                (
                    org,
                    run,
                    canonical(
                        {"type": "state", "status": "interrupted", "error_code": "backup_restored", "accounting_state": "uncertain"}
                    ).decode(),
                    now,
                ),
            )
        jobs = db.execute("SELECT id,organization_id FROM compute_jobs WHERE status IN ('queued','running','outcome_unknown')").fetchall()
        counts["compute_jobs_held"] = len(jobs)
        db.execute(
            "UPDATE compute_jobs SET status='outcome_unknown',cancel_requested=1,error_code='backup_restored',lease_expires_at=NULL "
            "WHERE status IN ('queued','running','outcome_unknown')"
        )
        for job, org in jobs:
            db.execute(
                "INSERT INTO compute_events(organization_id,job_id,event_json,created_at) VALUES(?,?,?,?)",
                (org, job, canonical({"type": "state", "status": "outcome_unknown", "error_code": "backup_restored"}).decode(), now),
            )
        for (org,) in db.execute("SELECT id FROM organizations").fetchall():
            db.execute(
                "INSERT INTO audit_events(organization_id,actor_id,action,target_id,created_at) "
                "VALUES(?,NULL,'installation.restore','installation',?)",
                (org, now),
            )
    # Never reactivate a setup credential from the old host.
    with (root / "setup-token").open("wb") as stream:
        stream.write(token().encode())
        stream.flush()
        os.fsync(stream.fileno())
    return counts


def _rename_noreplace(source: Path, target: Path) -> None:
    try:
        atomic_rename(source, target)
    except FileExistsError as exc:
        raise BackupError("restore destination already exists; refusing to overwrite installation state") from exc
    except OSError as exc:
        raise BackupError("atomic restore publication is unavailable") from exc


def restore(
    archive: Path,
    key_file: Path,
    expected: str,
    destination: Path,
    *,
    source_stopped: bool = False,
    maximum: int = DEFAULT_BYTES,
    _before_publish=None,
) -> dict:
    _allowance(maximum)
    if source_stopped is not True:
        raise BackupError("restore requires explicit confirmation that the original server and workers are stopped or fenced")
    destination = destination.absolute()
    try:
        protected_parent(destination)
        if destination.exists() or destination.is_symlink():
            raise BackupError("restore destination already exists; refusing to overwrite installation state")
        with tempfile.TemporaryDirectory(prefix=".reasoning-restore-", dir=destination.parent) as temporary:
            root = Path(temporary) / "state"
            root.mkdir(mode=0o700)
            manifest = _unpack(archive.absolute(), _key(key_file), expected, root, maximum)
            recovery = _fence_restored_state(root)
            _validate_state(root, maximum)
            if _before_publish is not None:
                _before_publish(root, manifest, recovery)
            for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
                _sync_directory(directory)
            with (root / "server.sqlite3").open("rb") as stream:
                os.fsync(stream.fileno())
            _sync_directory(root)
            _rename_noreplace(root, destination)
            _sync_directory(destination.parent)
        return {"data_dir": str(destination), "sha256": expected, "server_started": False, **_summary(manifest), **recovery}
    except (OSError, InstallError, sqlite3.Error) as exc:
        raise BackupError("restore failed; destination was not overwritten; check protected paths, space, and database state") from exc
