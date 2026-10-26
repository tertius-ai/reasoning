"""Mailbox possession, durable delivery and tenant boundaries for recovery."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_public_invitation_mail import CONFIG, PASSWORD
from test_public_invitation_mail import installation as installation

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.backup import backup, create_key, restore
from reasoning_ledger.server.mail import MailConfig, MailStore
from reasoning_ledger.server.store import StoreError

NEW_PASSWORD = "replacement private account password"


def request_link(installation, client="recovery-request-00001"):
    store, owner, mail = installation
    result = mail.recovery.request("owner@example.org", "test-peer", client)
    row = mail.claim()
    credential = row["message"]["text"].split("#reset=", 1)[1].split("\n", 1)[0]
    return store, owner, mail, row, credential, result


def member(installation, email="member@example.org", role="member"):
    store, owner, _ = installation
    invite = store.invite(owner["user_id"], owner["organization_id"], email, role, 3600)
    return store.accept_invitation(invite["token"], user_id=None, name="Member", password=PASSWORD)


def test_link_is_private_single_use_and_requires_mailbox_possession(installation):
    store, owner, mail, row, credential, result = request_link(installation)
    assert "If an account exists" in result["message"] and credential not in json.dumps(result)
    with store.transaction() as db:
        saved = dict(db.execute("SELECT * FROM account_mail").fetchone())
        assert saved["token_hash"] != credential and credential.encode() not in saved["ciphertext"]
    old_session = store.login("owner@example.org", PASSWORD, "first-browser")
    mail.finish(row, "accepted", provider_id="a" * 32)
    assert "Sign in" in mail.recovery.reset(credential, NEW_PASSWORD)["message"]
    with pytest.raises(StoreError):
        store.session(old_session["access_token"])
    assert store.login("owner@example.org", NEW_PASSWORD, "second-browser")
    assert store.me(owner["user_id"])["email_verified_at"] is not None
    with pytest.raises(StoreError, match="expired, used, or invalid"):
        mail.recovery.reset(credential, PASSWORD)
    notice = mail.claim()
    assert notice["message"]["subject"] == "Your Reasoning password changed"
    assert PASSWORD not in notice["message"]["text"] and NEW_PASSWORD not in notice["message"]["text"]


def test_known_unknown_and_duplicate_requests_have_identical_acknowledgements(installation):
    store, _, mail = installation
    known = mail.recovery.request("OWNER@example.org", "one-peer", "recovery-request-00001")
    unknown = mail.recovery.request("absent@example.org", "one-peer", "recovery-request-00002")
    duplicate = mail.recovery.request("owner@example.org", "one-peer", "recovery-request-00001")
    assert known == unknown == duplicate
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM account_mail").fetchone()[0] == 1


def test_concurrent_retry_does_not_create_duplicate_mail(installation):
    store, _, mail = installation
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: mail.recovery.request("owner@example.org", "peer", "recovery-request-00001"), range(2)))
    assert replies[0] == replies[1]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM account_mail").fetchone()[0] == 1


@pytest.mark.parametrize("email", ["owner@example.org", "absent@example.org"])
def test_recovery_request_limits_are_independent_of_account_existence(installation, email):
    _, _, mail = installation
    for i in range(3):
        mail.recovery.request(email, "peer", "recovery-request-0000" + str(i))
    with pytest.raises(StoreError) as error:
        mail.recovery.request(email, "peer", "recovery-request-00004")
    assert error.value.status == 429


def test_shared_network_allows_thirty_researchers_and_bounds_unknown_address_probes(installation):
    _, _, mail = installation
    for i in range(60):
        mail.recovery.request("researcher" + str(i) + "@example.org", "one-network", "recovery-request-" + str(i).zfill(8))
    with pytest.raises(StoreError) as error:
        mail.recovery.request("next@example.org", "one-network", "recovery-request-00000061")
    assert error.value.status == 429


def test_old_links_survive_resend_until_one_is_used(installation):
    store, _, mail, first, credential, _ = request_link(installation)
    mail.finish(first, "unknown", error="provider_uncertain")
    mail.recovery.request("owner@example.org", "peer", "recovery-request-00002")
    second = mail.claim()
    other = second["message"]["text"].split("#reset=", 1)[1].split("\n", 1)[0]
    mail.recovery.reset(credential, NEW_PASSWORD)
    with pytest.raises(StoreError):
        mail.recovery.reset(other, PASSWORD)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM account_mail WHERE kind='reset' AND consumed_at IS NULL").fetchone()[0] == 0


@pytest.mark.parametrize("invalidation", ["expired", "host-password-change", "email-change"])
def test_expired_or_stale_link_cannot_change_password(installation, invalidation):
    store, owner, mail, _, credential, _ = request_link(installation)
    if invalidation == "host-password-change":
        store.reset_password("owner@example.org", NEW_PASSWORD)
    else:
        with store.transaction(write=True) as db:
            if invalidation == "expired":
                db.execute("UPDATE account_mail SET expires_at=1")
            else:
                db.execute("UPDATE users SET email='changed@example.org' WHERE id=?", (owner["user_id"],))
    with pytest.raises(StoreError):
        mail.recovery.reset(credential, "an attacker replacement password")


def test_recovery_delivery_does_not_retry_uncertain_sends_or_cross_configuration(installation):
    store, _, mail, row, _, _ = request_link(installation)
    mail.recover()
    assert mail.claim() is None
    with store.transaction() as db:
        saved = dict(db.execute("SELECT state,ciphertext,error_code FROM account_mail").fetchone())
        assert saved == {"state": "unknown", "ciphertext": None, "error_code": "worker_interrupted"}
    mail.recovery.request("owner@example.org", "peer", "recovery-request-00002")
    changed = MailStore(store, CONFIG, "https://other.example.org")
    assert changed.claim() is None


def test_password_change_requires_current_password_and_revokes_every_session(installation):
    store, owner, mail = installation
    unrelated = member(installation)
    current = store.login("owner@example.org", PASSWORD, "first-browser")
    other = store.login("owner@example.org", PASSWORD, "second-browser")
    with pytest.raises(StoreError, match="current password"):
        mail.recovery.change_password(owner["user_id"], "wrong password", NEW_PASSWORD)
    mail.recovery.change_password(owner["user_id"], PASSWORD, NEW_PASSWORD)
    with pytest.raises(StoreError):
        store.session(current["access_token"])
    with pytest.raises(StoreError):
        store.session(other["access_token"])
    assert store.session(unrelated["access_token"])
    assert store.login("owner@example.org", NEW_PASSWORD, "third-browser")


def test_password_change_http_clears_cookies_and_requires_sign_in(installation):
    store, _, _ = installation
    app = create_app(Config(store.data_dir, "https://chat.example.org", mail=CONFIG, inference_enabled=False))
    client = TestClient(app, base_url="https://chat.example.org")
    session = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).json()
    headers = {"Origin": "https://chat.example.org", "X-CSRF-Token": session["csrf_token"]}
    body = {"current_password": PASSWORD, "password": NEW_PASSWORD}
    assert client.post("/api/auth/password", json=body).status_code == 403
    assert client.get("/api/auth/me").status_code == 200
    result = client.post("/api/auth/password", json=body, headers=headers)
    assert result.status_code == 200 and "access_token" not in result.json()
    assert "Sign in" in result.json()["message"]
    assert client.cookies.get("__Host-reasoning_session") is None
    assert client.cookies.get("__Host-reasoning_csrf") is None
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "owner@example.org", "password": NEW_PASSWORD}).status_code == 200


def test_sign_out_others_preserves_current_and_unrelated_accounts(installation):
    store, owner, mail = installation
    other_user = member(installation)
    current = store.login("owner@example.org", PASSWORD, "one")
    old = store.login("owner@example.org", PASSWORD, "two")
    result = mail.recovery.sign_out_others(owner["user_id"], current["access_token"])
    assert result["revoked_sessions"] >= 1 and store.session(current["access_token"])
    assert store.session(other_user["access_token"])
    with pytest.raises(StoreError):
        store.session(old["access_token"])


def test_administration_is_scoped_and_never_returns_reset_credentials(installation):
    store, owner, mail = installation
    user = member(installation)
    org = owner["organization_id"]
    mail.recovery.request("member@example.org", "admin", "recovery-request-00001",
                          actor=owner["user_id"], organization=org, target=user["user_id"])
    result = mail.recovery.administration(owner["user_id"], org)
    assert result["recoveries"][0]["state"] == "queued"
    assert {"password_hash", "token_hash", "ciphertext"}.isdisjoint(result["recoveries"][0])
    with pytest.raises(StoreError) as error:
        mail.recovery.administration(user["user_id"], org)
    assert error.value.status == 403
    with pytest.raises(StoreError):
        mail.recovery.request("member@example.org", "peer", "recovery-request-00002",
                              actor=owner["user_id"], organization=org, target="f" * 32)
    store.change_member(owner["user_id"], org, user["user_id"], None)
    assert mail.claim() is None  # Authority is rechecked before email delivery.


def test_invitation_inspection_does_not_consume_or_reveal_retired_recipient(installation):
    store, owner, _ = installation
    invite = store.invite(owner["user_id"], owner["organization_id"], "member@example.org", "member", 3600)
    assert store.invitation_status(invite["token"]) == {"state": "pending", "email": "member@example.org", "existing_account": False}
    store.revoke_invitation(owner["user_id"], owner["organization_id"], invite["id"])
    assert store.invitation_status(invite["token"]) == {"state": "revoked"}
    assert store.invitation_status("not-a-real-invitation") == {"state": "invalid"}


def test_recovery_http_does_not_create_a_session_and_clears_old_cookies(installation):
    store, owner, mail, _, credential, _ = request_link(installation)
    app = create_app(Config(store.data_dir, "https://chat.example.org", mail=CONFIG, inference_enabled=False))
    client = TestClient(app, base_url="https://chat.example.org")
    before = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).json()
    assert before["user_id"] == owner["user_id"]
    result = client.post("/api/auth/password-reset/confirm", json={"token": credential, "password": NEW_PASSWORD})
    assert result.status_code == 200 and "access_token" not in result.json()
    assert client.get("/api/auth/me").status_code == 401
    replay = client.post("/api/auth/password-reset/confirm", json={"token": credential, "password": PASSWORD})
    assert replay.status_code == 404
    unknown = client.post("/api/auth/password-reset", json={"email": "absent@example.org", "client_id": "unknown-request-0001"})
    known = client.post("/api/auth/password-reset", json={"email": "owner@example.org", "client_id": "existing-request-0001"})
    assert unknown.status_code == known.status_code == 202 and unknown.json() == known.json()


def test_simultaneous_redemptions_change_password_once(installation):
    _, _, mail, _, credential, _ = request_link(installation)

    def redeem(_):
        try:
            mail.recovery.reset(credential, NEW_PASSWORD)
            return "changed"
        except StoreError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, range(2)))
    assert results.count("changed") == 1 and results.count(404) == 1


def test_email_cap_does_not_block_recovery_or_disclose_account_existence(installation):
    store, _, _ = installation
    mail = MailStore(store, MailConfig(CONFIG.sender, CONFIG.api_key, daily_limit=1), "https://chat.example.org")
    mail.recovery.request("owner@example.org", "peer", "recovery-request-00001")
    row = mail.claim()
    credential = row["message"]["text"].split("#reset=", 1)[1].split("\n", 1)[0]
    # An old retry and an unknown address behave alike after the short rate
    # receipt expires; neither response reveals whether an account exists.
    with store.transaction(write=True) as db:
        db.execute("UPDATE auth_attempts SET attempted_at=attempted_at-901 WHERE scope LIKE 'recovery:%'")
    replies = [mail.recovery.request(email, "peer", client_id) for email, client_id in
               (("owner@example.org", "recovery-request-00001"), ("absent@example.org", "recovery-request-00002"))]
    assert replies[0] == replies[1] and "If an account exists" in replies[0]["message"]
    mail.recovery.reset(credential, NEW_PASSWORD)
    assert store.login("owner@example.org", NEW_PASSWORD, "browser")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM account_mail").fetchone()[0] == 1
        assert db.execute("SELECT 1 FROM audit_events WHERE action='account.password.notification_limited'").fetchone()


def test_invitation_checks_do_not_use_sign_in_quota(installation):
    store, _, _ = installation
    for i in range(65):
        store.invitation_status("an-invalid-invitation-token-" + str(i), peer="one-shared-network")
    assert store.login("owner@example.org", PASSWORD, "one-shared-network")


@pytest.mark.parametrize("has_csrf", [True, False])
def test_revoked_cookie_does_not_trap_new_invitation_but_csrf_and_bearer_still_fail(installation, has_csrf):
    store, owner, _ = installation
    app = create_app(Config(store.data_dir, "https://chat.example.org", inference_enabled=False))
    client = TestClient(app, base_url="https://chat.example.org")
    login = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).json()
    access = client.cookies.get("__Host-reasoning_session")
    invite = store.invite(owner["user_id"], owner["organization_id"], "fresh@example.org", "member", 3600)
    body = {"token": invite["token"], "name": "Fresh", "password": PASSWORD}
    assert client.post("/api/invitations/accept", json=body,
                       headers={"Origin": "https://chat.example.org", "X-CSRF-Token": "incorrect"}).status_code == 403
    assert client.post("/api/invitations/accept", json=body, headers={"Authorization": "Bearer invalid"}).status_code == 401
    store.logout(access)
    result = client.post("/api/invitations/accept", json=body,
                         headers={"Origin": "https://chat.example.org", **({"X-CSRF-Token": login["csrf_token"]} if has_csrf else {})})
    assert result.status_code == 200 and result.json()["user_id"] != owner["user_id"]


def test_restored_backup_invalidates_recovery_links_and_does_not_replay_mail(installation, tmp_path):
    store, _, mail, _, credential, _ = request_link(installation)
    key, archive = tmp_path / "backup.key", tmp_path / "backup.enc"
    create_key(key)
    result = backup(store.data_dir, archive, key)
    destination = tmp_path / "restored"
    restore(archive, key, result["sha256"], destination, source_stopped=True)
    from reasoning_ledger.server.store import Store

    restored = Store(destination)
    restored_mail = MailStore(restored, CONFIG, mail.public_url)
    assert restored_mail.claim() is None
    with pytest.raises(StoreError):
        restored_mail.recovery.reset(credential, NEW_PASSWORD)
    with restored.transaction() as db:
        row = db.execute("SELECT state,ciphertext,consumed_at FROM account_mail").fetchone()
        assert row["state"] == "unknown" and row["ciphertext"] is None and row["consumed_at"] is not None


def test_http_admin_and_session_controls_keep_account_and_organization_boundaries(installation):
    store, owner, _ = installation
    user = member(installation)
    app = create_app(Config(store.data_dir, "https://chat.example.org", mail=CONFIG, inference_enabled=False))
    owner_client = TestClient(app, base_url="https://chat.example.org")
    session = owner_client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).json()
    root = "/api/organizations/" + owner["organization_id"]
    path = root + "/members/" + user["user_id"] + "/password-reset"
    headers = {"Origin": "https://chat.example.org", "X-CSRF-Token": session["csrf_token"]}
    body = {"client_id": "admin-recovery-request-0001"}
    assert owner_client.post(path, json=body).status_code == 403
    assert owner_client.post(path, json=body, headers=headers).status_code == 202
    metadata = owner_client.get(root + "/user-administration").json()
    assert metadata["recoveries"][0]["user_id"] == user["user_id"]
    assert "token_hash" not in json.dumps(metadata) and "ciphertext" not in json.dumps(metadata)
    member_client = TestClient(app, base_url="https://chat.example.org")
    login = member_client.post("/api/auth/login", json={"email": "member@example.org", "password": PASSWORD}).json()
    assert member_client.get(root + "/user-administration").status_code == 403
    member_headers = {"Origin": "https://chat.example.org", "X-CSRF-Token": login["csrf_token"]}
    assert member_client.post(path, json=body, headers=member_headers).status_code == 403
    assert member_client.post("/api/auth/sessions/revoke-others", json={}, headers=member_headers).status_code == 200
    assert owner_client.get("/api/auth/me").status_code == 200


def test_missing_email_and_invalid_input_have_clear_safe_responses(installation):
    store, _, _ = installation
    app = create_app(Config(store.data_dir, "https://chat.example.org", inference_enabled=False))
    client = TestClient(app, base_url="https://chat.example.org")
    assert client.get("/api/auth/password-reset").json()["enabled"] is False
    assert client.post("/api/auth/password-reset",
                       json={"email": "owner@example.org", "client_id": "recovery-request-00001"}).status_code == 409
    result = client.post("/api/auth/password-reset/confirm", json={"token": "private-token", "password": "private-value"})
    assert result.status_code == 422 and "private-token" not in result.text and "private-value" not in result.text
