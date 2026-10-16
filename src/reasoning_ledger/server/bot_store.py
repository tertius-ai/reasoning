"""Organization-sponsored bot principals with live topic grants and durable quotas."""

from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext

from .security import digest, token
from .store import Store, StoreError, _id, _row

SCOPES = {"messages:read", "messages:write", "knowledge:read", "knowledge:write"}
TOKEN = re.compile(r"rnb_[A-Za-z0-9_-]{43}\Z")
IDENTITY = re.compile(r"[a-f0-9]{32}\Z")
REQUESTS_PER_MINUTE = 300
WRITES_PER_MINUTE = 30
MAX_BOTS = 100


class BotStore:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _personal_grants(db, user, org, topics, scopes):
        write = any(scope.endswith(":write") for scope in scopes)
        Store._require(db, user, org, write=write)
        for topic in topics:
            Store._topic_access(db, user, org, topic, write=write)

    @staticmethod
    def _authority(db, row):
        if row["delegation_kind"] == "personal":
            if row["authorized_by"] != row["created_by"]:
                raise StoreError(403, "personal bot ownership differs from its authorization")
            topics = [item[0] for item in db.execute("SELECT topic_id FROM bot_topics WHERE bot_id=?", (row["id"],))]
            if not topics and not row["public_access"]:
                raise StoreError(403, "personal bot has no current topic grants")
            # Following access is an intersection with live owner permissions, not
            # a requirement that every old explicit grant remain accessible.
            BotStore._personal_grants(db, row["created_by"], row["organization_id"],
                                      [] if row["public_access"] else topics, json.loads(row["scopes_json"]))
        else:
            Store._require(db, row["authorized_by"], row["organization_id"], admin=True)

    @staticmethod
    def topic_ids(db, row):
        explicit = {item[0] for item in db.execute("SELECT topic_id FROM bot_topics WHERE bot_id=?", (row["id"],))}
        if row["public_access"] and row["delegation_kind"] == "personal":
            try:
                role = Store._require(db, row["created_by"], row["organization_id"],
                                      write=any(scope.endswith(":write") for scope in json.loads(row["scopes_json"])))
            except StoreError:
                return []
            return [item[0] for item in db.execute(
                "SELECT t.id FROM topics t JOIN channels c ON c.id=t.channel_id JOIN projects p ON p.id=c.project_id "
                "LEFT JOIN project_memberships pm ON pm.organization_id=p.organization_id AND pm.project_id=p.id "
                "AND pm.user_id=? WHERE t.organization_id=? AND t.deleted_at IS NULL AND c.deleted_at IS NULL "
                "AND p.deleted_at IS NULL AND (p.visibility='organization' OR ? IN ('owner','admin') "
                "OR pm.user_id IS NOT NULL) ORDER BY t.id", (row["created_by"], row["organization_id"], role))]
        if row["public_access"]:
            explicit.update(item[0] for item in db.execute(
                "SELECT t.id FROM topics t JOIN channels c ON c.id=t.channel_id JOIN projects p ON p.id=c.project_id "
                "WHERE t.organization_id=? AND p.visibility='organization' AND p.deleted_at IS NULL "
                "AND c.deleted_at IS NULL AND t.deleted_at IS NULL", (row["organization_id"],)))
        if not explicit:
            return []
        return [item[0] for item in db.execute("SELECT t.id FROM topics t JOIN channels c ON c.id=t.channel_id "
            "JOIN projects p ON p.id=c.project_id WHERE t.organization_id=? AND t.id IN ("
            + ",".join("?" for _ in explicit) + ") AND t.deleted_at IS NULL AND c.deleted_at IS NULL "
            "AND p.deleted_at IS NULL ORDER BY t.id", (row["organization_id"], *sorted(explicit)))]

    @staticmethod
    def _manage(db, user, org, identity, personal):
        Store._require(db, user, org, admin=not personal)
        row = _row(db.execute("SELECT * FROM bots WHERE organization_id=? AND id=?", (org, identity)).fetchone())
        if personal and row["created_by"] != user:
            raise StoreError(404, "bot not found")
        return row

    @staticmethod
    def _arguments(name, topics, scopes, lifetime=None, public_access=False):
        if type(public_access) is not bool:
            raise StoreError(400, "choose an explicit public-conversation policy")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise StoreError(400, "bot name must contain 1–200 characters")
        if (
            not isinstance(topics, list)
            or not (0 if public_access else 1) <= len(topics) <= 64
            or any(not isinstance(topic, str) or not IDENTITY.fullmatch(topic) for topic in topics)
            or len(set(topics)) != len(topics)
        ):
            raise StoreError(400, "select one to 64 distinct topic identities")
        if (
            not isinstance(scopes, list)
            or not 1 <= len(scopes) <= 4
            or any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            raise StoreError(400, "select valid message and/or knowledge permissions")
        if lifetime is not None and (type(lifetime) is not int or not 3600 <= lifetime <= 31_536_000):
            raise StoreError(400, "bot credential lifetime must be one hour through 365 days")

    @staticmethod
    def _topics(db, org, topics):
        count = db.execute(
            "SELECT COUNT(*) FROM topics WHERE organization_id=? AND deleted_at IS NULL AND id IN ("
            + ",".join("?" for _ in topics) + ")",
            (org, *topics),
        ).fetchone()[0]
        if count != len(topics):
            raise StoreError(404, "topic unavailable")

    @staticmethod
    def _public(db, row) -> dict:
        try:
            BotStore._authority(db, row)
            authorized = True
        except StoreError:
            authorized = False
        return {
            **{
                field: row[field]
                for field in (
                    "id",
                    "organization_id",
                    "created_by",
                    "authorized_by",
                    "created_at",
                    "expires_at",
                    "revoked_at",
                    "last_used_at",
                    "delegation_kind",
                )
            },
            "name": db.execute("SELECT name FROM users WHERE id=?", (row["id"],)).fetchone()[0],
            "scopes": json.loads(row["scopes_json"]),
            "topic_ids": BotStore.topic_ids(db, row),
            "explicit_topic_ids": [item[0] for item in db.execute("SELECT topic_id FROM bot_topics WHERE bot_id=?", (row["id"],))],
            "public_access": bool(row["public_access"]),
            "developer": BotStore._developer(db, row["id"]),
            "runtime": BotStore._runtime(db, row["id"]),
            "enabled": row["revoked_at"] is None
            and row["expires_at"] > int(time.time())
            and authorized,
        }

    @staticmethod
    def _runtime(db, identity):
        row = db.execute("SELECT engine,models_json,local_tools,last_seen_at FROM bot_runtimes WHERE bot_id=?", (identity,)).fetchone()
        return {"engine": row["engine"], "models": json.loads(row["models_json"]), "local_tools": bool(row["local_tools"]),
                "online": row["last_seen_at"] >= int(time.time()) - 45} if row else None

    @staticmethod
    def _developer(db, identity):
        row = db.execute("SELECT * FROM bot_developer_permissions WHERE bot_id=?", (identity,)).fetchone()
        if not row:
            return {"enabled": False, "repositories": [], "requesters": [], "issue_self_approvers": [], "issue_daily_limit": 3}
        return {"enabled": bool(row["enabled"]), "repositories": json.loads(row["repositories_json"]),
                "requesters": json.loads(row["requesters_json"]),
                "issue_self_approvers": json.loads(row["issue_self_approvers_json"]), "issue_daily_limit": row["issue_daily_limit"]}

    def list(self, user: str, org: str, *, personal=False) -> list[dict]:
        with self.store.transaction() as db:
            self.store._require(db, user, org, admin=not personal)
            return [
                self._public(db, row) for row in db.execute("SELECT * FROM bots WHERE organization_id=? "
                    + ("AND created_by=? " if personal else "") + "ORDER BY created_at,id", (org, user) if personal else (org,))
            ]

    def create(self, user: str, org: str, name: str, topics: list[str], scopes: list[str], lifetime: int = 2_592_000,
               *, personal=False, _db=None, _credential_hash=None, public_access=False) -> dict:
        self._arguments(name, topics, scopes, lifetime, public_access)
        if _credential_hash is not None and not re.fullmatch(r"[a-f0-9]{64}", _credential_hash):
            raise StoreError(400, "invalid credential digest")
        with (nullcontext(_db) if _db is not None else self.store.transaction(write=True)) as db:
            self.store._require(db, user, org, admin=not personal)
            self._topics(db, org, topics)
            if personal:
                self._personal_grants(db, user, org, topics, scopes)
                if db.execute("SELECT COUNT(*) FROM bots WHERE organization_id=? AND created_by=? AND revoked_at IS NULL",
                              (org, user)).fetchone()[0] >= 10:
                    raise StoreError(409, "revoke an unused personal bot before connecting another")
            if db.execute("SELECT COUNT(*) FROM bots WHERE organization_id=? AND revoked_at IS NULL", (org,)).fetchone()[0] >= MAX_BOTS:
                raise StoreError(409, "organization has 100 unrevoked bots; revoke an unused identity first")
            identity, credential, now = _id(), "rnb_" + token(), int(time.time())
            db.execute(
                "INSERT INTO users(id,email,name,password_hash,created_at,account_kind) VALUES(?,?,?,'disabled',?,'bot')",
                (identity, f"bot-{identity}@bots.invalid", name, now),
            )
            db.execute(
                "INSERT INTO bots(id,organization_id,created_by,authorized_by,token_hash,scopes_json,created_at,expires_at,"
                "delegation_kind,public_access) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identity, org, user, user, _credential_hash or digest(credential), json.dumps(sorted(scopes)), now, now + lifetime,
                 "personal" if personal else "administrator", public_access),
            )
            db.executemany("INSERT INTO bot_topics VALUES(?,?,?)", [(org, identity, topic) for topic in sorted(topics)])
            self.store._audit(db, org, user, "bot.create", identity)
            public = self._public(db, db.execute("SELECT * FROM bots WHERE id=?", (identity,)).fetchone())
            return public if _credential_hash else {**public, "token": credential}

    def update(self, user: str, org: str, identity: str, name: str, topics: list[str], scopes: list[str], *, personal=False,
               public_access=False) -> dict:
        from .webhook_store import cancel_ineligible

        self._arguments(name, topics, scopes, public_access=public_access)
        with self.store.transaction(write=True) as db:
            row = self._manage(db, user, org, identity, personal)
            if row["delegation_kind"] == "administrator":
                self.store._require(db, user, org, admin=True)
            if row["revoked_at"] is not None:
                raise StoreError(409, "revoked bot identities cannot be reactivated")
            self._topics(db, org, topics)
            sponsor = row["created_by"] if row["delegation_kind"] == "personal" else user
            if row["delegation_kind"] == "personal":
                self._personal_grants(db, sponsor, org, topics, scopes)
            cancel_ineligible(db, org)
            db.execute("UPDATE users SET name=? WHERE id=?", (name, identity))
            db.execute("UPDATE bots SET authorized_by=?,scopes_json=?,public_access=? WHERE id=?",
                       (sponsor, json.dumps(sorted(scopes)), public_access, identity))
            db.execute("DELETE FROM bot_topics WHERE bot_id=?", (identity,))
            db.executemany("INSERT INTO bot_topics VALUES(?,?,?)", [(org, identity, topic) for topic in sorted(topics)])
            cancel_ineligible(db, org)
            self.store._audit(db, org, user, "bot.permissions", identity)
            return self._public(db, db.execute("SELECT * FROM bots WHERE id=?", (identity,)).fetchone())

    def rotate(self, user: str, org: str, identity: str, lifetime: int = 2_592_000, *, personal=False) -> dict:
        from .webhook_store import cancel_ineligible

        if type(lifetime) is not int or not 3600 <= lifetime <= 31_536_000:
            raise StoreError(400, "bot credential lifetime must be one hour through 365 days")
        with self.store.transaction(write=True) as db:
            row = self._manage(db, user, org, identity, personal)
            if row["delegation_kind"] == "administrator":
                self.store._require(db, user, org, admin=True)
            if row["revoked_at"] is not None:
                raise StoreError(409, "revoked bot identities cannot be reactivated")
            credential = "rnb_" + token()
            sponsor = row["created_by"] if row["delegation_kind"] == "personal" else user
            if row["delegation_kind"] == "personal":
                self._authority(db, row)
            cancel_ineligible(db, org)
            db.execute(
                "UPDATE bots SET token_hash=?,expires_at=?,authorized_by=? WHERE id=?",
                (digest(credential), int(time.time()) + lifetime, sponsor, identity),
            )
            self.store._audit(db, org, user, "bot.rotate", identity)
            return {**self._public(db, db.execute("SELECT * FROM bots WHERE id=?", (identity,)).fetchone()), "token": credential}

    def revoke(self, user: str, org: str, identity: str, *, personal=False) -> None:
        from .webhook_store import cancel_ineligible

        with self.store.transaction(write=True) as db:
            row = self._manage(db, user, org, identity, personal)
            if row["revoked_at"] is None:
                db.execute("UPDATE bots SET revoked_at=? WHERE id=?", (int(time.time()), identity))
                cancel_ineligible(db, org)
                self.store._audit(db, org, user, "bot.revoke", identity)

    @staticmethod
    def _authenticate(db, credential: str):
        if not isinstance(credential, str) or not TOKEN.fullmatch(credential):
            raise StoreError(401, "valid bot credential required")
        row = db.execute(
            "SELECT * FROM bots WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (digest(credential), int(time.time()))
        ).fetchone()
        if row is None:
            raise StoreError(401, "valid bot credential required")
        try:
            BotStore._authority(db, row)
        except StoreError:
            raise StoreError(403, "bot owner or sponsor no longer has its delegated access") from None
        return row

    def _meter(self, credential: str, operation: str) -> None:
        # Commit consumption independently of a subsequent rejected operation.
        # The data transaction reauthenticates, so revocation/grant races cannot bypass authorization.
        with self.store.transaction(write=True) as db:
            row, now = self._authenticate(db, credential), int(time.time())
            db.execute("DELETE FROM bot_requests WHERE attempted_at<=?", (now - 60,))
            counts = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(CASE WHEN operation='write' THEN 1 ELSE 0 END),0) "
                "FROM bot_requests WHERE bot_id=? AND attempted_at>?",
                (row["id"], now - 60),
            ).fetchone()
            if counts[0] >= REQUESTS_PER_MINUTE or operation == "write" and counts[1] >= WRITES_PER_MINUTE:
                raise StoreError(429, "bot request rate exceeded; wait before retrying")
            db.execute("INSERT INTO bot_requests VALUES(?,?,?)", (row["id"], now, operation))
            db.execute("UPDATE bots SET last_used_at=? WHERE id=?", (now, row["id"]))

    def identity(self, credential: str) -> dict:
        self._meter(credential, "read")
        with self.store.transaction() as db:
            value = self._public(db, self._authenticate(db, credential))
            value.pop("developer", None)  # Human management metadata is not part of the stable bot identity wire contract.
            value.pop("runtime", None)
            return value

    def topics(self, credential: str) -> list[dict]:
        self._meter(credential, "read")
        with self.store.transaction() as db:
            row = self._authenticate(db, credential)
            return [
                dict(topic)
                for topic in db.execute(
                    "SELECT t.id,t.name,t.kind,t.channel_id,c.project_id FROM topics t JOIN channels c ON c.id=t.channel_id "
                    "WHERE t.organization_id=? AND t.id IN (" + ",".join("?" for _ in self.topic_ids(db, row)) + ") "
                    "ORDER BY t.created_at,t.id", (row["organization_id"], *self.topic_ids(db, row)),
                )
            ]

    def messages(self, credential: str, after: int = 0, limit: int = 100) -> list[dict]:
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise StoreError(400, "invalid bot message pagination")
        self._meter(credential, "read")
        with self.store.transaction() as db:
            row = self._authenticate(db, credential)
            if "messages:read" not in json.loads(row["scopes_json"]):
                raise StoreError(403, "bot lacks messages:read permission")
            return [
                self.store._message(self.store.original_message(db, message), db)
                for message in db.execute(
                    "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                    "WHERE m.organization_id=? AND m.topic_id IN (" + ",".join("?" for _ in self.topic_ids(db, row)) + ") "
                    "AND m.sequence>? ORDER BY m.sequence LIMIT ?",
                    (row["organization_id"], *self.topic_ids(db, row), after, limit),
                )
            ]

    def post(self, credential: str, topic: str, content: str, client_id: str) -> dict:
        if not isinstance(content, str) or not content.strip() or not 1 <= len(content) <= 16_000:
            raise StoreError(400, "bot message must contain 1–16000 characters")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise StoreError(400, "bot client_id must contain 1–128 characters")
        self._meter(credential, "write")
        with self.store.transaction(write=True) as db:
            row = self._authenticate(db, credential)
            if "messages:write" not in json.loads(row["scopes_json"]):
                raise StoreError(403, "bot lacks messages:write permission")
            if topic not in self.topic_ids(db, row):
                raise StoreError(404, "topic unavailable")
            return self.store._post_message(db, row["id"], row["organization_id"], topic, content, client_id, sender_kind="bot")
