"""Mailbox-proven password recovery; an invitation never replaces a password."""

import hmac
import json
import re
import time

from .security import digest, password_hash, password_matches, token
from .store import StoreError, _id, _row

ACKNOWLEDGEMENT = {"message": "If an account exists for this email address, a password reset link will be sent. "
                  "Check your inbox and spam folder."}


def invalidate_recovery(db, user, now):
    db.execute("UPDATE account_mail SET consumed_at=?,expires_at=?,ciphertext=NULL,"
               "state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END "
               "WHERE user_id=? AND kind='reset' AND consumed_at IS NULL", (now, now, user))


class AccountRecovery:
    lifetime = 30 * 60

    def __init__(self, mail):
        self.mail, self.store = mail, mail.store

    def _binding(self, encoded):
        return hmac.digest(self.store._mail_binding_key, ("password-reset:" + encoded).encode(), "sha256").hex()

    @staticmethod
    def _rate(db, email, peer, client, now):
        db.execute("DELETE FROM auth_attempts WHERE scope LIKE 'recovery:%' AND attempted_at<=?", (now - 900,))
        request = "recovery:request:" + digest(email + "\0" + client)
        if db.execute("SELECT 1 FROM auth_attempts WHERE scope=? AND attempted_at>?", (request, now - 900)).fetchone():
            return False
        for scope, maximum in (("recovery:email:" + digest(email), 3), ("recovery:peer:" + digest(peer), 60)):
            count = db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=? AND attempted_at>?", (scope, now - 900)).fetchone()[0]
            if count >= maximum:
                raise StoreError(429, "too many recovery requests; try again later")
        db.executemany("INSERT INTO auth_attempts VALUES(?,?)", ((scope, now) for scope in
                       (request, "recovery:email:" + digest(email), "recovery:peer:" + digest(peer))))
        return True

    def request(self, email, peer, client_id, *, actor=None, organization=None, target=None):
        if self.mail.config is None:
            raise StoreError(409, "password recovery email is unavailable; contact your server administrator")
        from .mail import mailbox

        try:
            email = mailbox(email).lower()
        except ValueError as exc:
            raise StoreError(400, "enter a valid email address") from exc
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
            raise StoreError(400, "recovery requires a stable request identity")
        now, identity, credential = int(time.time()), _id(), token()
        with self.store.transaction(write=True) as db:
            if organization is not None:
                self.store._require(db, actor, organization, admin=True)
                member = db.execute("SELECT u.email FROM memberships m JOIN users u ON u.id=m.user_id "
                                    "WHERE m.organization_id=? AND m.user_id=? AND u.account_kind='human'",
                                    (organization, target)).fetchone()
                if member is None or member["email"] != email:
                    raise StoreError(404, "organization member unavailable")
            if not self._rate(db, email, peer, client_id, now):
                return ACKNOWLEDGEMENT
            user = db.execute("SELECT id,password_hash FROM users WHERE email=? AND account_kind='human'", (email,)).fetchone()
            previous = db.execute("SELECT 1 FROM account_mail WHERE user_id=? AND kind='reset' AND client_id=?",
                                  (user["id"] if user else "0" * 32, client_id)).fetchone()
            # Generate/encrypt the same payload for known and unknown addresses.
            # Never expose account existence, the recovery token, or delivery status.
            message = {"from": self.mail.config.sender, "to": [email], "subject": "Reset your Reasoning password",
                       "text": "Choose a new password by opening this private link:\n" + self.mail.public_url
                       + "/organization.html#reset=" + credential
                       + "\n\nThis link expires in 30 minutes and can be used once. If you did not request it, ignore this email. "
                       "Your password has not changed."}
            ciphertext = self.store._box.seal_mail(json.dumps(message), organization_id=user["id"] if user else "0" * 32,
                                                   invitation_id=identity)
            # Apply installation admission limits equally to unknown addresses.
            try:
                self.mail.check_limits(db, now)
            except StoreError as exc:
                if exc.status != 429 or organization is not None:
                    raise
                # A public quota error must not turn the shared mail allowance
                # into an oracle for which addresses caused an email to queue.
                return ACKNOWLEDGEMENT
            if user is not None and previous is None:
                db.execute("INSERT INTO account_mail(id,user_id,email,kind,client_id,token_hash,password_binding,organization_id,"
                           "requested_by,configuration_hash,ciphertext,state,created_at,expires_at) "
                           "VALUES(?,?,?,'reset',?,?,?,?,?,?,?,'queued',?,?)",
                           (identity, user["id"], email, client_id, digest(credential), self._binding(user["password_hash"]),
                            organization, actor, self.mail.binding, ciphertext, now, now + self.lifetime))
                self.store._audit(db, organization, actor, "account.recovery.request", user["id"])
        return ACKNOWLEDGEMENT

    def _challenge(self, db, credential):
        if not isinstance(credential, str) or not 16 <= len(credential) <= 512:
            raise StoreError(404, "This recovery link is expired, used, or invalid. Request a new link.")
        row = db.execute("SELECT m.*,u.password_hash FROM account_mail m JOIN users u ON u.id=m.user_id "
                         "AND u.email=m.email AND u.account_kind='human' WHERE m.kind='reset' AND m.token_hash=? "
                         "AND m.consumed_at IS NULL AND m.expires_at>?", (digest(credential), int(time.time()))).fetchone()
        if row is None or not hmac.compare_digest(row["password_binding"], self._binding(row["password_hash"])):
            raise StoreError(404, "This recovery link is expired, used, or invalid. Request a new link.")
        return row

    def reset(self, credential, password):
        with self.store.transaction() as db:
            self._challenge(db, credential)
        encoded = password_hash(password)
        with self.store.transaction(write=True) as db:
            row = self._challenge(db, credential)
            now = int(time.time())
            db.execute("UPDATE users SET password_hash=?,email_verified_at=? WHERE id=?", (encoded, now, row["user_id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
            invalidate_recovery(db, row["user_id"], now)
            self._notice(db, row["user_id"], row["email"], now)
            self._audit_account(db, row["user_id"], "account.password.recovered")
        return {"message": "Password changed. Sign in with your new password."}

    def change_password(self, user, current, password):
        with self.store.transaction() as db:
            row = _row(db.execute("SELECT email,password_hash FROM users WHERE id=? AND account_kind='human'", (user,)).fetchone())
        self.store._login_attempt(row["email"], "change-password:" + user)
        if not password_matches(current, row["password_hash"]):
            raise StoreError(403, "current password is incorrect")
        encoded = password_hash(password)
        with self.store.transaction(write=True) as db:
            updated = db.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?", (encoded, user, row["password_hash"]))
            if updated.rowcount != 1:
                raise StoreError(409, "password changed while this request was running; try again")
            # Reauthentication also replaces this browser's token, so a copied
            # current-session credential cannot survive a password change.
            db.execute("DELETE FROM sessions WHERE user_id=?", (user,))
            invalidate_recovery(db, user, int(time.time()))
            self._notice(db, user, row["email"], int(time.time()))
            self._audit_account(db, user, "account.password.change")
        return {"message": "Password changed. Sign in with your new password."}

    def sign_out_others(self, user, access):
        with self.store.transaction(write=True) as db:
            count = db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user, digest(access))).rowcount
            self._audit_account(db, user, "account.sessions.revoke")
        return {"revoked_sessions": count}

    def _audit_account(self, db, user, action):
        for org in db.execute("SELECT organization_id FROM memberships WHERE user_id=?", (user,)).fetchall():
            self.store._audit(db, org[0], user, action, user)

    def _notice(self, db, user, email, now):
        if self.mail.config is None:
            return
        try:
            self.mail.check_limits(db, now)
        except StoreError as exc:
            if exc.status != 429:
                raise
            # Preserve recovery and the operator's email cap. The missing
            # notification remains visible in the account audit history.
            self._audit_account(db, user, "account.password.notification_limited")
            return
        identity = _id()
        message = {"from": self.mail.config.sender, "to": [email], "subject": "Your Reasoning password changed",
                   "text": "Your Reasoning password was changed. If you did not make this change, "
                   "contact your server administrator immediately.\n"
                   + self.mail.public_url}
        ciphertext = self.store._box.seal_mail(json.dumps(message), organization_id=user, invitation_id=identity)
        db.execute("INSERT INTO account_mail(id,user_id,email,kind,client_id,configuration_hash,ciphertext,state,created_at,expires_at) "
                   "VALUES(?,?,?,'password_changed',?,?,?,'queued',?,?)",
                   (identity, user, email, identity, self.mail.binding, ciphertext, now, now + 86400))

    def claim(self, db):
        now = int(time.time())
        db.execute("UPDATE account_mail SET ciphertext=NULL,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END "
                   "WHERE expires_at<=? OR consumed_at IS NOT NULL", (now,))
        while row := db.execute("SELECT m.*,u.password_hash,u.email AS current_email,u.account_kind FROM account_mail m "
                               "JOIN users u ON u.id=m.user_id WHERE m.state='queued' ORDER BY m.created_at,m.id LIMIT 1").fetchone():
            valid = (row["configuration_hash"] == self.mail.binding and row["email"] == row["current_email"]
                     and row["account_kind"] == "human")
            if row["kind"] == "reset":
                valid = valid and hmac.compare_digest(row["password_binding"], self._binding(row["password_hash"]))
            if valid and row["organization_id"] is not None:
                try:
                    self.store._require(db, row["requested_by"], row["organization_id"], admin=True)
                    valid = db.execute("SELECT 1 FROM memberships WHERE organization_id=? AND user_id=?",
                                       (row["organization_id"], row["user_id"])).fetchone() is not None
                except StoreError:
                    valid = False
            if not valid:
                db.execute("UPDATE account_mail SET state='cancelled',ciphertext=NULL WHERE id=?", (row["id"],))
                continue
            message = json.loads(self.store._box.open_mail(row["ciphertext"], organization_id=row["user_id"], invitation_id=row["id"]))
            db.execute("UPDATE account_mail SET state='sending' WHERE id=?", (row["id"],))
            return {"id": row["id"], "kind": "account", "message": message}
        return None

    def finish(self, row, state, provider_id, error):
        with self.store.transaction(write=True) as db:
            db.execute("UPDATE account_mail SET state=?,provider_id=?,error_code=?,ciphertext=NULL WHERE id=? AND state='sending'",
                       (state, provider_id, error, row["id"]))

    def administration(self, actor, organization):
        from .mail import MAIL_TABLES

        with self.store.transaction() as db:
            self.store._require(db, actor, organization, admin=True)
            deliveries = [dict(row) for row in db.execute("SELECT user_id,state,created_at,error_code FROM account_mail "
                          "WHERE organization_id=? AND kind='reset' ORDER BY created_at DESC,id DESC LIMIT 100", (organization,))]
            events = [dict(row) for row in db.execute("SELECT a.sequence,a.actor_id,a.action,a.target_id,a.created_at,u.name AS actor_name "
                      "FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id WHERE a.organization_id=? "
                      "AND (a.action LIKE 'invitation.%' OR a.action LIKE 'membership.%' OR a.action LIKE 'account.%') "
                      "ORDER BY a.sequence DESC LIMIT 50", (organization,))]
            used = sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE created_at>?", (int(time.time()) - 86400,)).fetchone()[0]
                       for table in MAIL_TABLES)
        return {"members": self.store.members(actor, organization), "invitations": self.store.invitations(actor, organization),
                "mail": {**self.mail.configuration(actor, organization), "used_today": used}, "recoveries": deliveries, "events": events}
