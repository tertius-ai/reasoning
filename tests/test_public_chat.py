"""Durable chat preferences, mentions, search, paging, and public static serving."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.security import private_file
from reasoning_ledger.server.store import Store


def topic_state(client, org, topic):
    return next(row for row in client.get(f"/api/organizations/{org}/hierarchy").json()["topic_state"] if row["topic_id"] == topic)


def test_read_state_subscription_and_mentions_survive_restart(installation):
    config, _, owner_client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(owner_client, org)
    topic = topics[0]["id"]
    _, member, user = invited(installation)
    path = f"/api/organizations/{org}/topics/{topic}"
    first = owner_client.post(path + "/messages", json={"content": f"Hello @[{user['user_id']}]", "client_id": "first"}).json()
    second = owner_client.post(path + "/messages", json={"content": "Another message", "client_id": "second"}).json()
    state = topic_state(member, org, topic)
    assert state["unread_count"] == 2
    assert state["unread_mention_count"] == 1
    assert state["subscribed"] is True
    assert member.put(path + "/subscription", json={"subscribed": False}).status_code == 204
    assert member.put(path + "/read", json={"sequence": first["sequence"]}).status_code == 204
    restarted = TestClient(create_app(config), base_url=config.public_url, headers=dict(member.headers))
    state = topic_state(restarted, org, topic)
    assert state["subscribed"] is False
    assert state["last_read_sequence"] == first["sequence"]
    assert state["unread_count"] == 1
    assert state["unread_mention_count"] == 0
    assert restarted.put(path + "/read", json={"sequence": second["sequence"]}).status_code == 204
    assert restarted.put(path + "/read", json={"sequence": first["sequence"]}).status_code == 204
    assert topic_state(restarted, org, topic)["last_read_sequence"] == second["sequence"]
    assert topic_state(owner_client, org, topic)["unread_count"] == 2


def test_invalid_read_cursors_and_cross_topic_preferences_fail(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    root = f"/api/organizations/{org}/topics/"
    message = client.post(root + topics[1]["id"] + "/messages", json={"content": "Other topic", "client_id": "other"}).json()
    assert client.put(root + topics[0]["id"] + "/read", json={"sequence": message["sequence"]}).status_code == 400
    assert client.put(root + topics[0]["id"] + "/read", json={"sequence": 999999}).status_code == 400
    assert client.put(root + topics[0]["id"] + "/read", json={"sequence": -1}).status_code == 422
    assert client.put(root + topics[0]["id"] + "/read", json={"sequence": 10**30}).status_code == 422
    _, other, person = invited(installation, role="admin")
    installation[1].change_member(owner["user_id"], org, person["user_id"], "owner")
    other_org = other.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).json()["id"]
    other_topic = structure(other, other_org)[2][0]["id"]
    for suffix, body in (("subscription", {"subscribed": False}), ("read", {"sequence": 0})):
        assert client.put(f"/api/organizations/{org}/topics/{other_topic}/{suffix}", json=body).status_code == 404
        assert client.put(f"/api/organizations/{other_org}/topics/{other_topic}/{suffix}", json=body).status_code == 404


def test_viewers_can_manage_only_their_own_read_preferences(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    topic = structure(client, org)[2][0]["id"]
    _, viewer, _ = invited(installation, role="viewer")
    root = f"/api/organizations/{org}/topics/{topic}"
    assert viewer.put(root + "/subscription", json={"subscribed": False}).status_code == 204
    assert viewer.put(root + "/read", json={"sequence": 0}).status_code == 204
    assert topic_state(client, org, topic)["subscribed"] is True


def test_mentions_notify_only_current_organization_members_once(installation):
    _, store, client, owner = installation
    org = owner["organization_id"]
    topic = structure(client, org)[2][0]["id"]
    _, member, user = invited(installation)
    body = {"content": f"@[{user['user_id']}] @[{user['user_id']}] @[{32 * 'f'}]", "client_id": "mention"}
    path = f"/api/organizations/{org}/topics/{topic}/messages"
    assert client.post(path, json=body).status_code == 201
    assert client.post(path, json=body).status_code == 201
    assert topic_state(member, org, topic)["unread_mention_count"] == 1
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM message_mentions").fetchone()[0] == 1
    assert client.delete(f"/api/organizations/{org}/members/{user['user_id']}").status_code == 204
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM message_mentions").fetchone()[0] == 0
    assert len(client.get(path).json()) == 1


def test_search_is_literal_bounded_and_tenant_scoped(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    topic = structure(client, org)[2][0]["id"]
    path = f"/api/organizations/{org}/topics/{topic}/messages"
    for index, content in enumerate(("Rank is 100%_exact", "Rank is ordinary", "A different discussion")):
        assert client.post(path, json={"content": content, "client_id": str(index)}).status_code == 201
    search = f"/api/organizations/{org}/search"
    result = client.get(search, params={"q": "rank", "limit": 1}).json()
    assert len(result) == 1 and result[0]["content"] == "Rank is ordinary"
    assert result[0]["project_name"] == "Algebra" and result[0]["channel_name"] == "research"
    assert client.get(search, params={"q": "%_"}).json()[0]["content"] == "Rank is 100%_exact"
    assert client.get(search, params={"q": "   "}).status_code == 400
    assert client.get(search, params={"q": "x", "limit": 101}).status_code == 422
    _, other, person = invited(installation, role="admin")
    installation[1].change_member(owner["user_id"], org, person["user_id"], "owner")
    foreign_org = other.post("/api/organizations", json={"name": "Other", "credential_mode": "none"}).json()["id"]
    assert client.get(f"/api/organizations/{foreign_org}/search", params={"q": "Rank"}).status_code == 404
    assert other.get(f"/api/organizations/{foreign_org}/search", params={"q": "Rank"}).json() == []


def test_backward_history_is_ordered_and_does_not_skip_pages(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    topic = structure(client, org)[2][0]["id"]
    path = f"/api/organizations/{org}/topics/{topic}/messages"
    messages = [client.post(path, json={"content": str(index), "client_id": str(index)}).json() for index in range(5)]
    newest = client.get(path, params={"before": 2**63 - 1, "limit": 2}).json()
    assert newest == messages[-2:]
    older = client.get(path, params={"before": newest[0]["sequence"], "limit": 2}).json()
    assert older == messages[1:3]
    assert client.get(path, params={"before": older[0]["sequence"], "limit": 2}).json() == messages[:1]
    assert client.get(path, params={"after": 1, "before": 100}).status_code == 400
    assert client.get(path, params={"after": 10**30}).status_code == 422


@pytest.mark.parametrize("version", [1, 2])
def test_schema_upgrade_retains_core_state_and_initializes_chat_tables(installation, version):
    config, store, client, owner = installation
    org = owner["organization_id"]
    topic = structure(client, org)[2][0]["id"]
    path = f"/api/organizations/{org}/topics/{topic}/messages"
    message = client.post(path, json={"content": "Durable message", "client_id": "old"}).json()
    # Construct an actual old schema, rather than relabeling a newer database.
    old_dir = config.data_dir.parent / "old-state"
    old_dir.mkdir(mode=0o700)
    for name in ("encryption.key", "setup-token"):
        private_file(old_dir / name, (config.data_dir / name).read_bytes())
    private_file(old_dir / "server.sqlite3", b"")
    with sqlite3.connect(old_dir / "server.sqlite3") as old, store.transaction() as source:
        for migration in MIGRATIONS[:version]:
            for statement in migration:
                old.execute(statement)
        for (table,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'").fetchall():
            columns = [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
            rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            old.execute(f"DELETE FROM {table}")
            old.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", rows)
        old.execute(f"PRAGMA user_version={version}")
    restarted = TestClient(create_app(Config(old_dir, config.public_url)), base_url=config.public_url, headers=dict(client.headers))
    assert restarted.get(path).json() == [message]
    assert topic_state(restarted, org, topic)["unread_count"] == 1


def test_frontend_is_same_origin_and_never_exposes_local_workbench(tmp_path):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "licenses").mkdir()
    (frontend / "organization.html").write_text('<html><script src="/assets/chat.js"></script></html>')
    (frontend / "index.html").write_text("Do not expose the local workbench")
    (frontend / "assets" / "chat.js").write_text("console.log('Reasoning');")
    (frontend / "licenses" / "NOTICE.txt").write_text("Benjamin Antieau and Deven Manam")
    (frontend / "reasoning.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    client = TestClient(create_app(Config(tmp_path / "state", "https://chat.example.org", frontend)))
    assert client.get("/").status_code == 200
    for view in ("ledger", "bibliography", "journal"):
        assert client.get("/organization.html?view=" + view).text == client.get("/").text
    assert "Do not expose" not in client.get("/").text
    assert client.get("/assets/chat.js").status_code == 200
    assert client.get("/licenses/NOTICE.txt").status_code == 200
    assert client.get("/reasoning.svg").status_code == 200
    for path in ("/index.html", "/api/projects", "/api/terminal", "/setup-token", "/server.sqlite3"):
        assert client.get(path).status_code == 404
    assert "script-src 'self'" in client.get("/").headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]


def test_only_successful_hashed_assets_are_cacheable(tmp_path):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "organization.html").write_text('<script src="/assets/organization-Necs_lms.js"></script>')
    (frontend / "assets" / "organization-Necs_lms.js").write_text("export const fixture = true;")
    (frontend / "assets" / "chat.js").write_text("export const unversioned = true;")
    client = TestClient(create_app(Config(tmp_path / "state", "https://chat.example.org", frontend)))
    hashed = client.get("/assets/organization-Necs_lms.js")
    assert hashed.status_code == 200
    assert hashed.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert hashed.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in hashed.headers["content-security-policy"]
    revalidated = client.get("/assets/organization-Necs_lms.js", headers={"If-None-Match": hashed.headers["etag"]})
    assert revalidated.status_code == 304 and revalidated.headers["cache-control"] == "public, max-age=31536000, immutable"
    for path in ("/assets/chat.js", "/assets/missing-AbCd1234.js", "/", "/api/health"):
        assert client.get(path).headers["cache-control"] == "no-store", path
    assert client.get("/assets/missing-AbCd1234.js").status_code == 404


def test_hashed_assets_of_retained_releases_stay_available(tmp_path):
    releases = tmp_path / "opt/reasoning/releases"
    old, current = releases / ("a" * 64), releases / ("b" * 64)
    for release, chunk in ((old, "Ledger-OldChunk.js"), (current, "Ledger-NewChunk.js")):
        (release / "web/assets").mkdir(parents=True)
        (release / "web/organization.html").write_text("<html></html>")
        (release / "web/assets" / chunk).write_text("export const release = " + repr(release.name) + ";")
    (old / "web/assets/unhashed.js").write_text("export const old = true;")
    (releases / ".release-partial/web/assets").mkdir(parents=True)
    (releases / ".release-partial/web/assets/Partial-Abcd1234.js").write_text("partial")
    client = TestClient(create_app(Config(tmp_path / "state", "https://chat.example.org", current / "web")))
    assert client.get("/assets/Ledger-NewChunk.js").text.endswith(repr(current.name) + ";")
    retained = client.get("/assets/Ledger-OldChunk.js")
    assert retained.status_code == 200 and retained.text.endswith(repr(old.name) + ";")
    assert retained.headers["cache-control"] == "public, max-age=31536000, immutable"
    for path in ("/assets/unhashed.js", "/assets/Partial-Abcd1234.js", "/assets/..%2F..%2F" + "a" * 64 + "%2Fweb%2Forganization.html"):
        assert client.get(path).status_code == 404, path
    standalone = tmp_path / "standalone"
    (standalone / "assets").mkdir(parents=True)
    (standalone / "organization.html").write_text("<html></html>")
    assert TestClient(create_app(Config(tmp_path / "state2", "https://chat.example.org", standalone))).get(
        "/assets/Ledger-OldChunk.js").status_code == 404


def test_frontend_asset_symlinks_cannot_expose_installation_secrets(tmp_path):
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "organization.html").write_text("<html></html>")
    config = Config(tmp_path / "state", "https://chat.example.org", frontend)
    store = Store(config.data_dir)
    (frontend / "assets" / "leak").symlink_to(store.data_dir / "setup-token")
    client = TestClient(create_app(config))
    assert client.get("/assets/leak").status_code == 404


def test_visible_projects_match_the_hierarchy_for_each_reader(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    structure(client, org)
    store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    _, _, member = invited(installation)
    for reader in (user, member["user_id"]):
        assert store.visible_projects(reader, org) == store.hierarchy(reader, org)["projects"]
    assert "Private" in [project["name"] for project in store.visible_projects(user, org)]
    assert "Private" not in [project["name"] for project in store.visible_projects(member["user_id"], org)]
