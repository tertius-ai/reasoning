"""Public installation boundaries: durable tenancy, credentials, and invitations."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.cli import main
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.security import SecretBox, digest, password_hash, password_matches, private_file
from reasoning_ledger.server.store import Store, StoreError

PUBLIC_URL = "https://chat.example.org"
PASSWORD = "a very long test password"


@pytest.fixture
def installation(tmp_path):
    config = Config(tmp_path / "state", PUBLIC_URL)
    app = create_app(config)
    client = TestClient(app, base_url=PUBLIC_URL)
    store = app.state.store
    result = client.post(
        "/api/setup",
        json={
            "setup_token": (config.data_dir / "setup-token").read_text(),
            "email": "owner@example.org",
            "name": "Owner",
            "password": PASSWORD,
            "organization_name": "Mathematics",
            "transport": "bearer",
        },
    )
    assert result.status_code == 201, result.text
    owner = result.json()
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    return config, store, client, owner


def structure(client, org):
    root = f"/api/organizations/{org}"
    project = client.post(root + "/projects", json={"name": "Algebra"})
    assert project.status_code == 201, project.text
    channel = client.post(root + "/channels", json={"name": "research", "project_id": project.json()["id"]})
    assert channel.status_code == 201, channel.text
    topics = []
    for kind in ("alpha", "beta", "gamma"):
        result = client.post(root + "/topics", json={"name": kind, "kind": kind, "channel_id": channel.json()["id"]})
        assert result.status_code == 201, result.text
        topics.append(result.json())
    return project.json(), channel.json(), topics


def invited(installation, *, role="member", email="member@example.org"):
    config, _, client, owner = installation
    invitation = client.post(f"/api/organizations/{owner['organization_id']}/invitations", json={"email": email, "role": role})
    assert invitation.status_code == 201, invitation.text
    member_client = TestClient(create_app(config), base_url=PUBLIC_URL)
    result = member_client.post(
        "/api/invitations/accept",
        json={
            "token": invitation.json()["token"],
            "name": "Member",
            "password": PASSWORD,
            "transport": "bearer",
        },
    )
    assert result.status_code == 200, result.text
    member = result.json()
    member_client.headers["Authorization"] = "Bearer " + member["access_token"]
    return invitation.json(), member_client, member


def test_durable_hierarchy_messages_and_session_survive_restart(installation):
    config, _, client, owner = installation
    project, channel, topics = structure(client, owner["organization_id"])
    message_path = f"/api/organizations/{owner['organization_id']}/topics/{topics[0]['id']}/messages"
    message = client.post(message_path, json={"content": "Let us compute!", "client_id": "message-1"})
    assert message.status_code == 201
    restarted = TestClient(create_app(config), base_url=PUBLIC_URL, headers=dict(client.headers))
    assert restarted.get(message_path).json() == [message.json()]
    hierarchy = restarted.get(f"/api/organizations/{owner['organization_id']}/hierarchy").json()
    assert {key: hierarchy[key] for key in ("projects", "channels", "topics")} == {
        "projects": [project],
        "channels": [channel],
        "topics": sorted(topics, key=lambda row: (row["created_at"], row["id"])),
    }
    assert restarted.get("/api/auth/me").json()["email"] == "owner@example.org"
    assert "password_hash" not in restarted.get("/api/auth/me").text


def test_beta_topics_keep_model_selection_independent_of_legacy_catalog_hints(installation):
    _, _, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    project = client.post(root + "/projects", json={"name": "Algebra"}).json()
    channel = client.post(root + "/channels", json={"name": "research", "project_id": project["id"]}).json()
    client.put(root + "/policy", json={"allowed_models": ["test/one", "test/two"], "monthly_budget_microusd": 1_000_000}).raise_for_status()
    created = client.post(root + "/topics", json={
        "name": "Models", "kind": "beta", "channel_id": channel["id"], "default_model": "test/two",
    })
    assert created.status_code == 201 and created.json()["default_model"] == "test/two"
    assert client.post(root + "/topics", json={
        "name": "New router", "kind": "beta", "channel_id": channel["id"], "default_model": "openrouter/auto",
    }).status_code == 201
    assert client.post(root + "/topics", json={
        "name": "Human", "kind": "alpha", "channel_id": channel["id"], "default_model": "test/one",
    }).status_code == 400
    client.put(root + "/policy", json={"allowed_models": ["test/one"], "monthly_budget_microusd": 1_000_000}).raise_for_status()
    topic = next(item for item in client.get(root + "/hierarchy").json()["topics"] if item["id"] == created.json()["id"])
    assert topic["default_model"] == "test/two"


def test_bootstrap_invalid_secret_and_one_time_guard(installation):
    config, _, client, _ = installation
    body = {"setup_token": "wrong", "email": "other@example.org", "name": "Other", "password": PASSWORD, "organization_name": "Other"}
    assert client.post("/api/setup", json=body).status_code == 403
    body["setup_token"] = (config.data_dir / "setup-token").read_text()
    assert client.post("/api/setup", json=body).status_code == 409


def test_bootstrap_and_migrations_are_atomic_under_concurrent_initialization(tmp_path):
    path = tmp_path / "state"
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: Store(path), range(4)))
    credential = (path / "setup-token").read_text()

    def bootstrap(store):
        try:
            return store.bootstrap(credential, "owner@example.org", "Owner", PASSWORD, "Organization")
        except StoreError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bootstrap, stores))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert results.count(409) == 3
    with stores[0].transaction() as db:
        assert db.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_tenant_scope_for_all_objects_and_foreign_keys(installation):
    _, store, owner_client, owner = installation
    org = owner["organization_id"]
    project, channel, topics = structure(owner_client, org)
    _, other, other_account = invited(installation, role="admin")
    store.change_member(owner["user_id"], org, other_account["user_id"], "owner")
    other_org = other.post("/api/organizations", json={"name": "Other organization", "credential_mode": "none"}).json()["id"]
    other_structure = structure(other, other_org)
    other_topic = other_structure[2][0]["id"]
    foreign = f"/api/organizations/{other_org}"
    for suffix in ("/hierarchy", "/members", "/invitations", "/provider-keys", "/policy", "/audit"):
        assert owner_client.get(foreign + suffix).status_code == 404
    assert owner_client.get(foreign + f"/topics/{other_topic}/messages").status_code == 404
    assert owner_client.post(foreign + "/projects", json={"name": "Intruder"}).status_code == 404
    result = owner_client.post(f"/api/organizations/{org}/channels", json={"name": "wrong", "project_id": other_structure[0]["id"]})
    assert result.status_code == 404
    result = owner_client.post(
        f"/api/organizations/{org}/topics", json={"name": "wrong", "kind": "alpha", "channel_id": other_structure[1]["id"]}
    )
    assert result.status_code == 404
    assert owner_client.get(f"/api/organizations/{org}/topics/{other_topic}/messages").status_code == 404
    # Even a storage-layer programming error cannot join parents from another tenant.
    with pytest.raises(sqlite3.IntegrityError), store.transaction(write=True) as db:
        db.execute("INSERT INTO channels(id,organization_id,project_id,name,created_at) VALUES(?,?,?,?,?)",
                   ("bad", other_org, project["id"], "bad", 0))
    with pytest.raises(sqlite3.IntegrityError), store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO topics(id,organization_id,channel_id,name,kind,created_at,default_model) VALUES(?,?,?,?,?,?,?)",
            ("bad", other_org, channel["id"], "bad", "alpha", 0, None),
        )
    with pytest.raises(sqlite3.IntegrityError), store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO messages(id,organization_id,topic_id,author_id,content,client_id,created_at) VALUES(?,?,?,?,?,?,?)",
            ("bad", other_org, topics[0]["id"], owner["user_id"], "bad", "bad", 0),
        )


def test_viewer_can_read_but_not_write_or_manage_keys(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    _, viewer, _ = invited(installation, role="viewer")
    root = f"/api/organizations/{org}"
    assert viewer.get(root + "/hierarchy").status_code == 200
    assert viewer.get(root + f"/topics/{topics[0]['id']}/messages").status_code == 200
    assert viewer.post(root + "/projects", json={"name": "Not allowed"}).status_code == 403
    assert viewer.post(root + f"/topics/{topics[0]['id']}/messages", json={"content": "No", "client_id": "no"}).status_code == 403
    assert viewer.get(root + "/provider-keys").status_code == 403
    assert viewer.post(root + "/invitations", json={"email": "another@example.org"}).status_code == 403


def test_member_can_post_but_not_administer(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    _, member, _ = invited(installation)
    root = f"/api/organizations/{org}"
    assert member.post(root + f"/topics/{topics[0]['id']}/messages", json={"content": "Hello", "client_id": "hello"}).status_code == 201
    for suffix in ("/invitations", "/provider-keys", "/policy", "/audit"):
        assert member.get(root + suffix).status_code == 403


def test_private_projects_enforce_roster_and_delegate_ownership(installation):
    _, _, administrator, owner = installation
    org = owner["organization_id"]
    _, project_owner, project_owner_user = invited(installation, email="project-owner@example.org")
    _, outsider, outsider_user = invited(installation, email="outsider@example.org")
    root = f"/api/organizations/{org}"
    created = administrator.post(root + "/projects", json={
        "name": "Private research", "visibility": "private",
        "owner_id": project_owner_user["user_id"], "member_ids": [project_owner_user["user_id"]],
    })
    assert created.status_code == 201, created.text
    project = created.json()
    channel = administrator.post(root + "/channels", json={"name": "internal", "project_id": project["id"]}).json()
    topic = administrator.post(root + "/topics", json={"name": "results", "kind": "alpha", "channel_id": channel["id"]}).json()
    gamma = administrator.post(root + "/topics", json={"name": "agents", "kind": "gamma", "channel_id": channel["id"]}).json()
    administrator.post(
        root + f"/topics/{topic['id']}/messages", json={"content": "private theorem", "client_id": "private-message"}
    ).raise_for_status()
    knowledge = administrator.post(root + "/knowledge", json={
        "client_id": "private-knowledge-001", "kind": "ledger", "title": "Hidden result", "body": "Private proof",
        "state": "note", "scope_kind": "project", "scope_id": project["id"],
    }).json()
    workflow = administrator.post(root + f"/topics/{gamma['id']}/workflows", json={
        "client_id": "private-workflow-001", "definition": {"format": 1, "name": "Private review", "nodes": [{
            "id": "review", "kind": "approval", "depends_on": [], "inputs": {},
            "parameters": {"message": "Review", "required_role": "member"},
        }]},
    }).json()

    owner_project = project_owner.get(root + "/hierarchy").json()["projects"]
    assert [item["id"] for item in owner_project] == [project["id"]] and owner_project[0]["project_role"] == "owner"
    assert outsider.get(root + "/hierarchy").json()["projects"] == []
    assert outsider.get(root + f"/topics/{topic['id']}/messages").status_code == 404
    assert outsider.get(root + "/search", params={"q": "private theorem"}).json() == []
    assert outsider.get(root + "/knowledge").json() == []
    assert outsider.get(root + f"/knowledge/{knowledge['id']}").status_code == 404
    assert outsider.get(root + f"/workflows/{workflow['workflow_id']}").status_code == 404

    member_path = root + f"/projects/{project['id']}/members/{outsider_user['user_id']}"
    assert project_owner.put(member_path, json={"role": "member"}).status_code == 204
    assert outsider.get(root + f"/topics/{topic['id']}/messages").status_code == 200
    assert outsider.get(root + f"/knowledge/{knowledge['id']}").status_code == 200
    assert outsider.get(root + f"/workflows/{workflow['workflow_id']}").status_code == 200
    assert outsider.patch(root + f"/projects/{project['id']}", json={"name": "No"}).status_code == 403
    assert project_owner.patch(root + f"/channels/{channel['id']}", json={"name": "renamed"}).status_code == 200
    assert project_owner.delete(root + f"/projects/{project['id']}/members/{project_owner_user['user_id']}").status_code == 409


def test_last_owner_and_admin_privilege_boundaries(installation):
    _, _, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    path = root + "/members/" + owner["user_id"]
    assert client.delete(path).status_code == 409
    assert client.patch(path, json={"role": "member"}).status_code == 409
    _, admin, admin_user = invited(installation, role="admin")
    assert admin.delete(path).status_code == 403
    assert admin.patch(root + "/members/" + admin_user["user_id"], json={"role": "owner"}).status_code == 403
    assert admin.post(root + "/invitations", json={"email": "next@example.org", "role": "admin"}).status_code == 403
    assert client.patch(root + "/members/" + admin_user["user_id"], json={"role": "owner"}).status_code == 204
    assert client.delete(path).status_code == 204
    assert client.get(root + "/hierarchy").status_code == 404


def test_invitation_single_use_and_no_token_in_listing_or_database(installation):
    config, store, client, owner = installation
    invitation, member, _ = invited(installation)
    listed = client.get(f"/api/organizations/{owner['organization_id']}/invitations")
    assert "token" not in listed.text
    assert member.post("/api/invitations/accept", json={"token": invitation["token"]}).status_code == 404
    with store.transaction() as db:
        stored = dict(db.execute("SELECT * FROM invitations").fetchone())
    assert stored["token_hash"] != invitation["token"]
    for path in config.data_dir.glob("server.sqlite3*"):
        assert invitation["token"].encode() not in path.read_bytes()


@pytest.mark.parametrize("expired", [True, False])
def test_expired_or_revoked_invitations_are_refused(installation, expired):
    config, store, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    invitation = client.post(root + "/invitations", json={"email": "member@example.org"}).json()
    if expired:
        with store.transaction(write=True) as db:
            db.execute("UPDATE invitations SET expires_at=0 WHERE id=?", (invitation["id"],))
    else:
        assert client.delete(root + "/invitations/" + invitation["id"]).status_code == 204
    anonymous = TestClient(create_app(config), base_url=PUBLIC_URL)
    result = anonymous.post("/api/invitations/accept", json={"token": invitation["token"], "name": "Member", "password": PASSWORD})
    assert result.status_code == 404


def test_existing_account_invitation_cannot_reset_password_or_downgrade_owner(installation):
    config, _, client, owner = installation
    invitation = client.post(
        f"/api/organizations/{owner['organization_id']}/invitations", json={"email": "OWNER@example.org", "role": "viewer"}
    ).json()
    anonymous = TestClient(create_app(config), base_url=PUBLIC_URL)
    result = anonymous.post(
        "/api/invitations/accept", json={"token": invitation["token"], "name": "Intruder", "password": "an attacker chosen password"}
    )
    assert result.status_code == 409
    assert client.post("/api/invitations/accept", json={"token": invitation["token"]}).status_code == 200
    assert client.get("/api/organizations").json()[0]["role"] == "owner"
    assert anonymous.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).status_code == 200


def test_invitation_requires_the_correct_signed_in_account(installation):
    _, _, client, owner = installation
    _, other, _ = invited(installation)
    invitation = client.post(f"/api/organizations/{owner['organization_id']}/invitations", json={"email": "third@example.org"}).json()
    assert other.post("/api/invitations/accept", json={"token": invitation["token"]}).status_code == 403


def test_membership_removal_immediately_denies_existing_sessions(installation):
    _, _, client, owner = installation
    _, member, user = invited(installation)
    root = f"/api/organizations/{owner['organization_id']}"
    assert member.get(root + "/hierarchy").status_code == 200
    assert client.delete(root + "/members/" + user["user_id"]).status_code == 204
    assert member.get(root + "/hierarchy").status_code == 404
    assert member.get("/api/auth/me").status_code == 200


def test_message_idempotence_payload_conflicts_and_pagination(installation):
    _, _, client, owner = installation
    _, _, topics = structure(client, owner["organization_id"])
    root = f"/api/organizations/{owner['organization_id']}/topics/"
    path = root + topics[0]["id"] + "/messages"
    body = {"content": "First", "client_id": "same-id"}
    first = client.post(path, json=body).json()
    assert client.post(path, json=body).json() == first
    assert client.post(path, json={**body, "content": "Changed"}).status_code == 409
    assert client.post(root + topics[1]["id"] + "/messages", json=body).status_code == 409
    second = client.post(path, json={"content": "Second", "client_id": "second-id"}).json()
    assert client.get(path, params={"limit": 1}).json() == [first]
    assert client.get(path, params={"after": first["sequence"]}).json() == [second]
    assert client.get(path, params={"limit": 10000}).status_code == 422


def test_cookie_auth_origin_csrf_logout_and_secure_cookie_flags(installation):
    config, _, _, _ = installation
    client = TestClient(create_app(config), base_url=PUBLIC_URL)
    result = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD})
    assert result.status_code == 200
    assert "access_token" not in result.json()
    cookies = result.headers.get_list("set-cookie")
    assert all("Secure" in cookie and "SameSite=strict" in cookie for cookie in cookies)
    assert all(cookie.startswith("__Host-") and "Path=/" in cookie and "Domain=" not in cookie for cookie in cookies)
    assert "HttpOnly" in cookies[0]
    assert "HttpOnly" not in cookies[1]
    assert client.get("/api/auth/me").status_code == 200
    body = {"name": "Another", "credential_mode": "none"}
    assert client.post("/api/organizations", json=body).status_code == 403
    client.headers["Origin"] = PUBLIC_URL
    assert client.post("/api/organizations", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.post("/api/organizations", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    assert client.post("/api/organizations", json=body).status_code == 201
    client.headers["Origin"] = "https://evil.example.org"
    assert client.post("/api/organizations", json=body).status_code == 403
    client.headers["Origin"] = PUBLIC_URL
    assert client.post("/api/auth/logout", json={}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_session_expiry_bearer_logout_and_no_bad_authorization_fallback(installation):
    _, store, client, owner = installation
    assert client.get("/api/auth/me", headers={"Authorization": "Basic bad"}).status_code == 401
    assert client.post("/api/auth/logout", json={}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    login = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD, "transport": "bearer"}).json()
    assert login["access_token"] != owner["access_token"]
    client.headers["Authorization"] = "Bearer " + login["access_token"]
    with store.transaction(write=True) as db:
        db.execute("UPDATE sessions SET expires_at=0")
    assert client.get("/api/auth/me").status_code == 401


def test_login_rate_limit_persists_across_restart_and_rejects_forwarded_spoofing(installation):
    config, _, client, _ = installation
    for _ in range(10):
        result = client.post(
            "/api/auth/login", json={"email": "nobody@example.org", "password": "wrong"}, headers={"X-Forwarded-For": "different"}
        )
        assert result.status_code == 401
        assert result.json() == {"detail": "invalid email or password"}
    restarted = TestClient(create_app(config), base_url=PUBLIC_URL)
    assert restarted.post("/api/auth/login", json={"email": "NOBODY@example.org", "password": "wrong"}).status_code == 429


def test_provider_keys_encrypted_scoped_disabled_and_never_returned(installation):
    config, store, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    secret = "sk-or-v1-extremely-secret-provider-credential"
    added = client.post(root + "/provider-keys", json={"label": "Research", "key": secret})
    assert added.status_code == 201, added.text
    key = added.json()
    assert secret not in added.text
    assert "ciphertext" not in added.text
    assert client.get(root + "/provider-keys").json() == [key]
    with store.transaction() as db:
        stored = dict(db.execute("SELECT * FROM provider_keys").fetchone())
        session = dict(db.execute("SELECT * FROM sessions").fetchone())
    box = SecretBox((config.data_dir / "encryption.key").read_bytes())
    assert box.open(stored["ciphertext"], organization_id=owner["organization_id"], provider="openrouter", key_id=key["id"]) == secret
    for changes in ({"organization_id": "different"}, {"provider": "different"}, {"key_id": "different"}):
        scope = {"organization_id": owner["organization_id"], "provider": "openrouter", "key_id": key["id"], **changes}
        with pytest.raises(InvalidTag):
            box.open(stored["ciphertext"], **scope)
    assert session["token_hash"] != owner["access_token"]
    for path in config.data_dir.glob("server.sqlite3*"):
        payload = path.read_bytes()
        assert secret.encode() not in payload
        assert PASSWORD.encode() not in payload
        assert owner["access_token"].encode() not in payload
        assert path.stat().st_mode & 0o077 == 0
    assert client.delete(root + "/provider-keys/" + key["id"]).status_code == 204
    assert client.get(root + "/provider-keys").json()[0]["disabled_at"] is not None
    audit = client.get(root + "/audit").text
    assert secret not in audit
    assert "provider-key.disable" in audit


def test_new_organization_requires_an_explicit_isolated_credential_choice(installation):
    _, store, client, owner = installation
    source_root = f"/api/organizations/{owner['organization_id']}"
    source_key = client.post(source_root + "/provider-keys", json={"label": "Source", "key": "separate upstream secret"}).json()
    source_policy = {"allowed_models": ["provider/model"], "monthly_budget_microusd": 25_000_000}
    assert client.put(source_root + "/policy", json=source_policy).status_code == 200
    assert client.post("/api/organizations", json={"name": "Incomplete"}).status_code == 422

    reused = client.post("/api/organizations", json={
        "name": "Reused", "credential_mode": "reuse", "source_organization_id": owner["organization_id"],
        "source_key_id": source_key["id"],
    })
    assert reused.status_code == 201, reused.text
    reused_root = f"/api/organizations/{reused.json()['id']}"
    copied = client.get(reused_root + "/provider-keys").json()
    assert len(copied) == 1 and copied[0]["fingerprint"] == source_key["fingerprint"] and copied[0]["id"] != source_key["id"]
    assert client.get(reused_root + "/policy").json() == source_policy
    with store.transaction() as db:
        ciphertexts = db.execute(
            "SELECT ciphertext FROM provider_keys WHERE id IN (?,?) ORDER BY id", (source_key["id"], copied[0]["id"])
        ).fetchall()
        assert len(ciphertexts) == 2 and ciphertexts[0][0] != ciphertexts[1][0]

    fresh = client.post("/api/organizations", json={
        "name": "Fresh", "credential_mode": "new", "key_label": "Dedicated", "key": "brand new upstream secret",
        "allowed_models": ["provider/new"], "monthly_budget_microusd": 10_000_000,
    })
    assert fresh.status_code == 201, fresh.text
    fresh_root = f"/api/organizations/{fresh.json()['id']}"
    assert client.get(fresh_root + "/provider-keys").json()[0]["fingerprint"] == digest("brand new upstream secret")[:16]
    assert client.get(fresh_root + "/policy").json() == {
        "allowed_models": ["provider/new"], "monthly_budget_microusd": 10_000_000,
    }


def test_key_and_invitation_ids_are_organization_scoped(installation):
    _, store, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    key = client.post(root + "/provider-keys", json={"label": "Key", "key": "a provider credential"}).json()
    invitation, member, _ = invited(installation)
    assert member.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).status_code == 403
    _, administrator, admin_account = invited(installation, role="admin", email="administrator@example.org")
    assert administrator.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).status_code == 403
    store.change_member(owner["user_id"], owner["organization_id"], admin_account["user_id"], "owner")
    other_org = administrator.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).json()["id"]
    foreign = f"/api/organizations/{other_org}"
    assert administrator.delete(foreign + "/provider-keys/" + key["id"]).status_code == 404
    assert administrator.delete(foreign + "/invitations/" + invitation["id"]).status_code == 404


def test_policy_is_durable_and_defaults_closed(installation):
    config, _, client, owner = installation
    path = f"/api/organizations/{owner['organization_id']}/policy"
    assert client.get(path).json() == {"allowed_models": [], "monthly_budget_microusd": 0}
    policy = {"allowed_models": ["provider/model", "provider/model"], "monthly_budget_microusd": 50_000_000}
    assert client.put(path, json=policy).json() == {**policy, "allowed_models": ["provider/model"]}
    restarted = TestClient(create_app(config), base_url=PUBLIC_URL, headers=dict(client.headers))
    assert restarted.get(path).json()["monthly_budget_microusd"] == 50_000_000
    assert client.put(path, json={**policy, "monthly_budget_microusd": -1}).status_code == 422


def test_host_password_recovery_revokes_every_session_and_persists(installation, monkeypatch, capsys):
    config, store, client, _ = installation
    new_password = "a completely different password"
    monkeypatch.setattr("getpass.getpass", lambda _prompt: new_password)
    assert main(["--data-dir", str(config.data_dir), "reset-password", "--email", "owner@example.org"]) == 0
    assert "sessions revoked" in capsys.readouterr().out
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "owner@example.org", "password": new_password}).status_code == 200
    assert store.initialized()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/auth/login", {"email": "bad", "password": "sensitive-password-value", "unexpected": "another-secret"}),
        ("/api/organizations/unknown/provider-keys", {"label": "Key", "key": "s3cr3t", "unexpected": "sensitive-key-value"}),
        ("/api/setup", {"setup_token": "sensitive-setup-token", "email": "valid@example.org", "password": "s3cr3t"}),
    ],
)
def test_validation_errors_do_not_echo_secret_inputs(installation, path, body):
    _, _, client, _ = installation
    result = client.post(path, json=body)
    assert result.status_code == 422
    for value in body.values():
        assert value not in result.text
    assert "input" not in result.text


def test_validation_does_not_echo_an_unknown_field_name_containing_a_secret(installation):
    _, _, client, _ = installation
    sensitive_field = "sk-or-secret-used-as-field-name"
    result = client.post("/api/auth/login", json={"email": "owner@example.org", "password": PASSWORD, sensitive_field: "value"})
    assert result.status_code == 422
    assert sensitive_field not in result.text


def test_body_limits_content_type_and_local_workbench_not_exposed(installation):
    _, _, client, _ = installation
    result = client.post("/api/auth/login", content=json.dumps({"padding": "x" * 65536}), headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    # No Content-Length: enforce the same bound on a streamed/chunked body.
    result = client.post("/api/auth/login", content=iter([b"x" * 40000, b"x" * 40000]), headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    assert client.post("/api/auth/login", data={"email": "owner@example.org", "password": PASSWORD}).status_code == 415
    for path in ("/api/projects", "/api/workspaces", "/api/terminal", "/api/codex", "/docs", "/"):
        assert client.get(path).status_code == 404
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:pass@example.org",
        "https://example.org/path",
        "https://example.org?secret=yes",
        "https://example.org#fragment",
        "file:///tmp",
        "https://example.org:99999",
    ],
)
def test_public_url_must_be_secure_origin(tmp_path, url):
    with pytest.raises(ValueError):
        Config(tmp_path, url)


@pytest.mark.parametrize("url", ["https://example.org", "http://localhost:8766", "http://127.0.0.1:8766", "http://[::1]:8766"])
def test_local_development_origins(tmp_path, url):
    assert Config(tmp_path, url).public_url == url


def test_passwords_salted_and_malformed_hashes_fail_closed():
    first, second = password_hash(PASSWORD), password_hash(PASSWORD)
    assert first != second
    assert password_matches(PASSWORD, first)
    assert not password_matches("wrong", first)
    for encoded in ("bad", "bad$salt$hash", "scrypt-v1$xx$yy", "scrypt-v1$" + "z" * 32 + "$" + "x" * 64):
        assert not password_matches(PASSWORD, encoded)
    assert not password_matches("x" * 1025, first)
    with pytest.raises(ValueError):
        password_hash("short")


def test_private_files_initialize_atomically_and_reject_exposure_or_symlinks(tmp_path):
    path = tmp_path / "secret"
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda number: private_file(path, str(number).encode()), range(8)))
    assert len(set(values)) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError):
        private_file(path, b"replacement")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        private_file(link, b"replacement")
    assert path.read_bytes() == values[0]


def test_missing_encryption_key_and_future_schema_fail_closed(tmp_path):
    path = tmp_path / "state"
    store = Store(path)
    (path / "encryption.key").rename(path / "saved-key")
    with pytest.raises(ValueError, match="original encryption.key"):
        Store(path)
    (path / "saved-key").rename(path / "encryption.key")
    with store.transaction(write=True) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"):
        Store(path)


def test_exposed_data_directory_and_database_symlink_are_refused(tmp_path):
    path = tmp_path / "state"
    path.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        Store(path)
    path.chmod(0o700)
    target = tmp_path / "target"
    target.write_bytes(b"do not overwrite")
    (path / "server.sqlite3").symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        Store(path)
    assert target.read_bytes() == b"do not overwrite"


def test_cli_initialization_and_setup_do_not_print_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state"
    assert main(["--data-dir", str(path), "init"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["initialized"] is False
    assert output["setup_token_file"] == str(path / "setup-token")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: PASSWORD)
    assert main(["--data-dir", str(path), "setup", "--email", "owner@example.org", "--name", "Owner", "--organization", "Research"]) == 0
    output = capsys.readouterr().out
    assert PASSWORD not in output
    assert (path / "setup-token").read_text() not in output
    assert Store(path).initialized()
    with Store(path).transaction() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_concurrent_message_replay_creates_one_record(installation):
    _, store, client, owner = installation
    _, _, topics = structure(client, owner["organization_id"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        messages = list(
            pool.map(
                lambda _: store.post_message(owner["user_id"], owner["organization_id"], topics[0]["id"], "same message", "same-client-id"),
                range(8),
            )
        )
    assert len({row["id"] for row in messages}) == 1
    assert store.messages(owner["user_id"], owner["organization_id"], topics[0]["id"]) == [messages[0]]


def test_concurrent_invitation_acceptance_creates_only_one_account(installation):
    _, store, client, owner = installation
    invitation = client.post(f"/api/organizations/{owner['organization_id']}/invitations", json={"email": "new@example.org"}).json()

    def accept(_):
        try:
            return store.accept_invitation(invitation["token"], user_id=None, name="New", password=PASSWORD)
        except StoreError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(accept, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(404) == 3
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM users WHERE email='new@example.org'").fetchone()[0] == 1


def test_changed_installation_key_is_detected_before_serving(installation):
    config, _, client, owner = installation
    root = f"/api/organizations/{owner['organization_id']}"
    assert client.post(root + "/provider-keys", json={"label": "Key", "key": "a provider credential"}).status_code == 201
    original = config.data_dir / "encryption.key"
    original.rename(config.data_dir / "saved-key")
    private_file(original, b"x" * 32)
    with pytest.raises(ValueError, match="cannot decrypt"):
        create_app(config)


@pytest.mark.parametrize("peer,trusted", [("198.51.100.10", False), ("127.0.0.1", True)])
def test_only_loopback_proxy_can_supply_client_address(installation, peer, trusted):
    config, _, _, _ = installation
    app = create_app(config)
    client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"]), base_url=PUBLIC_URL, client=(peer, 1234))
    assert (
        client.post(
            "/api/auth/login", json={"email": "nobody@example.org", "password": "wrong"}, headers={"X-Forwarded-For": "203.0.113.20"}
        ).status_code
        == 401
    )
    expected = "203.0.113.20" if trusted else peer
    with app.state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM auth_attempts WHERE scope=?", ("peer:" + digest(expected),)).fetchone()[0] == 1


def test_public_server_cli_starts_and_serves_real_http(tmp_path):
    path = tmp_path / "state"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "reasoning_ledger.server.cli", "--data-dir", str(path), "serve", "--public-url", url, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with httpx.Client(base_url=url, timeout=1) as client:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, process.communicate()[1].decode()
                try:
                    result = client.get("/api/health")
                    break
                except httpx.TransportError:
                    if time.monotonic() >= deadline:
                        pytest.fail("public server did not become ready")
                    time.sleep(0.05)
            health = result.json()
            assert type(health.pop("uptime_seconds")) is int and type(health.pop("schema_version")) is int
            # A server started without --deployment-release reports no release or commit.
            assert health == {"status": "ok", "initialized": False, "release_id": None,
                              "source_revision": None, "source_modified": None}
            result = client.post(
                "/api/setup",
                json={
                    "setup_token": (path / "setup-token").read_text(),
                    "email": "owner@example.org",
                    "name": "Owner",
                    "password": PASSWORD,
                    "organization_name": "Real HTTP",
                },
            )
            assert result.status_code == 201, result.text
            assert "access_token" not in result.json()
            assert client.get("/api/auth/me").status_code == 200
            result = client.post(
                "/api/organizations", json={"name": "Second", "credential_mode": "none"},
                headers={"Origin": url, "X-CSRF-Token": result.json()["csrf_token"]}
            )
            assert result.status_code == 201, result.text
            assert client.get("/api/projects").status_code == 404
    finally:
        process.terminate()
        try:
            output, errors = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate(timeout=5)
        assert PASSWORD.encode() not in output + errors


def test_password_derivation_parallelism_is_bounded(monkeypatch):
    import threading

    from reasoning_ledger.server import security

    lock = threading.Lock()
    running = peak = 0

    def derive(*args, **kwargs):
        nonlocal running, peak
        assert kwargs["n"] == 2**15 and kwargs["r"] == 8 and kwargs["p"] == 3
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.02)
        with lock:
            running -= 1
        return b"x" * 32

    monkeypatch.setattr(security.hashlib, "scrypt", derive)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(lambda _: password_hash(PASSWORD), range(8)))) == 8
    assert peak == 2
