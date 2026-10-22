"""Optional operator-owned HTTPS invitation mail with a durable, encrypted outbox."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .security import private_file
from .store import Store, StoreError
from .worker_poll import claim_work

MAIL_ENDPOINT = "https://api.resend.com/emails"


def mailbox(value: object) -> str:
    # Only one ASCII addr-spec; no display names, control characters, or lists.
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 254
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)
    ):
        raise ValueError("email delivery requires a single ASCII email address")
    local, domain = value.split("@")
    if (
        len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in value
        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in domain.split("."))
    ):
        raise ValueError("invalid email delivery address")
    return value


@dataclass(frozen=True)
class MailConfig:
    sender: str
    api_key: str = field(repr=False)
    daily_limit: int = 100

    def __post_init__(self):
        mailbox(self.sender)
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,4096}", self.api_key):
            raise ValueError("invalid mail credential")
        if type(self.daily_limit) is not int or not 1 <= self.daily_limit <= 10000:
            raise ValueError("mail daily limit must be between 1 and 10000")

    def document(self):
        return {"format": 1, "provider": "resend", "sender": self.sender, "api_key": self.api_key, "daily_limit": self.daily_limit}

    @classmethod
    def read(cls, path: Path):
        from reasoning_ledger.deployment.private import read_private

        try:

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            value = json.loads(read_private(path, 8192), object_pairs_hook=unique)
            if (
                not isinstance(value, dict)
                or set(value) != {"format", "provider", "sender", "api_key", "daily_limit"}
                or type(value["format"]) is not int
                or value["format"] != 1
                or value["provider"] != "resend"
            ):
                raise ValueError
            return cls(value["sender"], value["api_key"], value["daily_limit"])
        except Exception as exc:
            # File names, JSON values, and upstream errors never become CLI/API diagnostics.
            raise ValueError("invalid or unavailable private mail configuration") from exc


class MailStore:
    def __init__(self, store: Store, config: MailConfig | None, public_url: str):
        self.store, self.config, self.public_url = store, config, public_url
        self.binding = (
            hmac.digest(
                store._mail_binding_key,
                json.dumps({"sender": config.sender, "key": config.api_key, "origin": public_url}, sort_keys=True).encode(),
                "sha256",
            ).hex()
            if config
            else None
        )
        from .email_verification import EmailVerification

        self.verification = EmailVerification(self)

    def check_limits(self, db, now):
        count = sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE created_at>?", (now - 86400,)).fetchone()[0]
                    for table in ("invitation_mail", "email_verifications"))
        if count >= self.config.daily_limit:
            raise StoreError(429, "installation invitation email daily limit reached")

    @staticmethod
    def _delivery_slot(db, now):
        # The indexed, scope-separated rolling quota ledger is shared with other
        # installation quotas. Commit the attempt with the claim, before HTTPS.
        scope = "mail:delivery"
        db.execute("DELETE FROM auth_attempts WHERE scope=? AND attempted_at<=?", (scope, now - 60))
        return db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=?", (scope,)).fetchone()[0] < 10

    @staticmethod
    def _sweep(db):
        now = int(time.time())
        # An invalid invitation cannot be sent or replayed. Keep nonsecret history.
        db.execute(
            "UPDATE invitation_mail SET ciphertext=NULL, state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END "
            "WHERE invitation_id IN (SELECT id FROM invitations WHERE accepted_at IS NOT NULL OR revoked_at IS NOT NULL OR expires_at<=?)",
            (now,),
        )

    def configuration(self, actor, organization):
        with self.store.transaction() as db:
            self.store._require(db, actor, organization, admin=True)
        return {"enabled": self.config is not None, "daily_limit": self.config.daily_limit if self.config else None}

    def enqueue_batch(self, actor, organization, emails, role, lifetime, client_id):
        """Per-recipient durable identities allow partial rate-limited batches to resume."""
        import hashlib

        with self.store.transaction() as db:
            actor_role = self.store._require(db, actor, organization, admin=True)
            if role == "admin" and actor_role != "owner":
                raise StoreError(403, "only owners can invite administrators")
        if self.config is None:
            raise StoreError(409, "invitation email is not configured")
        results = []
        for email in dict.fromkeys(address.strip().lower() for address in emails):
            identity = hashlib.sha256((client_id + "\0" + email).encode()).hexdigest()
            try:
                invitation = self.enqueue(actor, organization, email, role, lifetime, identity, _receipt_only=True)
                results.append({"email": email, "status": "queued", "invitation_id": invitation["id"],
                                "email_state": invitation["email_state"]})
            except StoreError as exc:
                results.append({"email": email, "status": "retryable" if exc.status == 429 else "failed",
                                "http_status": exc.status, "error": str(exc)})
        retry_after = None
        if any(row["status"] == "retryable" for row in results):
            now = int(time.time())
            with self.store.transaction() as db:
                earliest = [db.execute(f"SELECT MIN(created_at) FROM {table} WHERE created_at>?", (now - 86400,)).fetchone()[0]
                            for table in ("invitation_mail", "email_verifications")]
            retry_after = max(1, min((value for value in earliest if value is not None), default=now - 86400) + 86400 - now)
        return {"results": results, "retry_after_seconds": retry_after}

    def enqueue(self, actor, organization, email, role, lifetime, client_id, *, replaces=None, _receipt_only=False):
        try:
            mailbox(email)
        except ValueError as exc:
            raise StoreError(400, "invalid invitation email delivery address") from exc
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
            raise StoreError(400, "email invitations require a stable client_id of 16–128 ASCII identifier characters")
        if self.config is None:
            raise StoreError(409, "invitation email is not configured")
        inputs = {"email": email.lower(), "role": role, "lifetime": lifetime}
        if replaces is not None:
            inputs["replaces"] = replaces
        request = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
        now = int(time.time())
        with self.store.transaction(write=True) as db:
            actor_role = self.store._require(db, actor, organization, admin=True)
            if role == "admin" and actor_role != "owner":
                raise StoreError(403, "only owners can invite administrators")
            self._sweep(db)
            previous = db.execute(
                "SELECT * FROM invitation_mail WHERE organization_id=? AND requested_by=? AND client_id=?",
                (organization, actor, client_id),
            ).fetchone()
            if previous:
                if previous["request_json"] != request or previous["configuration_hash"] != self.binding:
                    raise StoreError(409, "invitation request requires its original input and mail configuration")
                invite = dict(
                    db.execute("SELECT id,email,role,expires_at FROM invitations WHERE id=?", (previous["invitation_id"],)).fetchone()
                )
                if _receipt_only:
                    return {**invite, "email_state": previous["state"]}
                if previous["ciphertext"] is None:
                    raise StoreError(409, "invitation is no longer available")
                payload = json.loads(
                    self.store._box.open_mail(previous["ciphertext"], organization_id=organization, invitation_id=previous["invitation_id"])
                )
                return {**invite, "token": payload["token"], "email_state": previous["state"]}
            if replaces is not None:
                old = db.execute("SELECT * FROM invitations WHERE id=? AND organization_id=?", (replaces, organization)).fetchone()
                if old is None or old["accepted_at"] is not None or old["revoked_at"] is not None:
                    raise StoreError(409, "invitation is no longer available to replace")
                if (old["email"], old["role"]) != (email.lower(), role):
                    raise StoreError(409, "replacement must keep the invited email and role")
            # Limits cover all organizations using this operator's mail account, including rejected messages.
            self.check_limits(db, now)
            invite = self.store._invite(db, actor, organization, email, role, lifetime)
            name = db.execute("SELECT name FROM organizations WHERE id=?", (organization,)).fetchone()[0]
            payload = {
                "token": invite["token"],
                "message": {
                    "from": self.config.sender,
                    "to": [invite["email"]],
                    "subject": "Invitation to Reasoning",
                    "text": f"You have been invited to {name} on Reasoning as a {role}.\n\n"
                    f"Open this link to join:\n{self.public_url}/#invite={invite['token']}\n\n"
                    f"This invitation expires in {lifetime} seconds. If you did not expect it, ignore this email.\n",
                },
            }
            ciphertext = self.store._box.seal_mail(json.dumps(payload), organization_id=organization, invitation_id=invite["id"])
            db.execute(
                "INSERT INTO invitation_mail(invitation_id,organization_id,requested_by,client_id,request_json,configuration_hash,"
                "ciphertext,state,created_at) VALUES(?,?,?,?,?,?,?,'queued',?)",
                (invite["id"], organization, actor, client_id, request, self.binding, ciphertext, now),
            )
            self.store._audit(db, organization, actor, "invitation.email.queue", invite["id"])
            if replaces is not None:
                db.execute("UPDATE invitations SET revoked_at=? WHERE id=?", (now, replaces))
                self.store._retire_invitation_mail(db, replaces)
                self.store._audit(db, organization, actor, "invitation.revoke", replaces)
            return {**invite, "email_state": "queued"}

    def recover(self):
        with self.store.transaction(write=True) as db:
            self._sweep(db)
            db.execute(
                "UPDATE invitation_mail SET state='unknown',error_code='worker_interrupted',finished_at=? WHERE state='sending'",
                (int(time.time()),),
            )
            db.execute("UPDATE email_verifications SET state='unknown',error_code='worker_interrupted' WHERE state='sending'")

    def claim(self):
        if self.config is None:
            return None
        with self.store.transaction(write=True) as db:
            self._sweep(db)
            now = int(time.time())
            if not self._delivery_slot(db, now):
                return None
            while row := db.execute(
                "SELECT * FROM invitation_mail WHERE state='queued' ORDER BY created_at,invitation_id LIMIT 1"
            ).fetchone():
                try:
                    role = self.store._require(db, row["requested_by"], row["organization_id"], admin=True)
                    invitation_role = db.execute("SELECT role FROM invitations WHERE id=?", (row["invitation_id"],)).fetchone()[0]
                    if invitation_role == "admin" and role != "owner":
                        raise StoreError(403, "owner required")
                except StoreError:
                    db.execute(
                        "UPDATE invitation_mail SET state='cancelled',ciphertext=NULL,error_code='authority_changed' WHERE invitation_id=?",
                        (row["invitation_id"],),
                    )
                    continue
                if row["configuration_hash"] != self.binding:
                    db.execute(
                        "UPDATE invitation_mail SET state='cancelled',ciphertext=NULL,error_code='configuration_changed' "
                        "WHERE invitation_id=?",
                        (row["invitation_id"],),
                    )
                    continue
                payload = json.loads(
                    self.store._box.open_mail(row["ciphertext"], organization_id=row["organization_id"], invitation_id=row["invitation_id"])
                )
                db.execute(
                    "UPDATE invitation_mail SET state='sending',attempted_at=? WHERE invitation_id=?",
                    (now, row["invitation_id"]),
                )
                db.execute("INSERT INTO auth_attempts VALUES(?,?)", ("mail:delivery", now))
                return {"id": row["invitation_id"], "organization_id": row["organization_id"], "message": payload["message"]}
            verification = self.verification.claim(db)
            if verification:
                db.execute("INSERT INTO auth_attempts VALUES(?,?)", ("mail:delivery", now))
            return verification

    def finish(self, row, state, provider_id=None, error=None):
        if state not in {"accepted", "rejected", "unknown"}:
            raise ValueError("invalid mail completion")
        if row.get("kind") == "verification":
            return self.verification.finish(row, "sent" if state == "accepted" else state, provider_id, error)
        with self.store.transaction(write=True) as db:
            updated = db.execute(
                "UPDATE invitation_mail SET state=?,provider_id=?,error_code=?,finished_at=? "
                "WHERE invitation_id=? AND organization_id=? AND state='sending'",
                (state, provider_id, error, int(time.time()), row["id"], row["organization_id"]),
            )
            if updated.rowcount:
                self.store._audit(db, row["organization_id"], None, "invitation.email." + state, row["id"])
            self._sweep(db)


class MailError(Exception):
    def __init__(self, state, code):
        super().__init__(code)
        self.state, self.code = state, code


class Resend:
    def __init__(self, config: MailConfig, *, transport=None):
        self.config, self.transport = config, transport

    async def send(self, row):
        async def submit():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20, transport=self.transport) as client:  # noqa: SIM117
                async with client.stream(
                    "POST",
                    MAIL_ENDPOINT,
                    json=row["message"],
                    headers={
                        "Authorization": "Bearer " + self.config.api_key,
                        "Idempotency-Key": "reasoning-invitation/" + row["id"],
                    },
                ) as response:
                    if response.status_code in {400, 401, 403, 404, 422, 429}:
                        raise MailError("rejected", "provider_rejected")
                    if response.status_code not in {200, 201}:
                        raise MailError("unknown", "provider_uncertain")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4096:
                            raise MailError("unknown", "provider_protocol")
                    value = json.loads(body)
                    if (
                        not isinstance(value, dict)
                        or not isinstance(value.get("id"), str)
                        or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value["id"])
                    ):
                        raise MailError("unknown", "provider_protocol")
                    return value["id"]

        try:
            return await asyncio.wait_for(submit(), timeout=30)
        except MailError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as exc:
            raise MailError("unknown", "provider_uncertain") from exc


class MailWorker:
    def __init__(self, state: MailStore, provider=None):
        self.state = state
        self.provider = provider or (Resend(state.config) if state.config else None)
        self._stop = threading.Event()
        self._thread = None
        self._lock = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self):
        if self._thread is not None or self.provider is None:
            raise ValueError("mail worker is unavailable or already started")
        filename = self.state.store.data_dir / "mail-worker.lock"
        private_file(filename, b"")
        lock = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state.recover()
            self._lock = lock
            self._thread = threading.Thread(target=self._run, name="reasoning-mail", daemon=True)
            self._thread.start()
        except BaseException:
            self._lock = None
            self._thread = None
            os.close(lock)
            raise

    async def execute_once(self):
        row = claim_work(self.state.claim)
        if row is None:
            return False
        try:
            provider_id = await self.provider.send(row)
        except MailError as exc:
            self.state.finish(row, exc.state, error=exc.code)
        except Exception:
            # Do not print exception text or user/provider values from a failed adapter.
            self.state.finish(row, "unknown", error="worker_interrupted")
        except BaseException:
            self.state.finish(row, "unknown", error="worker_interrupted")
            raise
        else:
            self.state.finish(row, "accepted", provider_id=provider_id)
        return True

    def _run(self):
        try:
            while not self._stop.is_set():
                asyncio.run(self.execute_once())
                self._stop.wait(1)
        finally:
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise ValueError("mail worker did not stop; its lock remains held")
