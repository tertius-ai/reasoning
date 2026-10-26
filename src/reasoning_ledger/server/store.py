"""Durable, tenant-scoped state for a public Reasoning installation."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag

from .migrations import MIGRATIONS
from .schema_operations import sqlite_migration
from .security import SecretBox, digest, password_hash, password_matches, private_file, token


class StoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _id() -> str:
    return secrets.token_hex(16)


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise StoreError(404, "not found")
    return dict(row)


class Store:
    session_lifetime = 7 * 24 * 3600

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.absolute()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.data_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("server data directory must be owner-owned, private, and not a symlink")
        self.database = self.data_dir / "server.sqlite3"
        self._postgres = None
        configuration = self.data_dir / "postgresql.json"
        if configuration.exists() or configuration.is_symlink():
            from .postgres import Database, PostgresConfig

            if self.database.exists():
                raise ValueError("PostgreSQL needs a separate data directory; existing SQLite data must be migrated explicitly")
            self._postgres = Database(PostgresConfig.read(configuration))
        from .maintenance import assert_initialization_allowed

        assert_initialization_allowed(self.data_dir)
        # Serialize initialization, including the key-before-database invariant.
        private_file(self.data_dir / "initialization.lock", b"")
        fd = os.open(self.data_dir / "initialization.lock", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Recheck after waiting: maintenance may have published its fence
            # between the earlier check and acquisition of this lock.
            assert_initialization_allowed(self.data_dir)
            key_path = self.data_dir / "encryption.key"
            if self.database.exists() and not key_path.exists():
                raise ValueError("existing server database requires its original encryption.key")
            key = private_file(key_path, secrets.token_bytes(32))
            self._box = SecretBox(key)
            self._host_setup_key = hmac.digest(key, b"reasoning-host-owner-setup-v1", "sha256")
            self._mail_binding_key = hmac.digest(key, b"reasoning-mail-configuration-v1", "sha256")
            self._setup_token = private_file(self.data_dir / "setup-token", token().encode()).decode()
            if self._postgres:
                fingerprint = hmac.digest(key, b"reasoning-postgres-installation-v1", "sha256").hex()
                self._postgres.initialize(fingerprint, MIGRATIONS)
            else:
                private_file(self.database, b"")
            with self.transaction(write=True, _migration=self._postgres is None) as db:
                if self._postgres is None:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version > len(MIGRATIONS):
                        raise ValueError("server database schema is newer than this release")
                    for index in range(version, len(MIGRATIONS)):
                        for statement in MIGRATIONS[index]:
                            sqlite_migration(db, statement)
                        db.execute(f"PRAGMA user_version={index + 1}")
                for row in db.execute("SELECT id,organization_id,provider,ciphertext FROM provider_keys"):
                    try:
                        self._box.open(
                            row["ciphertext"], organization_id=row["organization_id"], provider=row["provider"], key_id=row["id"]
                        )
                    except (InvalidTag, ValueError) as exc:
                        raise ValueError("cannot decrypt stored provider credentials with this installation key") from exc
                for row in db.execute("SELECT invitation_id,organization_id,ciphertext FROM invitation_mail WHERE ciphertext IS NOT NULL"):
                    try:
                        self._box.open_mail(row["ciphertext"], organization_id=row["organization_id"], invitation_id=row["invitation_id"])
                    except (InvalidTag, ValueError) as exc:
                        raise ValueError("cannot decrypt stored invitation email with this installation key") from exc
                for row in db.execute("SELECT id,organization_id,ciphertext FROM bot_webhooks WHERE ciphertext IS NOT NULL"):
                    try:
                        self._box.open_webhook(row["ciphertext"], organization_id=row["organization_id"], webhook_id=row["id"])
                    except (InvalidTag, ValueError) as exc:
                        raise ValueError("cannot decrypt stored webhook secrets with this installation key") from exc
        # All login attempts perform the same password derivation, even for absent users.
        self._dummy_password = password_hash(token())

    @contextmanager
    def transaction(self, *, write: bool = False, scope: str | None | bool = None, _migration: bool = False) -> Iterator[Any]:
        if self._postgres:
            with self._postgres.transaction(write=write, scope=scope) as db:
                yield db
            return
        db = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            # SQLite's table-rebuild migration needs FK enforcement disabled
            # before BEGIN, followed by an explicit full check before commit.
            # Ordinary transactions always enforce foreign keys immediately.
            db.execute("PRAGMA foreign_keys=OFF" if _migration else "PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if _migration and db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("server schema migration has inconsistent foreign-key references")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _require(db: sqlite3.Connection, user_id: str, organization_id: str, *, write: bool = False, admin: bool = False) -> str:
        member = db.execute(
            "SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id "
            "WHERE m.organization_id=? AND m.user_id=? AND u.account_kind='human'",
            (organization_id, user_id),
        ).fetchone()
        if member is None:
            raise StoreError(404, "not found")
        role = member["role"]
        if (admin and role not in {"owner", "admin"}) or (write and role == "viewer"):
            raise StoreError(403, "insufficient organization permissions")
        return role

    @staticmethod
    def _project_access(
        db: sqlite3.Connection, user_id: str, organization_id: str, project_id: str, *, write: bool = False, manage: bool = False
    ) -> tuple[str, dict[str, Any], str | None]:
        role = Store._require(db, user_id, organization_id)
        project = _row(db.execute(
            "SELECT * FROM projects WHERE organization_id=? AND id=? AND deleted_at IS NULL", (organization_id, project_id)
        ).fetchone())
        membership = db.execute(
            "SELECT role FROM project_memberships WHERE organization_id=? AND project_id=? AND user_id=?",
            (organization_id, project_id, user_id),
        ).fetchone()
        project_role = membership["role"] if membership else None
        administrator = role in {"owner", "admin"}
        if project["visibility"] == "private" and not administrator and project_role is None:
            raise StoreError(404, "not found")
        if manage and not administrator and project_role != "owner":
            raise StoreError(403, "project owner permissions required")
        if write and not administrator and role == "viewer" and project_role != "owner":
            raise StoreError(403, "insufficient project permissions")
        return role, project, project_role

    @staticmethod
    def _channel_access(db, user_id: str, organization_id: str, channel_id: str, *, write: bool = False, manage: bool = False):
        channel = _row(db.execute(
            "SELECT * FROM channels WHERE organization_id=? AND id=? AND deleted_at IS NULL", (organization_id, channel_id)
        ).fetchone())
        role, project, project_role = Store._project_access(
            db, user_id, organization_id, channel["project_id"], write=write, manage=manage
        )
        return role, project, project_role, channel

    @staticmethod
    def _topic_access(db, user_id: str, organization_id: str, topic_id: str, *, write: bool = False):
        topic = _row(db.execute(
            "SELECT * FROM topics WHERE organization_id=? AND id=? AND deleted_at IS NULL", (organization_id, topic_id)
        ).fetchone())
        role, project, project_role, channel = Store._channel_access(
            db, user_id, organization_id, topic["channel_id"], write=write
        )
        return role, project, project_role, channel, topic

    @staticmethod
    def _audit(db: sqlite3.Connection, organization_id: str, actor_id: str | None, action: str, target_id: str) -> None:
        db.execute(
            "INSERT INTO audit_events(organization_id,actor_id,action,target_id,created_at) VALUES(?,?,?,?,?)",
            (organization_id, actor_id, action, target_id, int(time.time())),
        )

    @staticmethod
    def _new_session(db: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        if db.execute("SELECT 1 FROM users WHERE id=? AND account_kind='human'", (user_id,)).fetchone() is None:
            raise StoreError(401, "human account required")
        access, csrf = token(), token()
        now = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (digest(access), user_id, digest(csrf), now, now + Store.session_lifetime))
        return {"access_token": access, "csrf_token": csrf, "expires_at": now + Store.session_lifetime, "user_id": user_id}

    def initialized(self) -> bool:
        with self.transaction() as db:
            return bool(db.execute("SELECT initialized FROM installation WHERE singleton=1").fetchone()[0])

    def bootstrap(self, setup_token: str, email: str, name: str, password: str, organization_name: str) -> dict[str, Any]:
        if not hmac.compare_digest(digest(setup_token), digest(self._setup_token)):
            raise StoreError(403, "invalid setup credential")
        encoded = password_hash(password)
        with self.transaction(write=True) as db:
            ids = self._initial_owner(db, email, name, encoded, organization_name)
            return {**self._new_session(db, ids["user_id"]), "organization_id": ids["organization_id"]}

    def _initial_owner(self, db, email, name, encoded, organization_name):
        if db.execute("SELECT initialized FROM installation WHERE singleton=1").fetchone()[0]:
            raise StoreError(409, "installation is already initialized")
        user_id, organization_id = _id(), _id()
        now = int(time.time())
        db.execute(
            "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)", (user_id, email.lower(), name, encoded, now)
        )
        db.execute("INSERT INTO organizations(id,name,created_at) VALUES(?,?,?)", (organization_id, organization_name, now))
        db.execute("INSERT INTO memberships VALUES(?,?,?)", (organization_id, user_id, "owner"))
        db.execute("UPDATE installation SET initialized=1 WHERE singleton=1")
        self._audit(db, organization_id, user_id, "organization.create", organization_id)
        return {"user_id": user_id, "organization_id": organization_id}

    def bootstrap_host(self, setup_token, value):
        """Host-only exact replay; never issue a session or reset an account."""
        from .onboarding import canonical_owner, owner_binding, owner_input, owner_result

        if not hmac.compare_digest(digest(setup_token), digest(self._setup_token)):
            raise StoreError(403, "invalid setup credential")
        value = owner_input(value)
        binding = owner_binding(value)
        request_hash = hmac.digest(self._host_setup_key, canonical_owner(value), "sha256").hex()
        encoded = password_hash(value["password"])
        with self.transaction(write=True) as db:
            receipt = db.execute("SELECT * FROM host_setup_receipt WHERE singleton=1").fetchone()
            if receipt is None:
                ids = self._initial_owner(db, value["email"], value["name"], encoded, value["organization_name"])
                db.execute(
                    "INSERT INTO host_setup_receipt VALUES(1,?,?,?,?)",
                    (value["setup_id"], request_hash, ids["user_id"], ids["organization_id"]),
                )
            else:
                if receipt["setup_id"] != value["setup_id"] or not hmac.compare_digest(receipt["request_hash"], request_hash):
                    raise StoreError(409, "initial-owner setup requires its original exact private input")
                ids = {key: receipt[key] for key in ("user_id", "organization_id")}
                if (
                    not db.execute("SELECT initialized FROM installation WHERE singleton=1").fetchone()[0]
                    or self._require(db, ids["user_id"], ids["organization_id"]) != "owner"
                ):
                    raise StoreError(409, "initial-owner receipt no longer identifies the installation owner")
            return owner_result({"format": 1, **binding, **ids, "initialized": True}, binding)

    def _login_attempt(self, email: str, peer: str) -> None:
        now = int(time.time())
        scopes = (("email:" + digest(email.lower()), 10), ("peer:" + digest(peer), 60))
        with self.transaction(write=True) as db:
            db.execute("DELETE FROM auth_attempts WHERE attempted_at<=?", (now - 900,))
            for scope, limit in scopes:
                count = db.execute("SELECT count(*) FROM auth_attempts WHERE scope=?", (scope,)).fetchone()[0]
                if count >= limit:
                    raise StoreError(429, "too many sign-in attempts; try again later")
            db.executemany("INSERT INTO auth_attempts VALUES(?,?)", ((scope, now) for scope, _ in scopes))

    def login(self, email: str, password: str, peer: str) -> dict[str, Any]:
        self._login_attempt(email, peer)
        with self.transaction() as db:
            user = db.execute("SELECT id,password_hash FROM users WHERE email=? AND account_kind='human'", (email.lower(),)).fetchone()
        encoded = user["password_hash"] if user else self._dummy_password
        valid = password_matches(password, encoded)
        if user is None or not valid:
            raise StoreError(401, "invalid email or password")
        with self.transaction(write=True) as db:
            # Refuse a password changed while the derivation was running.
            current = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            if current is None or not hmac.compare_digest(current[0], encoded):
                raise StoreError(401, "invalid email or password")
            return self._new_session(db, user["id"])

    def session(self, access: str, csrf: str | None = None) -> dict[str, Any]:
        with self.transaction() as db:
            session = db.execute(
                "SELECT s.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.account_kind='human'",
                (digest(access), int(time.time())),
            ).fetchone()
            if session is None:
                raise StoreError(401, "sign-in required")
            if csrf is not None and not hmac.compare_digest(session["csrf_hash"], digest(csrf)):
                raise StoreError(403, "invalid CSRF credential")
            return {"user_id": session["user_id"], "expires_at": session["expires_at"]}

    def logout(self, access: str) -> None:
        with self.transaction(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(access),))

    def reset_password(self, email: str, password: str) -> None:
        """Host-administrator recovery; never exposed over HTTP."""
        from .account_recovery import invalidate_recovery

        encoded = password_hash(password)
        with self.transaction(write=True) as db:
            user = _row(db.execute("SELECT id FROM users WHERE email=? AND account_kind='human'", (email.lower(),)).fetchone())
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            invalidate_recovery(db, user["id"], int(time.time()))
            for org in db.execute("SELECT organization_id FROM memberships WHERE user_id=?", (user["id"],)).fetchall():
                self._audit(db, org[0], user["id"], "account.host-password-reset", user["id"])

    def me(self, user_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            return _row(db.execute("SELECT id,email,name,created_at,email_verified_at FROM users WHERE id=?", (user_id,)).fetchone())

    def organizations(self, user_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.id,o.name,o.created_at,m.role FROM organizations o JOIN memberships m ON m.organization_id=o.id "
                    "WHERE m.user_id=? ORDER BY o.created_at,o.id",
                    (user_id,),
                )
            ]

    def create_organization(
        self,
        user_id: str,
        name: str,
        *,
        credential_mode: str = "none",
        key_label: str | None = None,
        key: str | None = None,
        source_organization_id: str | None = None,
        source_key_id: str | None = None,
        allowed_models: list[str] | None = None,
        monthly_budget_microusd: int = 0,
    ) -> dict[str, Any]:
        allowed_models = allowed_models or []
        if credential_mode not in {"new", "reuse", "none"}:
            raise StoreError(400, "invalid organization credential choice")
        if credential_mode == "none" and any(
            value for value in (key_label, key, source_organization_id, source_key_id, allowed_models, monthly_budget_microusd)
        ):
            raise StoreError(400, "no-AI organization setup cannot include credentials or policy")
        if credential_mode == "reuse" and any(value for value in (key_label, key, allowed_models, monthly_budget_microusd)):
            raise StoreError(400, "reused organization setup cannot include fresh credentials or policy")
        with self.transaction(write=True) as db:
            administrator = db.execute(
                "SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.user_id=? AND m.role='owner' AND u.account_kind='human' LIMIT 1",
                (user_id,),
            ).fetchone()
            if administrator is None:
                raise StoreError(403, "an existing organization owner must create organizations")
            source = None
            if credential_mode == "reuse":
                if source_organization_id is None or source_key_id is None:
                    raise StoreError(400, "existing organization key selection is incomplete")
                self._require(db, user_id, source_organization_id, admin=True)
                source = _row(
                    db.execute(
                        "SELECT * FROM provider_keys WHERE organization_id=? AND id=? AND disabled_at IS NULL",
                        (source_organization_id, source_key_id),
                    ).fetchone()
                )
            elif credential_mode == "new" and (
                key_label is None or key is None or monthly_budget_microusd <= 0
            ):
                raise StoreError(400, "fresh organization credentials require a key and positive budget")
            organization_id, now = _id(), int(time.time())
            if source is None:
                policy, budget = json.dumps(sorted(set(allowed_models))), monthly_budget_microusd
            else:
                policy_row = _row(
                    db.execute(
                        "SELECT model_policy_json,monthly_budget_microusd FROM organizations WHERE id=?",
                        (source_organization_id,),
                    ).fetchone()
                )
                policy, budget = policy_row["model_policy_json"], policy_row["monthly_budget_microusd"]
            db.execute(
                "INSERT INTO organizations(id,name,created_at,model_policy_json,monthly_budget_microusd) VALUES(?,?,?,?,?)",
                (organization_id, name, now, policy, budget),
            )
            db.execute("INSERT INTO memberships VALUES(?,?,?)", (organization_id, user_id, "owner"))
            self._audit(db, organization_id, user_id, "organization.create", organization_id)
            if credential_mode in {"new", "reuse"}:
                key_id = _id()
                if source is None:
                    value, label, provider = key, key_label, "openrouter"
                else:
                    value = self._box.open(
                        source["ciphertext"],
                        organization_id=source["organization_id"],
                        provider=source["provider"],
                        key_id=source["id"],
                    )
                    label, provider = source["label"], source["provider"]
                ciphertext = self._box.seal(value, organization_id=organization_id, provider=provider, key_id=key_id)
                db.execute(
                    "INSERT INTO provider_keys VALUES(?,?,?,?,?,?,?,NULL)",
                    (key_id, organization_id, provider, label, ciphertext, digest(value)[:16], now),
                )
                self._audit(db, organization_id, user_id, "provider-key.create", key_id)
                if source is not None:
                    self._audit(db, source_organization_id, user_id, "provider-key.reuse", source_key_id)
                self._audit(db, organization_id, user_id, "organization.policy", organization_id)
            return {"id": organization_id, "name": name, "created_at": now, "role": "owner"}

    def members(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            self._require(db, user_id, organization_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT u.id,u.name,u.email,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.organization_id=?",
                    (organization_id,),
                )
            ]

    def presence(self, user_id: str, organization_id: str) -> dict[str, list[dict[str, Any]]]:
        """Mark this browser active and return organization presence."""
        now = int(time.time())
        with self.transaction(write=True) as db:
            self._require(db, user_id, organization_id)
            db.execute(
                "INSERT INTO user_presence(organization_id,user_id,last_seen_at) VALUES(?,?,?) "
                "ON CONFLICT(organization_id,user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (organization_id, user_id, now),
            )
            humans = [
                {
                    **dict(row),
                    "online": row["last_seen_at"] is not None and row["last_seen_at"] >= now - 45,
                }
                for row in db.execute(
                    "SELECT u.id,u.name,u.email,m.role,p.last_seen_at FROM memberships m JOIN users u ON u.id=m.user_id "
                    "LEFT JOIN user_presence p ON p.organization_id=m.organization_id AND p.user_id=m.user_id "
                    "WHERE m.organization_id=? AND u.account_kind='human' ORDER BY u.name COLLATE NOCASE,u.id",
                    (organization_id,),
                )
            ]
            from .bot_store import BotStore

            bots = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "owner_user_id": row["created_by"],
                    "last_seen_at": row["last_used_at"],
                    "online": row["last_used_at"] is not None and row["last_used_at"] >= now - 45,
                }
                for row in db.execute(
                    "SELECT b.*,u.name FROM bots b JOIN users u ON u.id=b.id "
                    "WHERE b.organization_id=? AND b.revoked_at IS NULL AND b.expires_at>? "
                    "ORDER BY u.name COLLATE NOCASE,b.id",
                    (organization_id, now),
                )
                if BotStore._public(db, row)["enabled"]
            ]
            for bot in bots:
                bot["developer"] = BotStore._developer(db, bot["id"])["enabled"]
                runtime = BotStore._runtime(db, bot["id"])
                bot["engine"] = runtime["engine"] if runtime else None
                bot["models"] = runtime["models"] if runtime else []
                if bot["owner_user_id"] == user_id and runtime:
                    # What the composer offers when you message your own bot: its effort levels per model and local tools.
                    capabilities = db.execute("SELECT capabilities_json FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone()[0]
                    bot["efforts"] = json.loads(capabilities or "{}").get("efforts", {})
                    bot["local_tools"] = runtime["local_tools"]
                activity = db.execute("SELECT topic_id,direct_conversation_id,requested_by,phase,status,activity_json FROM bot_runs "
                    "WHERE bot_id=? "
                    "AND status IN ('running','waiting_approval') AND updated_at>? ORDER BY updated_at DESC LIMIT 1",
                    (bot["id"], now - 120)).fetchone()
                bot["activity"] = None
                if activity:
                    try:
                        if activity["direct_conversation_id"]:
                            # Work in a direct conversation shows only to the owner who asked for it.
                            if activity["requested_by"] != user_id:
                                raise StoreError(404, "not found")
                        else:
                            self._topic_access(db, user_id, organization_id, activity["topic_id"])
                        bot["activity"] = activity["status"] if activity["status"] == "waiting_approval" else activity["phase"]
                        entries = json.loads(activity["activity_json"])
                        if entries and activity["status"] != "waiting_approval":
                            bot["activity"] = entries[-1]["label"]
                    except StoreError:
                        pass
            return {"humans": humans, "bots": bots}

    def change_member(self, actor_id: str, organization_id: str, user_id: str, role: str | None) -> None:
        from .webhook_store import cancel_ineligible

        if role not in {None, "owner", "admin", "member", "viewer"}:
            raise StoreError(400, "invalid role")
        with self.transaction(write=True) as db:
            actor_role = self._require(db, actor_id, organization_id, admin=True)
            target = _row(
                db.execute("SELECT role FROM memberships WHERE organization_id=? AND user_id=?", (organization_id, user_id)).fetchone()
            )
            if actor_role != "owner" and (target["role"] in {"owner", "admin"} or role in {"owner", "admin"}):
                raise StoreError(403, "only owners can manage administrators and owners")
            if target["role"] == "owner" and role != "owner":
                owners = db.execute(
                    "SELECT count(*) FROM memberships WHERE organization_id=? AND role='owner'", (organization_id,)
                ).fetchone()[0]
                if owners <= 1:
                    raise StoreError(409, "organization must retain an owner")
            if role is None:
                sole_project_owner = db.execute(
                    """SELECT pm.project_id FROM project_memberships pm WHERE pm.organization_id=? AND pm.user_id=?
                    AND pm.role='owner' AND NOT EXISTS (SELECT 1 FROM project_memberships other
                        WHERE other.organization_id=pm.organization_id AND other.project_id=pm.project_id
                        AND other.role='owner' AND other.user_id<>pm.user_id) LIMIT 1""",
                    (organization_id, user_id),
                ).fetchone()
                if sole_project_owner is not None:
                    raise StoreError(409, "private projects must retain an owner")
                db.execute("DELETE FROM memberships WHERE organization_id=? AND user_id=?", (organization_id, user_id))
            else:
                db.execute("UPDATE memberships SET role=? WHERE organization_id=? AND user_id=?", (role, organization_id, user_id))
            cancel_ineligible(db, organization_id)
            self._audit(db, organization_id, actor_id, "membership.remove" if role is None else "membership.role", user_id)

    def invite(self, actor_id: str, organization_id: str, email: str, role: str, lifetime: int) -> dict[str, Any]:
        with self.transaction(write=True) as db:
            return self._invite(db, actor_id, organization_id, email, role, lifetime)

    def _invite(self, db, actor_id: str, organization_id: str, email: str, role: str, lifetime: int) -> dict[str, Any]:
        if role not in {"admin", "member", "viewer"} or not 60 <= lifetime <= 7 * 24 * 3600:
            raise StoreError(400, "invalid invitation role or lifetime")
        actor_role = self._require(db, actor_id, organization_id, admin=True)
        if role == "admin" and actor_role != "owner":
            raise StoreError(403, "only owners can invite administrators")
        invitation_id, credential, now = _id(), token(), int(time.time())
        db.execute(
            "INSERT INTO invitations(id,organization_id,email,role,token_hash,expires_at,created_by) VALUES(?,?,?,?,?,?,?)",
            (invitation_id, organization_id, email.lower(), role, digest(credential), now + lifetime, actor_id),
        )
        self._audit(db, organization_id, actor_id, "invitation.create", invitation_id)
        return {"id": invitation_id, "token": credential, "email": email.lower(), "role": role, "expires_at": now + lifetime}

    def invitations(self, actor_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            self._require(db, actor_id, organization_id, admin=True)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT i.id,i.email,i.role,i.expires_at,i.accepted_at,i.revoked_at,m.state AS email_state,"
                    "m.error_code AS email_error FROM invitations i LEFT JOIN invitation_mail m ON m.invitation_id=i.id "
                    "WHERE i.organization_id=?",
                    (organization_id,),
                )
            ]

    def revoke_invitation(self, actor_id: str, organization_id: str, invitation_id: str) -> None:
        with self.transaction(write=True) as db:
            self._require(db, actor_id, organization_id, admin=True)
            _row(db.execute("SELECT id FROM invitations WHERE id=? AND organization_id=?", (invitation_id, organization_id)).fetchone())
            db.execute("UPDATE invitations SET revoked_at=? WHERE id=?", (int(time.time()), invitation_id))
            self._retire_invitation_mail(db, invitation_id)
            self._audit(db, organization_id, actor_id, "invitation.revoke", invitation_id)

    @staticmethod
    def _retire_invitation_mail(db, invitation_id):
        db.execute(
            "UPDATE invitation_mail SET ciphertext=NULL,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END "
            "WHERE invitation_id=?",
            (invitation_id,),
        )

    @staticmethod
    def _invitation(db: sqlite3.Connection, credential: str) -> dict[str, Any]:
        invitation = db.execute(
            "SELECT * FROM invitations WHERE token_hash=? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?",
            (digest(credential), int(time.time())),
        ).fetchone()
        if invitation is None:
            raise StoreError(404, "invitation unavailable")
        return dict(invitation)

    def invitation_status(self, credential: str, *, peer: str | None = None) -> dict[str, Any]:
        """A link holder can inspect availability without consuming the invite."""
        with self.transaction(write=peer is not None) as db:
            if peer is not None:
                now = int(time.time())
                db.execute("DELETE FROM auth_attempts WHERE scope LIKE 'invitation:inspect:%' AND attempted_at<=?", (now - 900,))
                scopes = (("invitation:inspect:peer:" + digest(peer), 120), ("invitation:inspect:link:" + digest(credential), 20))
                for scope, maximum in scopes:
                    count = db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=? AND attempted_at>?",
                                       (scope, now - 900)).fetchone()[0]
                    if count >= maximum:
                        raise StoreError(429, "too many invitation checks; try again later")
                db.executemany("INSERT INTO auth_attempts VALUES(?,?)", ((scope, now) for scope, _ in scopes))
            row = db.execute("SELECT * FROM invitations WHERE token_hash=?", (digest(credential),)).fetchone()
            if row is None:
                return {"state": "invalid"}
            if row["accepted_at"] is not None:
                return {"state": "used"}
            if row["revoked_at"] is not None:
                return {"state": "revoked"}
            if row["expires_at"] <= int(time.time()):
                return {"state": "expired"}
            exists = db.execute("SELECT 1 FROM users WHERE email=? AND account_kind='human'", (row["email"],)).fetchone()
            return {"state": "pending", "email": row["email"], "existing_account": exists is not None}

    def accept_invitation(self, credential: str, *, user_id: str | None, name: str | None, password: str | None) -> dict[str, Any]:
        with self.transaction() as db:
            self._invitation(db, credential)
        encoded = password_hash(password) if user_id is None and password is not None else None
        with self.transaction(write=True) as db:
            invitation = self._invitation(db, credential)
            user = db.execute("SELECT id FROM users WHERE email=?", (invitation["email"],)).fetchone()
            if user:
                if db.execute("SELECT 1 FROM users WHERE id=? AND account_kind='human'", (user["id"],)).fetchone() is None:
                    raise StoreError(403, "human account required")
                if user_id != user["id"]:
                    raise StoreError(409, "sign in to the invited account first")
            elif user_id is not None:
                raise StoreError(403, "invitation belongs to a different email address")
            else:
                if not name or not encoded:
                    raise StoreError(400, "new accounts require a name and password")
                user_id = _id()
                db.execute(
                    "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
                    (user_id, invitation["email"], name, encoded, int(time.time())),
                )
            existing = db.execute(
                "SELECT role FROM memberships WHERE organization_id=? AND user_id=?", (invitation["organization_id"], user_id)
            ).fetchone()
            # An invitation must never overwrite an existing member's role.
            if not existing:
                db.execute("INSERT INTO memberships VALUES(?,?,?)", (invitation["organization_id"], user_id, invitation["role"]))
            db.execute("UPDATE invitations SET accepted_at=? WHERE id=?", (int(time.time()), invitation["id"]))
            self._retire_invitation_mail(db, invitation["id"])
            self._audit(db, invitation["organization_id"], user_id, "invitation.accept", invitation["id"])
            result = {"organization_id": invitation["organization_id"], "user_id": user_id}
            return result if user else {**result, **self._new_session(db, user_id)}

    @staticmethod
    def _visible_projects(db, user_id: str, organization_id: str, role: str) -> list[dict[str, Any]]:
        projects = [dict(row) for row in db.execute(
            """SELECT p.*,pm.role AS project_role FROM projects p
            LEFT JOIN project_memberships pm ON pm.organization_id=p.organization_id AND pm.project_id=p.id AND pm.user_id=?
            WHERE p.organization_id=? AND p.deleted_at IS NULL
                AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
            ORDER BY p.created_at,p.id""", (user_id, organization_id, role)
        )]
        for project in projects:
            project.pop("deleted_at")
            if project["project_role"] is None:
                project.pop("project_role")
        return projects

    def visible_projects(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        """The projects of `hierarchy`, without channels, topics, or per-topic unread state."""
        with self.transaction() as db:
            return self._visible_projects(db, user_id, organization_id, self._require(db, user_id, organization_id))

    def hierarchy(self, user_id: str, organization_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            role = self._require(db, user_id, organization_id)
            projects = self._visible_projects(db, user_id, organization_id, role)
            project_ids = {project["id"] for project in projects}
            channels = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM channels WHERE organization_id=? AND deleted_at IS NULL ORDER BY created_at,id", (organization_id,)
                )
                if row["project_id"] in project_ids
            ]
            channel_ids = {channel["id"] for channel in channels}
            topics = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM topics WHERE organization_id=? AND deleted_at IS NULL ORDER BY created_at,id", (organization_id,)
                )
                if row["channel_id"] in channel_ids
            ]
            result = {"projects": projects, "channels": channels, "topics": topics}
            for entity in channels + topics:
                entity.pop("deleted_at")
            for topic in result["topics"]:
                topic["knowledge_tools"] = json.loads(topic.pop("knowledge_policy_json"))
            result["topic_state"] = [
                dict(row)
                for row in db.execute(
                    """SELECT t.id AS topic_id, COALESCE(s.subscribed,1) AS subscribed,
                COALESCE(s.last_read_sequence,0) AS last_read_sequence,
                (SELECT COALESCE(MAX(m.sequence),0) FROM messages m WHERE m.organization_id=t.organization_id AND m.topic_id=t.id)
                    AS last_message_sequence,
                (SELECT COUNT(*) FROM messages m WHERE m.organization_id=t.organization_id AND m.topic_id=t.id
                    AND m.sequence>COALESCE(s.last_read_sequence,0)) AS unread_count,
                (SELECT COUNT(*) FROM message_mentions mm JOIN messages m ON m.id=mm.message_id
                    WHERE mm.organization_id=t.organization_id AND mm.user_id=? AND m.topic_id=t.id
                    AND m.sequence>COALESCE(s.last_read_sequence,0)) AS unread_mention_count
                FROM topics t LEFT JOIN topic_subscriptions s ON s.organization_id=t.organization_id AND s.topic_id=t.id AND s.user_id=?
                JOIN channels c ON c.organization_id=t.organization_id AND c.id=t.channel_id
                WHERE t.organization_id=? AND t.deleted_at IS NULL AND c.deleted_at IS NULL AND c.project_id IN (
                    SELECT p.id FROM projects p LEFT JOIN project_memberships pm
                    ON pm.organization_id=p.organization_id AND pm.project_id=p.id AND pm.user_id=?
                    WHERE p.organization_id=? AND p.deleted_at IS NULL
                        AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
                ) ORDER BY t.created_at,t.id""",
                    (user_id, user_id, organization_id, user_id, organization_id, role),
                )
            ]
            for state in result["topic_state"]:
                state["subscribed"] = bool(state["subscribed"])
            return result

    def topic_preference(
        self, user_id: str, organization_id: str, topic_id: str, *, subscribed: bool | None = None, sequence: int | None = None
    ) -> None:
        with self.transaction(write=True) as db:
            self._topic_access(db, user_id, organization_id, topic_id)
            if sequence is not None and sequence != 0:
                message = db.execute(
                    "SELECT id FROM messages WHERE organization_id=? AND topic_id=? AND sequence=?", (organization_id, topic_id, sequence)
                ).fetchone()
                if message is None:
                    raise StoreError(400, "read cursor must identify a message in this topic")
            db.execute(
                "INSERT OR IGNORE INTO topic_subscriptions(organization_id,user_id,topic_id) VALUES(?,?,?)",
                (organization_id, user_id, topic_id),
            )
            if subscribed is not None:
                db.execute(
                    "UPDATE topic_subscriptions SET subscribed=? WHERE organization_id=? AND user_id=? AND topic_id=?",
                    (int(subscribed), organization_id, user_id, topic_id),
                )
            if sequence is not None:
                db.execute(
                    "UPDATE topic_subscriptions SET last_read_sequence=MAX(last_read_sequence,?) "
                    "WHERE organization_id=? AND user_id=? AND topic_id=?",
                    (sequence, organization_id, user_id, topic_id),
                )

    def topic_mark_unread(self, user_id: str, organization_id: str, topic_id: str, message_id: str) -> dict[str, int]:
        """Move this reader's read point back to just before `message_id`; it never moves forward here."""
        with self.transaction(write=True) as db:
            self._topic_access(db, user_id, organization_id, topic_id)
            message = db.execute(
                "SELECT sequence FROM messages WHERE organization_id=? AND topic_id=? AND id=?", (organization_id, topic_id, message_id)
            ).fetchone()
            if message is None:
                raise StoreError(404, "message not found")
            db.execute(
                "INSERT OR IGNORE INTO topic_subscriptions(organization_id,user_id,topic_id) VALUES(?,?,?)",
                (organization_id, user_id, topic_id),
            )
            db.execute(
                "UPDATE topic_subscriptions SET last_read_sequence=MIN(last_read_sequence,?) "
                "WHERE organization_id=? AND user_id=? AND topic_id=?",
                (message["sequence"] - 1, organization_id, user_id, topic_id),
            )
            row = db.execute(
                "SELECT last_read_sequence FROM topic_subscriptions WHERE organization_id=? AND user_id=? AND topic_id=?",
                (organization_id, user_id, topic_id),
            ).fetchone()
            return {"last_read_sequence": row["last_read_sequence"]}

    def search(self, user_id: str, organization_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        if not query.strip() or not 1 <= limit <= 100:
            raise StoreError(400, "search requires text and a limit of 1–100")
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.transaction() as db:
            role = self._require(db, user_id, organization_id)
            return [
                self._message(row, db)
                for row in db.execute(
                    """SELECT m.*,u.name AS sender_name,t.name AS topic_name,t.kind AS topic_kind,
                    c.name AS channel_name,p.name AS project_name
                FROM messages m JOIN topics t ON t.organization_id=m.organization_id AND t.id=m.topic_id
                JOIN users u ON u.id=m.author_id
                JOIN channels c ON c.organization_id=t.organization_id AND c.id=t.channel_id
                JOIN projects p ON p.organization_id=c.organization_id AND p.id=c.project_id
                LEFT JOIN project_memberships pm ON pm.organization_id=p.organization_id AND pm.project_id=p.id AND pm.user_id=?
                WHERE m.organization_id=? AND m.content LIKE ? ESCAPE '\\'
                AND p.deleted_at IS NULL AND c.deleted_at IS NULL AND t.deleted_at IS NULL
                AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
                ORDER BY m.sequence DESC LIMIT ?""",
                    (user_id, organization_id, pattern, role, limit),
                )
            ]

    def create_entity(
        self,
        user_id: str,
        organization_id: str,
        kind: str,
        name: str,
        parent_id: str | None = None,
        topic_kind: str | None = None,
        default_model: str | None = None,
        knowledge_tools: list[str] | None = None,
        visibility: str = "organization",
        member_ids: list[str] | None = None,
        owner_id: str | None = None,
        agent_tools: list[str] | None = None,
        default_effort: str | None = None,
    ) -> dict[str, Any]:
        from .knowledge_store import normalize_tools

        if kind not in {"projects", "channels", "topics"}:
            raise StoreError(400, "invalid entity")
        if kind == "topics" and topic_kind not in {"alpha", "beta", "gamma"}:
            raise StoreError(400, "invalid topic kind")
        if kind == "topics" and topic_kind != "beta" and default_model is not None:
            raise StoreError(400, "only beta topics have a default model")
        if default_effort is not None:
            from .reasoning_effort import checked

            checked(default_effort)
            if kind != "topics" or topic_kind != "beta" or not default_model:
                raise StoreError(400, "reasoning effort requires a beta topic model")
        if agent_tools is not None:
            from .research_tools import DEFAULT_TOOLS
            if (kind != "topics" or topic_kind != "beta" or not isinstance(agent_tools, list)
                or any(tool not in DEFAULT_TOOLS for tool in agent_tools) or len(set(agent_tools)) != len(agent_tools)):
                raise StoreError(400, "select distinct beta research tools")
        if kind == "projects" and (visibility not in {"organization", "private"} or visibility == "private" and not owner_id):
            raise StoreError(400, "private projects require a project owner")
        with self.transaction(write=True) as db:
            role = self._require(db, user_id, organization_id, write=True)
            if kind == "projects" and visibility == "private" and role not in {"owner", "admin"}:
                raise StoreError(403, "only organization administrators can create private projects")
            result = {"id": _id(), "organization_id": organization_id, "name": name, "created_at": int(time.time())}
            if kind == "projects":
                result["visibility"] = visibility
            if kind != "projects":
                parent_table, parent_field = ("projects", "project_id") if kind == "channels" else ("channels", "channel_id")
                if kind == "channels":
                    self._project_access(db, user_id, organization_id, str(parent_id), write=True)
                else:
                    self._channel_access(db, user_id, organization_id, str(parent_id), write=True)
                result[parent_field] = parent_id
            if kind == "topics":
                result["kind"] = topic_kind
                if default_model and default_model.startswith("bot/"):
                    from .bot_runtime import BotRuntime
                    from .bot_store import BotStore

                    choices = BotRuntime(BotStore(self)).channel_models(user_id, organization_id, parent_id, db=db)
                    selected = next((item for item in choices if item["id"] == default_model), None)
                    if selected is None:
                        raise StoreError(403, "selected bot does not grant access to new topics in this channel")
                    if default_effort and default_effort not in selected["efforts"]:
                        raise StoreError(400, "this bot model does not advertise that reasoning effort")
                policy = json.loads(
                    db.execute("SELECT model_policy_json FROM organizations WHERE id=?", (organization_id,)).fetchone()[0]
                )
                result["default_model"] = (
                    default_model if default_model is not None else (policy[0] if topic_kind == "beta" and policy else None)
                )
                if default_effort is not None:
                    result["default_effort"] = default_effort
                result["knowledge_policy_json"] = json.dumps(normalize_tools(topic_kind, knowledge_tools))
            fields = ",".join(result)
            placeholders = ",".join("?" for _ in result)
            try:
                db.execute(f"INSERT INTO {kind}({fields}) VALUES({placeholders})", tuple(result.values()))
            except sqlite3.IntegrityError as exc:
                raise StoreError(409, "entity conflicts with existing organization state") from exc
            if kind == "topics" and topic_kind == "beta" and agent_tools is not None:
                db.execute("INSERT INTO research_topic_settings(organization_id,topic_id,tools_json) VALUES(?,?,?)",
                           (organization_id, result["id"], json.dumps(sorted(agent_tools))))
            self._audit(db, organization_id, user_id, f"{kind}.create", result["id"])
            if kind == "projects" and visibility == "private":
                selected = set(member_ids or []) | {str(owner_id)}
                existing = {row[0] for row in db.execute(
                    "SELECT user_id FROM memberships WHERE organization_id=? AND user_id IN ({})".format(",".join("?" for _ in selected)),
                    (organization_id, *sorted(selected)),
                )}
                if existing != selected:
                    raise StoreError(400, "project members must belong to the organization")
                now = int(time.time())
                db.executemany(
                    "INSERT INTO project_memberships VALUES(?,?,?,?,?)",
                    (
                        (organization_id, result["id"], member, "owner" if member == owner_id else "member", now)
                        for member in sorted(selected)
                    ),
                )
                result["project_role"] = "owner" if owner_id == user_id else None
            if kind == "topics":
                result["knowledge_tools"] = json.loads(result.pop("knowledge_policy_json"))
                result.setdefault("default_effort", None)
            return result

    def messages(
        self, user_id: str, organization_id: str, topic_id: str, after: int = 0, limit: int = 100, before: int | None = None
    ) -> list[dict[str, Any]]:
        if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 200 or (before is not None and (after != 0 or not 1 <= before <= 2**63 - 1)):
            raise StoreError(400, "invalid pagination")
        with self.transaction() as db:
            self._topic_access(db, user_id, organization_id, topic_id)
            result = [
                self._message(row, db)
                for row in db.execute(
                    "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                    "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence"
                    + ("<? ORDER BY m.sequence DESC LIMIT ?" if before is not None else ">? ORDER BY m.sequence LIMIT ?"),
                    (organization_id, topic_id, before if before is not None else after, limit),
                )
            ]
            return list(reversed(result)) if before is not None else result

    def project_members(self, user_id: str, organization_id: str, project_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            _, project, _ = self._project_access(db, user_id, organization_id, project_id, manage=True)
            if project["visibility"] != "private":
                raise StoreError(400, "organization projects do not have a private roster")
            return [dict(row) for row in db.execute(
                """SELECT u.id,u.name,u.email,pm.role FROM project_memberships pm
                JOIN users u ON u.id=pm.user_id WHERE pm.organization_id=? AND pm.project_id=?
                ORDER BY CASE pm.role WHEN 'owner' THEN 0 ELSE 1 END,u.name COLLATE NOCASE,u.id""",
                (organization_id, project_id),
            )]

    def change_project_member(
        self, actor_id: str, organization_id: str, project_id: str, user_id: str, role: str | None
    ) -> None:
        if role not in {None, "owner", "member"}:
            raise StoreError(400, "invalid project role")
        with self.transaction(write=True) as db:
            _, project, _ = self._project_access(db, actor_id, organization_id, project_id, manage=True)
            if project["visibility"] != "private":
                raise StoreError(400, "organization projects do not have a private roster")
            if db.execute(
                "SELECT 1 FROM memberships WHERE organization_id=? AND user_id=?", (organization_id, user_id)
            ).fetchone() is None:
                raise StoreError(400, "project members must belong to the organization")
            existing = db.execute(
                "SELECT role FROM project_memberships WHERE organization_id=? AND project_id=? AND user_id=?",
                (organization_id, project_id, user_id),
            ).fetchone()
            if existing and existing["role"] == "owner" and role != "owner":
                owners = db.execute(
                    "SELECT count(*) FROM project_memberships WHERE organization_id=? AND project_id=? AND role='owner'",
                    (organization_id, project_id),
                ).fetchone()[0]
                if owners <= 1:
                    raise StoreError(409, "private project must retain an owner")
            if role is None:
                db.execute(
                    "DELETE FROM project_memberships WHERE organization_id=? AND project_id=? AND user_id=?",
                    (organization_id, project_id, user_id),
                )
            else:
                db.execute(
                    "INSERT INTO project_memberships VALUES(?,?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role",
                    (organization_id, project_id, user_id, role, int(time.time())),
                )
            action = "project-membership.remove" if role is None else "project-membership.role"
            self._audit(db, organization_id, actor_id, action, f"{project_id}:{user_id}")

    def rename_entity(self, user_id: str, organization_id: str, kind: str, entity_id: str, name: str) -> dict[str, Any]:
        if kind not in {"projects", "channels", "topics"}:
            raise StoreError(400, "invalid entity")
        with self.transaction(write=True) as db:
            if kind == "projects":
                self._project_access(db, user_id, organization_id, entity_id, manage=True)
            elif kind == "channels":
                self._channel_access(db, user_id, organization_id, entity_id, manage=True)
            else:
                channel = self._topic_access(db, user_id, organization_id, entity_id)[3]
                self._channel_access(db, user_id, organization_id, channel["id"], manage=True)
            try:
                db.execute(f"UPDATE {kind} SET name=? WHERE organization_id=? AND id=?", (name, organization_id, entity_id))
            except sqlite3.IntegrityError as exc:
                raise StoreError(409, "name conflicts with existing organization state") from exc
            self._audit(db, organization_id, user_id, f"{kind}.rename", entity_id)
            return _row(db.execute(f"SELECT * FROM {kind} WHERE organization_id=? AND id=?", (organization_id, entity_id)).fetchone())

    def _deletion_plan(self, db, user_id: str, organization_id: str, kind: str, entity_id: str) -> dict[str, Any]:
        if kind == "projects":
            self._require(db, user_id, organization_id, admin=True)
            entity = self._project_access(db, user_id, organization_id, entity_id)[1]
            channels = [row[0] for row in db.execute(
                "SELECT id FROM channels WHERE organization_id=? AND project_id=? AND deleted_at IS NULL ORDER BY id",
                (organization_id, entity_id),
            )]
            topics = [row[0] for row in db.execute(
                "SELECT t.id FROM topics t JOIN channels c ON c.organization_id=t.organization_id AND c.id=t.channel_id "
                "WHERE t.organization_id=? AND c.project_id=? AND t.deleted_at IS NULL AND c.deleted_at IS NULL ORDER BY t.id",
                (organization_id, entity_id),
            )]
        elif kind == "channels":
            entity = self._channel_access(db, user_id, organization_id, entity_id, manage=True)[3]
            channels = [entity_id]
            topics = [row[0] for row in db.execute(
                "SELECT id FROM topics WHERE organization_id=? AND channel_id=? AND deleted_at IS NULL ORDER BY id",
                (organization_id, entity_id),
            )]
        elif kind == "topics":
            _, _, _, channel, entity = self._topic_access(db, user_id, organization_id, entity_id)
            self._channel_access(db, user_id, organization_id, channel["id"], manage=True)
            channels, topics = [], [entity_id]
        else:
            raise StoreError(400, "invalid entity")
        placeholders = ",".join("?" for _ in topics)
        arguments = (organization_id, *topics)
        def count(table, predicate="1=1"):
            if not topics:
                return 0
            return db.execute(
                f"SELECT count(*) FROM {table} WHERE organization_id=? AND topic_id IN ({placeholders}) AND {predicate}",
                arguments,
            ).fetchone()[0]
        counts = {"projects": int(kind == "projects"), "channels": len(channels), "topics": len(topics),
                  "messages": count("messages"), "workflows": count("workflows"), "compute_jobs": count("compute_jobs"),
                  "knowledge_entries": count("knowledge_entries"), "model_runs": 0}
        if kind == "projects":
            counts["knowledge_entries"] = db.execute(
                "SELECT count(*) FROM knowledge_entries WHERE organization_id=? AND project_id=?",
                (organization_id, entity_id),
            ).fetchone()[0]
        from .navigation_lifecycle import blockers

        blocked = blockers(db, organization_id, topics)
        active = {kind: sum(item["kind"] == kind for item in blocked)
                  for kind in ("inference", "workflows", "compute", "agents", "bots", "reviews", "agent_threads")}
        if topics:
            counts["model_runs"] = db.execute(
                "SELECT count(*) FROM llm_runs r JOIN llm_requests q ON q.organization_id=r.organization_id AND q.id=r.request_id "
                f"WHERE q.organization_id=? AND q.topic_id IN ({placeholders})", arguments,
            ).fetchone()[0]
        contents = json.dumps([kind, entity_id, entity["name"], channels, topics, counts], sort_keys=True)
        return {"kind": kind, "id": entity_id, "name": entity["name"], "counts": counts, "active": active, "blockers": blocked,
                "can_delete": not any(active.values()), "plan_token": digest(contents), "topic_ids": topics, "channel_ids": channels}

    def deletion_plan(self, user_id: str, organization_id: str, kind: str, entity_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            result = self._deletion_plan(db, user_id, organization_id, kind, entity_id)
            return {key: value for key, value in result.items() if key not in {"topic_ids", "channel_ids"}}

    def delete_entity(
        self, user_id: str, organization_id: str, kind: str, entity_id: str, confirmation: str, plan_token: str,
        *, stop_active: bool = False
    ) -> None:
        from .webhook_store import cancel_ineligible

        with self.transaction(write=True) as db:
            plan = self._deletion_plan(db, user_id, organization_id, kind, entity_id)
            if confirmation != plan["name"]:
                raise StoreError(400, "type the exact name to confirm deletion")
            if not hmac.compare_digest(plan_token, plan["plan_token"]):
                raise StoreError(409, "contents changed; review the deletion counts again")
            if not plan["can_delete"] and not stop_active:
                raise StoreError(409, "stop active inference, workflows, compute, and agent goals before deletion")
            if stop_active:
                from .navigation_lifecycle import stop_subtree

                stop_subtree(self, db, organization_id, plan["topic_ids"])
            now = int(time.time())
            for topic in plan["topic_ids"]:
                db.execute("UPDATE topics SET deleted_at=? WHERE organization_id=? AND id=?", (now, organization_id, topic))
                db.execute("DELETE FROM bot_topics WHERE organization_id=? AND topic_id=?", (organization_id, topic))
                db.execute("DELETE FROM topic_subscriptions WHERE organization_id=? AND topic_id=?", (organization_id, topic))
            for channel in plan["channel_ids"]:
                # Deleted channels reserve no user-facing name, so the same name can be created again.
                db.execute("UPDATE channels SET deleted_at=?,name=? WHERE organization_id=? AND id=?",
                           (now, "deleted-" + channel, organization_id, channel))
            if kind == "projects":
                db.execute("UPDATE projects SET deleted_at=? WHERE organization_id=? AND id=?", (now, organization_id, entity_id))
                db.execute("DELETE FROM project_memberships WHERE organization_id=? AND project_id=?", (organization_id, entity_id))
            cancel_ineligible(db, organization_id)
            db.execute("INSERT INTO hierarchy_deletions VALUES(?,?,?,?,?,?,?)",
                       (organization_id, kind, entity_id, plan["name"], user_id, now, json.dumps(plan["counts"], sort_keys=True)))
            self._audit(db, organization_id, user_id, f"{kind}.delete", entity_id)

    def post_message(self, user_id: str, organization_id: str, topic_id: str, content: str, client_id: str,
                     reply_to: str | None = None, adds_to: str | None = None) -> dict[str, Any]:
        """``adds_to`` names a running model reply in this topic that reads the message at its next step."""
        with self.transaction(write=True) as db:
            self._topic_access(db, user_id, organization_id, topic_id, write=True)
            result = self._post_message(db, user_id, organization_id, topic_id, content, client_id, reply_to=reply_to)
            if adds_to is not None:
                from . import inference_additions
                from .inference_store import InferenceStore

                added = inference_additions.for_message(db, organization_id, result["id"])
                if added is None:
                    message = db.execute("SELECT * FROM messages WHERE organization_id=? AND id=?",
                                         (organization_id, result["id"])).fetchone()
                    inference_additions.link(db, InferenceStore(self), organization_id, topic_id, adds_to, message, user_id)
                elif added["run_id"] != adds_to:
                    raise StoreError(409, "client_id was already used for a different message")
                result["addition"] = inference_additions.for_message(db, organization_id, result["id"])
            return result

    def edit_message(self, user_id, organization_id, topic_id, message_id, content, revision):
        if not isinstance(content, str) or not content.strip() or len(content) > 16000 or type(revision) is not int or revision < 0:
            raise StoreError(400, "invalid message edit")
        with self.transaction(write=True) as db:
            self._topic_access(db, user_id, organization_id, topic_id, write=True)
            message = _row(db.execute("SELECT * FROM messages WHERE organization_id=? AND topic_id=? AND id=?",
                                     (organization_id, topic_id, message_id)).fetchone())
            if message["sender_kind"] != "human" or message["author_id"] != user_id:
                raise StoreError(403, "only the human author can edit this message")
            # Exact retries are safe, but a different stale draft cannot overwrite a newer edit.
            if message["revision"] == revision + 1 and message["content"] == content:
                return self._message(message)
            if message["revision"] != revision:
                raise StoreError(409, "message changed; review the current version before saving")
            if content == message["content"]:
                return self._message(message)
            if revision == 0:
                db.execute("INSERT INTO message_revisions(organization_id,message_id,revision,content,edited_by,created_at) "
                           "VALUES(?,?,?,?,?,?)", (organization_id, message_id, 0, message["content"], user_id, message["created_at"]))
            now = int(time.time())
            db.execute("INSERT INTO message_revisions(organization_id,message_id,revision,content,edited_by,created_at) "
                       "VALUES(?,?,?,?,?,?)", (organization_id, message_id, revision + 1, content, user_id, now))
            db.execute("UPDATE messages SET content=?,revision=?,edited_at=? WHERE id=?", (content, revision + 1, now, message_id))
            db.execute("DELETE FROM message_mentions WHERE organization_id=? AND message_id=?", (organization_id, message_id))
            for mentioned in sorted(set(re.findall(r"@\[([a-f0-9]{32})(?::[^\]]+)?\]", content))):
                if db.execute("SELECT 1 FROM memberships WHERE organization_id=? AND user_id=?", (organization_id, mentioned)).fetchone():
                    db.execute("INSERT INTO message_mentions VALUES(?,?,?)", (organization_id, message_id, mentioned))
            # Editing emits no webhook/bot event, inference request, or workflow execution.
            self._audit(db, organization_id, user_id, "message.edit", message_id)
            return self._message(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def message_history(self, user_id, organization_id, topic_id, message_id):
        with self.transaction() as db:
            self._topic_access(db, user_id, organization_id, topic_id)
            message = _row(db.execute("SELECT * FROM messages WHERE organization_id=? AND topic_id=? AND id=?",
                                     (organization_id, topic_id, message_id)).fetchone())
            if not message["revision"]:
                return [{"revision": 0, "content": message["content"], "edited_by": message["author_id"],
                         "created_at": message["created_at"]}]
            return [dict(row) for row in db.execute("SELECT revision,content,edited_by,created_at FROM message_revisions "
                "WHERE organization_id=? AND message_id=? ORDER BY revision", (organization_id, message_id))]

    def message_updates(self, user_id, organization_id, topic_id, after=0, limit=200):
        with self.transaction() as db:
            self._topic_access(db, user_id, organization_id, topic_id)
            changes = list(db.execute("SELECT r.sequence,r.message_id FROM message_revisions r JOIN messages m ON m.id=r.message_id "
                "WHERE r.organization_id=? AND m.topic_id=? AND r.sequence>? ORDER BY r.sequence LIMIT ?",
                (organization_id, topic_id, after, limit)))
            messages = [self._message(db.execute("SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                "WHERE m.id=?", (message_id,)).fetchone(), db) for message_id in dict.fromkeys(row["message_id"] for row in changes)]
            return {"after": changes[-1]["sequence"] if changes else after, "messages": messages}

    @staticmethod
    def original_message(db, row):
        message = dict(row)
        if message.get("revision", 0):
            original = db.execute("SELECT content FROM message_revisions WHERE organization_id=? AND message_id=? AND revision=0",
                                  (message["organization_id"], message["id"])).fetchone()
            if original:
                message.update(content=original[0], revision=0, edited_at=None)
        return message

    @staticmethod
    def _message(row, db=None) -> dict[str, Any]:
        result = _row(row)
        result["author_kind"] = result.pop("sender_kind")
        if db is not None and result["author_kind"] == "human":
            from .inference_additions import for_message

            addition = for_message(db, result["organization_id"], result["id"])
            if addition is not None:
                result["addition"] = addition
        name = result.pop("sender_name", None)
        if result["author_kind"] == "bot":
            result["bot_name"] = name
            if db is not None and result.get("client_id", "").startswith("bot-run-"):
                assignment = db.execute("SELECT role FROM topic_agent_tasks WHERE child_bot_run=?",
                                        (result["client_id"][8:40],)).fetchone()
                if assignment:
                    result["agent_role"] = assignment[0]
        if db is not None and result.get("inference_run_id"):
            from .usage_store import footprint

            run = db.execute("SELECT prompt_tokens,completion_tokens,actual_microusd,impact_profile_json "
                             "FROM llm_runs WHERE organization_id=? AND id=?",
                             (result["organization_id"], result["inference_run_id"])).fetchone()
            result["usage"] = footprint(run) if run is not None else None
            reply = db.execute("SELECT id,organization_id,request_id FROM llm_runs WHERE organization_id=? AND id=?",
                               (result["organization_id"], result["inference_run_id"])).fetchone()
            if reply is not None:
                from .inference_additions import read_by_reply
                from .reply_steps import run_activity

                # The reply's steps and the later messages it took into account, folded under the answer.
                additions = read_by_reply(db, reply)
                if additions:
                    result["additions"] = additions
                steps = run_activity(db, reply["organization_id"], reply["id"])
                if steps:
                    result["steps"] = steps
            assignment = db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id="
                                    "(SELECT request_id FROM llm_runs WHERE id=?)", (result["inference_run_id"],)).fetchone()
            if assignment:
                metadata = json.loads(assignment[0])
                if metadata.get("agent_role"):
                    result["agent_role"] = metadata["agent_role"]
            parent = db.execute(
                "SELECT w.run_id FROM workflow_node_attempts w JOIN workflow_runs r ON r.id=w.run_id "
                "AND r.organization_id=w.organization_id WHERE w.organization_id=? AND w.inference_run_id=? AND r.topic_id=? LIMIT 1",
                (result["organization_id"], result["inference_run_id"], result["topic_id"]),
            ).fetchone()
            if parent is not None:
                result["workflow_run_id"] = parent[0]
        if db is not None and result["author_kind"] in {"assistant", "bot"}:
            from .research_artifacts import reply_files

            files = reply_files(db, result)
            if files:
                result["files"] = files
        return result

    def _post_message(self, db, user_id, organization_id, topic_id, content, client_id, *, sender_kind="human",
                      reply_to=None) -> dict[str, Any]:
        # Callers must authorize the principal and topic inside this same transaction.
        existing = db.execute(
            "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
            "WHERE m.organization_id=? AND m.author_id=? AND m.client_id=?",
            (organization_id, user_id, client_id),
        ).fetchone()
        if existing:
            original = db.execute("SELECT content FROM message_revisions WHERE message_id=? AND revision=0", (existing["id"],)).fetchone()
            original_content = original[0] if original else existing["content"]
            if (existing["topic_id"] != topic_id or original_content != content or existing["sender_kind"] != sender_kind
                    or existing["reply_to"] != reply_to):
                raise StoreError(409, "client_id was already used for a different message")
            return self._message(existing)
        # A reply names a message of the same topic, so it never reveals or links across topics.
        if reply_to is not None and not db.execute("SELECT 1 FROM messages WHERE organization_id=? AND topic_id=? AND id=?",
                                                   (organization_id, topic_id, reply_to)).fetchone():
            raise StoreError(400, "a reply must answer a message in the same topic")
        message_id = _id()
        db.execute(
            "INSERT INTO messages(id,organization_id,topic_id,author_id,content,client_id,created_at,sender_kind,reply_to) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message_id, organization_id, topic_id, user_id, content, client_id, int(time.time()), sender_kind, reply_to),
        )
        for mentioned in sorted(set(re.findall(r"@\[([a-f0-9]{32})(?::[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?\]", content))):
            if db.execute("SELECT 1 FROM memberships WHERE organization_id=? AND user_id=?", (organization_id, mentioned)).fetchone():
                db.execute("INSERT INTO message_mentions VALUES(?,?,?)", (organization_id, message_id, mentioned))
        result = self._message(
            db.execute(
                "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?", (message_id,)
            ).fetchone()
        )
        from .webhook_store import enqueue

        enqueue(db, result)
        return result

    def keys(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            self._require(db, user_id, organization_id, admin=True)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,provider,label,fingerprint,created_at,disabled_at FROM provider_keys WHERE organization_id=?",
                    (organization_id,),
                )
            ]

    def add_key(
        self,
        user_id: str,
        organization_id: str,
        provider: str,
        label: str,
        value: str,
        *,
        allowed_models: list[str] | None = None,
    ) -> dict[str, Any]:
        if provider != "openrouter":
            raise StoreError(400, "unsupported provider")
        if allowed_models is not None and (
            len(allowed_models) > 10000
            or any(not isinstance(model, str) or not 1 <= len(model) <= 200 for model in allowed_models)
        ):
            raise StoreError(400, "invalid text-model catalog")
        with self.transaction(write=True) as db:
            self._require(db, user_id, organization_id, admin=True)
            key_id, now = _id(), int(time.time())
            ciphertext = self._box.seal(value, organization_id=organization_id, provider=provider, key_id=key_id)
            fingerprint = digest(value)[:16]
            db.execute(
                "INSERT INTO provider_keys VALUES(?,?,?,?,?,?,?,NULL)",
                (key_id, organization_id, provider, label, ciphertext, fingerprint, now),
            )
            self._audit(db, organization_id, user_id, "provider-key.create", key_id)
            return {"id": key_id, "provider": provider, "label": label, "fingerprint": fingerprint, "created_at": now, "disabled_at": None}

    def disable_key(self, user_id: str, organization_id: str, key_id: str) -> None:
        with self.transaction(write=True) as db:
            self._require(db, user_id, organization_id, admin=True)
            _row(db.execute("SELECT id FROM provider_keys WHERE organization_id=? AND id=?", (organization_id, key_id)).fetchone())
            db.execute("UPDATE provider_keys SET disabled_at=? WHERE id=?", (int(time.time()), key_id))
            self._audit(db, organization_id, user_id, "provider-key.disable", key_id)

    def policy(self, user_id: str, organization_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            self._require(db, user_id, organization_id, admin=True)
            row = _row(
                db.execute("SELECT model_policy_json,monthly_budget_microusd FROM organizations WHERE id=?", (organization_id,)).fetchone()
            )
            return {"allowed_models": json.loads(row["model_policy_json"]), "monthly_budget_microusd": row["monthly_budget_microusd"]}

    def set_policy(self, user_id: str, organization_id: str, models: list[str], budget: int) -> dict[str, Any]:
        if budget < 0:
            raise StoreError(400, "invalid budget")
        with self.transaction(write=True) as db:
            self._require(db, user_id, organization_id, admin=True)
            db.execute(
                "UPDATE organizations SET model_policy_json=?,monthly_budget_microusd=? WHERE id=?",
                (json.dumps(sorted(set(models))), budget, organization_id),
            )
            self._audit(db, organization_id, user_id, "organization.policy", organization_id)
            return {"allowed_models": sorted(set(models)), "monthly_budget_microusd": budget}

    def audit(self, user_id: str, organization_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.transaction() as db:
            self._require(db, user_id, organization_id, admin=True)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_events WHERE organization_id=? AND sequence>? ORDER BY sequence LIMIT 200",
                    (organization_id, after),
                )
            ]
