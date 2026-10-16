"""Bot authorization, durable quotas, message provenance, recovery, and bounded client fixtures."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from public_schema_fixture import remove_collaboration_schema

from reasoning_ledger.server import bot_store as module
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.backup import backup, create_key, restore
from reasoning_ledger.server.bot_client import BotClient, BotClientError, configuration, gcd_reply, research_step
from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.openrouter import Quote
from reasoning_ledger.server.security import digest, private_file
from reasoning_ledger.server.store import Store, StoreError


@pytest.fixture
def fixture(tmp_path):
    store = Store(tmp_path / "server")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "fixture password long", "First")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Project")
    channel = store.create_entity(user, org, "channels", "Channel", project["id"])
    topics = [store.create_entity(user, org, "topics", kind, channel["id"], kind)["id"] for kind in ("alpha", "beta", "gamma")]
    other = store.create_organization(user, "Other")["id"]
    p = store.create_entity(user, other, "projects", "Other")
    c = store.create_entity(user, other, "channels", "Other", p["id"])
    other_topic = store.create_entity(user, other, "topics", "Other", c["id"], "alpha")["id"]
    return store, owner, topics, other, other_topic, BotStore(store)


def bot(fixture, *, scopes=None, topics=None):
    _, owner, allowed, _, _, bots = fixture
    return bots.create(
        owner["user_id"], owner["organization_id"], "Fixture bot", topics or allowed[:1], scopes or ["messages:read", "messages:write"]
    )


def member(fixture, role="member"):
    store, owner, _, _, _, _ = fixture
    invite = store.invite(owner["user_id"], owner["organization_id"], "member@example.org", role, 3600)
    return store.accept_invitation(invite["token"], user_id=None, name="Member", password="fixture password long")["user_id"]


def test_follow_owner_access_includes_future_private_topics_and_tracks_revocation(fixture):
    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.knowledge_store import KnowledgeStore

    store, owner, topics, _, other_topic, bots = fixture
    user, org = member(fixture), owner["organization_id"]
    connected = bots.create(user, org, "My Codex", [], ["messages:read", "messages:write", "knowledge:read"],
                            personal=True, public_access=True)
    restricted = bots.create(user, org, "Restricted", topics[:1], ["messages:read"], personal=True)
    project = store.create_entity(owner["user_id"], org, "projects", "Private", visibility="private", owner_id=owner["user_id"])
    channel = store.create_entity(owner["user_id"], org, "channels", "Private", project["id"])
    topic = store.create_entity(owner["user_id"], org, "topics", "New private beta", channel["id"], "beta")
    assert topic["id"] not in bots.identity(connected["token"])["topic_ids"]
    store.change_project_member(owner["user_id"], org, project["id"], user, "member")
    bots.update(user, org, connected["id"], "My Codex", [topic["id"]], connected["scopes"],
                personal=True, public_access=True)
    assert topic["id"] in bots.identity(connected["token"])["topic_ids"]
    assert other_topic not in bots.identity(connected["token"])["topic_ids"]
    assert topic["id"] not in bots.identity(restricted["token"])["topic_ids"]
    later = store.create_entity(user, org, "topics", "Even newer", channel["id"], "alpha")
    assert later["id"] in bots.identity(connected["token"])["topic_ids"]
    bots.post(connected["token"], later["id"], "New topic works", "future-topic")
    knowledge = KnowledgeStore(store)
    entry = knowledge.create(user, org, client_id="private-reference-one", kind="bibliography", state="active",
                             title="Private source", body="Reference", scope_kind="project", scope_id=project["id"])
    assert entry["id"] in {row["id"] for row in knowledge.list_bot(bots, connected["token"])}
    assert knowledge.get_bot(bots, connected["token"], entry["id"])["id"] == entry["id"]
    runtime = BotRuntime(bots)
    runtime.advertise(connected["token"], "codex", ["default"], False, False)
    assert runtime.models(user, org, topic["id"])[0]["name"] == "My Codex · default"
    store.change_project_member(owner["user_id"], org, project["id"], user, None)
    assert topic["id"] not in bots.identity(connected["token"])["topic_ids"]
    assert bots.identity(connected["token"])["enabled"]  # Other public topics remain usable.
    assert entry["id"] not in {row["id"] for row in knowledge.list_bot(bots, connected["token"])}
    with pytest.raises(StoreError):
        knowledge.get_bot(bots, connected["token"], entry["id"])
    with pytest.raises(StoreError):
        bots.post(connected["token"], later["id"], "Denied", "revoked-private")
    store.change_member(owner["user_id"], org, user, None)
    assert not bots.list(owner["user_id"], org)[0]["enabled"]
    with pytest.raises(StoreError):
        bots.identity(connected["token"])


def test_following_bot_mention_choices_are_live_in_new_topics(fixture):
    store, owner, _, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Codex", [], ["messages:read"],
                      personal=True, public_access=True)
    project = store.create_entity(owner["user_id"], owner["organization_id"], "projects", "Later private",
                                   visibility="private", owner_id=owner["user_id"])
    channel = store.create_entity(owner["user_id"], owner["organization_id"], "channels", "Later", project["id"])
    topic = store.create_entity(owner["user_id"], owner["organization_id"], "topics", "Later", channel["id"], "beta")
    app = create_app(Config(store.data_dir, "https://server.example"))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            path = f"/api/organizations/{owner['organization_id']}/topics/{topic['id']}/bots"
            result = await client.get(path, headers={"authorization": "Bearer " + owner["access_token"]})
            assert result.status_code == 200
            assert result.json() == [{"id": bot["id"], "name": "Codex", "owner_user_id": owner["user_id"]}]
            bots.revoke(owner["user_id"], owner["organization_id"], bot["id"], personal=True)
            assert (await client.get(path, headers={"authorization": "Bearer " + owner["access_token"]})).json() == []
    asyncio.run(check())


def test_reconnect_backoff_is_bounded_and_auth_failures_never_retry():
    from reasoning_ledger.server.bot_client import BotUnavailable, reconnect

    waits, reports = [], []
    failures = 10

    def operation():
        nonlocal failures
        if failures:
            failures -= 1
            raise BotUnavailable("temporary")
        return "ready"

    assert reconnect(operation, wait=waits.append, report=reports.append) == "ready"
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert reports[-1].startswith("✓ Reconnected")
    for status in (401, 403):
        calls = []
        detail = "secret" if status == 401 else "bot access is no longer active"
        def reject(request, calls=calls, status=status, detail=detail):
            calls.append(request)
            return httpx.Response(status, json={"detail": detail})
        client = BotClient(configuration("https://server.example", "rnb_" + "a" * 43), transport=httpx.MockTransport(reject))
        with pytest.raises(BotClientError, match="My bots") as error:
            reconnect(client.identity, wait=waits.append)
        # Authentication refusals stay opaque; a policy refusal carries its server explanation.
        assert (detail not in str(error.value)) if status == 401 else ("(HTTP 403): " + detail in str(error.value))
        assert len(calls) == 1
        client.close()


@pytest.mark.parametrize("failure", ["transport", 502, 503, 504])
def test_client_marks_temporary_disconnects_without_logging_provider_body(failure):
    from reasoning_ledger.server.bot_client import BotUnavailable, reconnect

    calls, waits = [], []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "transport":
                raise httpx.ConnectError("private-address-secret", request=request)
            return httpx.Response(failure, text="private-provider-secret")
        return httpx.Response(200, json=[])
    client = BotClient(configuration("https://server.example", "rnb_" + "a" * 43), transport=httpx.MockTransport(respond))
    assert reconnect(client.topics, wait=waits.append, report=lambda _: None) == []
    assert waits == [1] and len(calls) == 2
    with pytest.raises(BotUnavailable):
        reconnect(lambda: (_ for _ in ()).throw(BotUnavailable("temporary")), once=True, wait=waits.append)
    assert waits == [1]
    client.close()


def test_status_cli_uses_existing_default_config_without_prompt_or_secret(fixture, tmp_path, monkeypatch, capsys):
    from reasoning_ledger.deployment.private import write_document
    from reasoning_ledger.server import bot_client

    _, _, _, _, _, bots = fixture
    record = bot(fixture)
    write_document(tmp_path / "config.json", configuration("https://server.example", record["token"]), new=True)
    original = BotClient
    monkeypatch.setattr(bot_client, "BotClient", lambda conf: original(conf, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=bots.identity(record["token"])))))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("status must not prompt"))
    assert bot_client.main(["status", "--state-dir", str(tmp_path), "--json"]) == 0
    output = capsys.readouterr().out
    assert record["token"] not in output and json.loads(output)["topics"] == 1
    assert bot_client.main(["status", "--state-dir", str(tmp_path)]) == 0
    assert "BOT STATUS" in capsys.readouterr().out


def test_bot_has_separate_identity_and_one_time_hashed_credentials(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    assert record["id"] != owner["user_id"] and record["token"].startswith("rnb_")
    metadata = bots.list(owner["user_id"], owner["organization_id"])
    assert len(metadata) == 1 and "token" not in metadata[0] and "token_hash" not in metadata[0]
    assert "token" not in bots.identity(record["token"])
    assert bots.topics(record["token"])[0]["id"] == topics[0]
    with store.transaction() as db:
        assert db.execute("SELECT token_hash FROM bots WHERE id=?", (record["id"],)).fetchone()[0] == digest(record["token"])
        assert db.execute("SELECT account_kind FROM users WHERE id=?", (record["id"],)).fetchone()[0] == "bot"
    assert record["id"] not in {person["id"] for person in store.members(owner["user_id"], owner["organization_id"])}


def test_bot_messages_have_labeled_provenance_mentions_unread_search_and_idempotency(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    message = bots.post(record["token"], topics[0], f"Hello @[{owner['user_id']}]", "stable-notification")
    assert message["author_kind"] == "bot" and message["bot_name"] == record["name"] and message["author_id"] == record["id"]
    assert "sender_kind" not in message and "sender_name" not in message
    assert bots.post(record["token"], topics[0], message["content"], "stable-notification")["id"] == message["id"]
    assert store.messages(owner["user_id"], owner["organization_id"], topics[0])[0] == message
    assert store.search(owner["user_id"], owner["organization_id"], "Hello")[0]["bot_name"] == record["name"]
    state = store.hierarchy(owner["user_id"], owner["organization_id"])["topic_state"]
    assert next(item for item in state if item["topic_id"] == topics[0])["unread_mention_count"] == 1
    with pytest.raises(StoreError) as conflict:
        bots.post(record["token"], topics[0], "different", "stable-notification")
    assert conflict.value.status == 409


def test_scoped_polling_excludes_ungranted_topics_and_other_organizations(fixture):
    store, owner, topics, other, other_topic, bots = fixture
    first = store.post_message(owner["user_id"], owner["organization_id"], topics[0], "allowed", "first")
    store.post_message(owner["user_id"], owner["organization_id"], topics[1], "hidden", "second")
    store.post_message(owner["user_id"], other, other_topic, "other organization", "third")
    record = bot(fixture)
    assert [row["content"] for row in bots.messages(record["token"])] == ["allowed"]
    assert bots.messages(record["token"], first["sequence"]) == []
    for denied in (topics[1], other_topic, "f" * 32):
        with pytest.raises(StoreError) as error:
            bots.post(record["token"], denied, "denied", "attempt-" + denied)
        assert error.value.status == 404


@pytest.mark.parametrize("scopes,operation", [(["messages:read"], "write"), (["messages:write"], "read")])
def test_read_and_write_are_independent_capabilities(fixture, scopes, operation):
    _, _, topics, _, _, bots = fixture
    record = bot(fixture, scopes=scopes)
    assert bots.identity(record["token"])["enabled"] and len(bots.topics(record["token"])) == 1
    with pytest.raises(StoreError) as error:
        bots.post(record["token"], topics[0], "denied", "denied") if operation == "write" else bots.messages(record["token"])
    assert error.value.status == 403


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_non_admins_cannot_create_or_manage_bot_credentials(fixture, role):
    _, owner, topics, _, _, bots = fixture
    user = member(fixture, role)
    record = bot(fixture)
    actions = [
        lambda: bots.list(user, owner["organization_id"]),
        lambda: bots.create(user, owner["organization_id"], "Bot", topics[:1], ["messages:read"]),
        lambda: bots.update(user, owner["organization_id"], record["id"], "Bot", topics[:1], ["messages:read"]),
        lambda: bots.rotate(user, owner["organization_id"], record["id"]),
        lambda: bots.revoke(user, owner["organization_id"], record["id"]),
    ]
    for action in actions:
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 403


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_personal_bots_are_owned_and_bounded_by_current_user_access(fixture, role):
    store, owner, topics, _, _, bots = fixture
    user = member(fixture, role)
    record = bots.create(user, owner["organization_id"], "My CLI", topics[:1], ["messages:read"], personal=True)
    assert record["delegation_kind"] == "personal" and record["created_by"] == user
    assert bots.list(user, owner["organization_id"], personal=True)[0]["id"] == record["id"]
    assert bots.list(owner["user_id"], owner["organization_id"], personal=True) == []
    assert bots.identity(record["token"])["enabled"]
    assert next(bot for bot in store.presence(user, owner["organization_id"])["bots"] if bot["id"] == record["id"])["owner_user_id"] == user
    if role == "viewer":
        with pytest.raises(StoreError) as error:
            bots.create(user, owner["organization_id"], "Writer", topics[:1], ["messages:write"], personal=True)
        assert error.value.status == 403
    other_bot = bot(fixture)
    with pytest.raises(StoreError) as error:
        bots.rotate(user, owner["organization_id"], other_bot["id"], personal=True)
    assert error.value.status == 404
    fresh = bots.rotate(user, owner["organization_id"], record["id"], personal=True)
    assert fresh["authorized_by"] == user
    store.change_member(owner["user_id"], owner["organization_id"], user, None)
    with pytest.raises(StoreError) as error:
        bots.messages(fresh["token"])
    assert error.value.status == 403


def test_personal_private_topic_grant_is_live_and_cannot_be_expanded_by_admin(fixture):
    store, owner, topics, _, _, bots = fixture
    user = member(fixture)
    org = owner["organization_id"]
    private = store.create_entity(owner["user_id"], org, "projects", "Private", visibility="private", owner_id=owner["user_id"])
    channel = store.create_entity(owner["user_id"], org, "channels", "Private", private["id"])
    topic = store.create_entity(owner["user_id"], org, "topics", "Hidden", channel["id"], "alpha")
    record = bots.create(user, org, "Personal", topics[:1], ["messages:read"], personal=True)
    with pytest.raises(StoreError) as error:
        bots.update(owner["user_id"], org, record["id"], "Expanded", [topic["id"]], ["messages:read"])
    assert error.value.status == 404
    assert bots.identity(record["token"])["topic_ids"] == topics[:1]
    with store.transaction(write=True) as db:
        db.execute("UPDATE projects SET visibility='private' WHERE id=(SELECT project_id FROM channels WHERE id="
                   "(SELECT channel_id FROM topics WHERE id=?))", (topics[0],))
    with pytest.raises(StoreError) as error:
        bots.messages(record["token"])
    assert error.value.status == 403


def test_cross_organization_management_and_grants_are_refused(fixture):
    _, owner, topics, other, other_topic, bots = fixture
    record = bot(fixture)
    for action in (
        lambda: bots.create(owner["user_id"], owner["organization_id"], "Bad", [other_topic], ["messages:read"]),
        lambda: bots.update(owner["user_id"], other, record["id"], "Bad", [other_topic], ["messages:read"]),
        lambda: bots.rotate(owner["user_id"], other, record["id"]),
        lambda: bots.revoke(owner["user_id"], other, record["id"]),
    ):
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 404
    assert bots.identity(record["token"])["topic_ids"] == topics[:1]


def test_live_permission_changes_rotation_expiry_and_permanent_revocation(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    changed = bots.update(owner["user_id"], owner["organization_id"], record["id"], "Renamed", topics[1:2], ["messages:read"])
    assert changed["name"] == "Renamed" and "token" not in changed
    assert bots.topics(record["token"])[0]["id"] == topics[1]
    with pytest.raises(StoreError):
        bots.post(record["token"], topics[0], "denied", "before-grant")
    with store.transaction(write=True) as db:
        db.execute("UPDATE bots SET expires_at=0 WHERE id=?", (record["id"],))
    with pytest.raises(StoreError) as expired:
        bots.identity(record["token"])
    assert expired.value.status == 401
    fresh = bots.rotate(owner["user_id"], owner["organization_id"], record["id"])
    assert fresh["token"] != record["token"] and bots.identity(fresh["token"])["enabled"]
    with pytest.raises(StoreError):
        bots.identity(record["token"])
    bots.revoke(owner["user_id"], owner["organization_id"], record["id"])
    bots.revoke(owner["user_id"], owner["organization_id"], record["id"])
    with pytest.raises(StoreError):
        bots.identity(fresh["token"])
    with pytest.raises(StoreError) as permanent:
        bots.rotate(owner["user_id"], owner["organization_id"], record["id"])
    assert permanent.value.status == 409


@pytest.mark.parametrize("role", ["member", None])
def test_removed_or_demoted_sponsor_disables_bot_until_new_admin_approves(fixture, role):
    store, owner, topics, _, _, bots = fixture
    sponsor = member(fixture, "admin")
    record = bots.create(sponsor, owner["organization_id"], "Sponsored", topics[:1], ["messages:read"])
    store.change_member(owner["user_id"], owner["organization_id"], sponsor, role)
    with pytest.raises(StoreError) as error:
        bots.identity(record["token"])
    assert error.value.status == 403 and not bots.list(owner["user_id"], owner["organization_id"])[0]["enabled"]
    approved = bots.update(owner["user_id"], owner["organization_id"], record["id"], "Sponsored", topics[:1], ["messages:read"])
    assert approved["created_by"] == sponsor and approved["authorized_by"] == owner["user_id"]
    assert bots.identity(record["token"])["enabled"]


def test_bot_cannot_get_human_sessions_memberships_provider_keys_or_organizations(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    with pytest.raises(StoreError):
        store.session(record["token"])
    with pytest.raises(StoreError):
        store.login(f"bot-{record['id']}@bots.invalid", "fixture password long", "fixture")
    with pytest.raises(StoreError):
        store.reset_password(f"bot-{record['id']}@bots.invalid", "fixture password long")
    with pytest.raises(StoreError):
        store.create_organization(record["id"], "Escape")
    with store.transaction(write=True) as db:
        with pytest.raises(StoreError):
            store._new_session(db, record["id"])
        # Even an invalid operator-created human membership cannot promote a bot principal.
        db.execute("INSERT INTO memberships VALUES(?,?,'owner')", (owner["organization_id"], record["id"]))
    with pytest.raises(StoreError):
        store.keys(record["id"], owner["organization_id"])
    with pytest.raises(StoreError):
        store.post_message(record["id"], owner["organization_id"], topics[0], "wrong route", "wrong-route")


def test_revocation_between_metering_and_data_transaction_is_rechecked(fixture, monkeypatch):
    _, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    original = bots._meter

    def metered(*args):
        original(*args)
        bots.revoke(owner["user_id"], owner["organization_id"], record["id"])

    monkeypatch.setattr(bots, "_meter", metered)
    with pytest.raises(StoreError) as error:
        bots.post(record["token"], topics[0], "not committed", "race")
    assert error.value.status == 401


def test_durable_rate_limits_include_denied_scopes_and_survive_reopening(fixture, monkeypatch):
    store, _, topics, _, _, bots = fixture
    monkeypatch.setattr(module, "WRITES_PER_MINUTE", 2)
    record = bot(fixture, scopes=["messages:read"])
    for _ in range(2):
        with pytest.raises(StoreError) as denied:
            bots.post(record["token"], topics[0], "denied", "denied")
        assert denied.value.status == 403
    reopened = BotStore(Store(store.data_dir))
    with pytest.raises(StoreError) as limited:
        reopened.post(record["token"], topics[0], "denied", "denied")
    assert limited.value.status == 429
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_requests SET attempted_at=0")
    with pytest.raises(StoreError) as permitted:
        reopened.post(record["token"], topics[0], "denied", "denied")
    assert permitted.value.status == 403


def test_concurrent_retries_commit_one_message(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: bots.post(record["token"], topics[0], "one message", "same-id"), range(20)))
    assert len({result["id"] for result in results}) == 1
    assert len(store.messages(owner["user_id"], owner["organization_id"], topics[0])) == 1


def test_concurrent_write_admission_does_not_exceed_quota(fixture, monkeypatch):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    monkeypatch.setattr(module, "WRITES_PER_MINUTE", 4)

    def post(index):
        try:
            bots.post(record["token"], topics[0], "bounded", "request-" + str(index))
            return 201
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(post, range(20)))
    assert statuses.count(201) == 4 and statuses.count(429) == 16
    assert len(store.messages(owner["user_id"], owner["organization_id"], topics[0])) == 4


def test_request_quota_counts_identity_topics_and_history(fixture, monkeypatch):
    _, _, _, _, _, bots = fixture
    record = bot(fixture)
    monkeypatch.setattr(module, "REQUESTS_PER_MINUTE", 2)
    assert bots.identity(record["token"])["enabled"]
    assert bots.topics(record["token"])
    with pytest.raises(StoreError) as error:
        bots.messages(record["token"])
    assert error.value.status == 429


def test_bot_admission_requires_revoking_expired_or_unused_identities(fixture, monkeypatch):
    _, owner, _, _, _, bots = fixture
    monkeypatch.setattr(module, "MAX_BOTS", 2)
    first, _ = bot(fixture), bot(fixture)
    with pytest.raises(StoreError) as error:
        bot(fixture)
    assert error.value.status == 409
    bots.revoke(owner["user_id"], owner["organization_id"], first["id"])
    assert bot(fixture)["id"] != first["id"]


@pytest.mark.parametrize(
    "change,value",
    [
        ("name", " "),
        ("name", "x" * 201),
        ("topics", []),
        ("topics", ["invalid"]),
        ("scopes", []),
        ("scopes", ["admin"]),
        ("scopes", ["messages:read", "messages:read"]),
        ("lifetime", True),
        ("lifetime", 0),
        ("lifetime", 31_536_001),
    ],
)
def test_invalid_bot_permissions_and_lifetimes_are_refused(fixture, change, value):
    _, owner, topics, _, _, bots = fixture
    args = {"name": "Bot", "topics": topics[:1], "scopes": ["messages:read"], "lifetime": 3600}
    args[change] = value
    with pytest.raises(StoreError):
        bots.create(owner["user_id"], owner["organization_id"], **args)


def test_backup_restores_labeled_history_but_revokes_bots(fixture, tmp_path):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    bots.post(record["token"], topics[0], "keep bot history", "keep")
    key = tmp_path / "key"
    create_key(key)
    result = backup(store.data_dir, tmp_path / "archive", key)
    restored = restore(tmp_path / "archive", key, result["sha256"], tmp_path / "restored", source_stopped=True)
    assert restored["bots_revoked"] == 1
    new = Store(tmp_path / "restored")
    assert new.messages(owner["user_id"], owner["organization_id"], topics[0])[0]["author_kind"] == "bot"
    with pytest.raises(StoreError):
        BotStore(new).identity(record["token"])


def test_bot_messages_are_quoted_user_context_not_assistant_or_paid_execution(fixture):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture, topics=topics[1:2])
    store.add_key(owner["user_id"], owner["organization_id"], "openrouter", "Fixture", "provider-secret")
    store.set_policy(owner["user_id"], owner["organization_id"], ["fixture/model"], 1_000_000)
    sent = bots.post(record["token"], topics[1], "Bot research note", "note")
    inference = InferenceStore(store)
    assert inference.runs(owner["user_id"], owner["organization_id"], topics[1]) == []
    params = inference.parameters(["fixture/model"], 128, 20, sent["sequence"])
    with pytest.raises(StoreError):
        inference.enqueue(record["id"], owner["organization_id"], topics[1], "not-allowed", params, [])
    inference.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[1],
        "explicit-human-request",
        params,
        [Quote("fixture/model", "0.000001", "0.000002", "0", 200000, 8192)],
    )
    with store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests").fetchone()[0])
        source = next(
            (message, json.loads(message["content"]))
            for message in context
            if message["content"].startswith("{") and json.loads(message["content"]).get("id") == sent["id"]
        )
        assert source[0]["role"] == "user"
        assert source[1] == {
            "id": sent["id"],
            "sequence": sent["sequence"],
            "kind": "bot",
            "sender": "Fixture bot",
            "text": "Bot research note",
        }
    run = inference.claim()
    assert run is not None
    assert inference.progress(run, "Assistant reply", None)
    assert inference.finish(run, "succeeded", no_charge=True)
    assert store.messages(owner["user_id"], owner["organization_id"], topics[1])[-1]["author_kind"] == "assistant"


def test_schema_five_migrates_human_and_assistant_provenance_atomically(fixture):
    store, owner, topics, _, _, _ = fixture
    store.post_message(owner["user_id"], owner["organization_id"], topics[0], "human", "human")
    with store.transaction(write=True) as db:
        remove_collaboration_schema(db)
        for table in (
            "knowledge_writes",
            "knowledge_versions",
            "knowledge_entries",
            "user_presence",
            "bot_webhook_requests",
            "bot_webhook_deliveries",
            "bot_webhooks",
            "workflow_run_actions",
            "workflow_run_events",
            "workflow_node_attempts",
            "workflow_runs",
            "workflow_writes",
            "workflow_versions",
            "workflows",
            "invitation_mail",
            "host_setup_receipt",
            "compute_scaler_requests",
            "compute_enrollments",
            "compute_scalers",
            "bot_requests",
            "bot_topics",
            "bots",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP INDEX invitation_org_identity")
        db.execute("DROP INDEX message_org_identity")
        db.execute("ALTER TABLE messages DROP COLUMN sender_kind")
        db.execute("ALTER TABLE users DROP COLUMN account_kind")
        db.execute("DROP INDEX compute_worker_retirement")
        db.execute("ALTER TABLE compute_workers DROP COLUMN retirement_id")
        db.execute("ALTER TABLE compute_workers DROP COLUMN retired_at")
        db.execute("UPDATE messages SET author_kind='assistant',assistant_model='fixture/model'")
        db.execute("PRAGMA user_version=5")
        db.execute("DROP TABLE model_impact_profiles")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("DROP TABLE project_memberships")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
    migrated = Store(store.data_dir)
    assert migrated.messages(owner["user_id"], owner["organization_id"], topics[0])[0]["author_kind"] == "assistant"
    with migrated.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:secret@example.org",
        "https://example.org/path",
        "https://example.org?secret=1",
        "https://example.org#secret",
        "https://example.org:99999",
        "https://exam\nple.org",
    ],
)
def test_client_rejects_unsafe_origins_without_echoing_input(url):
    with pytest.raises(BotClientError) as error:
        configuration(url, "rnb_" + "a" * 43)
    assert "secret" not in str(error.value) and url not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 307, 401, 429, 500])
def test_client_refuses_redirects_and_error_bodies_without_echoing_secrets(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.example"}, json={"detail": "credential-secret"})

    client = BotClient(configuration("https://server.example", "rnb_" + "a" * 43), transport=httpx.MockTransport(respond))
    with pytest.raises(BotClientError) as error:
        client.identity()
    assert len(calls) == 1 and calls[0].url.host == "server.example" and "credential-secret" not in str(error.value)
    client.close()


def test_client_reports_a_policy_refusal_as_one_bounded_line_without_following_redirects():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403, headers={"location": "https://attacker.example"},
                              json={"detail": "bot owner no longer\npermits other users", "extra": "never shown"})

    client = BotClient(configuration("https://server.example", "rnb_" + "a" * 43), transport=httpx.MockTransport(respond))
    with pytest.raises(BotClientError) as error:
        client.identity()
    client.close()
    assert len(calls) == 1 and calls[0].url.host == "server.example"
    assert str(error.value).startswith("bot access rejected (HTTP 403): bot owner no longer permits other users.")
    assert "never shown" not in str(error.value) and "attacker" not in str(error.value)


def test_client_messages_validate_order_and_bounds():
    row = {"id": "a" * 32, "topic_id": "b" * 32, "sequence": 1, "author_kind": "human", "content": "hello"}
    client = BotClient(
        configuration("https://server.example", "rnb_" + "c" * 43),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[row, row])),
    )
    with pytest.raises(BotClientError, match="message page"):
        client.messages()
    client.close()


def test_bot_configure_cli_is_private_exclusive_and_does_not_print_token(tmp_path, capsys):
    from reasoning_ledger.server.bot_client import main

    token = "rnb_" + "a" * 43
    private_file(tmp_path / "token", token.encode())
    arguments = [
        "configure",
        "--server",
        "https://server.example",
        "--token-file",
        str(tmp_path / "token"),
        "--output",
        str(tmp_path / "config"),
    ]
    assert main(arguments) == 0
    assert token not in capsys.readouterr().out
    assert (tmp_path / "config").stat().st_mode & 0o777 == 0o600
    before = (tmp_path / "config").read_bytes()
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2 and token not in capsys.readouterr().err and (tmp_path / "config").read_bytes() == before


def test_bot_configure_cli_prompts_privately_when_no_token_file(tmp_path, monkeypatch, capsys):
    from reasoning_ledger.server import bot_client

    token = "rnb_" + "a" * 43
    prompts = []
    monkeypatch.setattr(bot_client.getpass, "getpass", lambda prompt: prompts.append(prompt) or token)
    output = tmp_path / "config"
    assert bot_client.main(["configure", "--server", "https://server.example", "--output", str(output)]) == 0
    assert prompts == ["Bot credential: "]
    assert bot_client.load_configuration(output)["token"] == token
    assert token not in capsys.readouterr().out


def test_notification_and_research_cli_with_actual_store_transport_fixture(fixture, tmp_path, monkeypatch, capsys):
    from reasoning_ledger.deployment.private import write_document
    from reasoning_ledger.server import bot_client

    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    config = tmp_path / "config"
    write_document(config, configuration("https://server.example", record["token"]), new=True)
    private_file(tmp_path / "notification", b"Notification fixture")

    def respond(request):
        if request.url.path == "/api/bot/identity":
            value = bots.identity(record["token"])
        elif request.url.path == "/api/bot/topics":
            value = bots.topics(record["token"])
        elif request.method == "GET":
            value = bots.messages(record["token"], int(request.url.params["after"]), int(request.url.params["limit"]))
        else:
            body = json.loads(request.content)
            value = bots.post(record["token"], topics[0], body["content"], body["client_id"])
        return httpx.Response(200, json=value)

    original = BotClient
    monkeypatch.setattr(bot_client, "BotClient", lambda conf: original(conf, transport=httpx.MockTransport(respond)))
    assert bot_client.main(["identity", "--config", str(config)]) == 0
    assert record["token"] not in capsys.readouterr().out
    assert bot_client.main(["topics", "--config", str(config)]) == 0
    granted = json.loads(capsys.readouterr().out)
    assert [topic["id"] for topic in granted] == [topic["id"] for topic in bots.topics(record["token"])]
    assert {topic["kind"] for topic in granted} == {"alpha"}
    assert (
        bot_client.main(
            [
                "post",
                "--config",
                str(config),
                "--topic",
                topics[0],
                "--message-file",
                str(tmp_path / "notification"),
                "--client-id",
                "cli-notification",
            ]
        )
        == 0
    )
    assert "message_id" in capsys.readouterr().out
    store.post_message(owner["user_id"], owner["organization_id"], topics[0], "!gcd 12 18", "cli-command")
    command = ["research", "--config", str(config), "--state-dir", str(tmp_path / "research"), "--once"]
    assert bot_client.main(command) == 0
    assert json.loads(capsys.readouterr().out)["replies"] == 1
    assert bot_client.main(command) == 0
    assert json.loads(capsys.readouterr().out)["replies"] == 0
    assert len(store.messages(owner["user_id"], owner["organization_id"], topics[0])) == 3


@pytest.mark.parametrize(
    "kind,content,expected",
    [
        ("human", "!gcd 12 18", "= 6"),
        ("human", "!gcd -12 18", "= 6"),
        ("human", "!gcd 0 0", "= 0"),
        ("bot", "!gcd 12 18", None),
        ("assistant", "!gcd 12 18", None),
        ("human", "!gcd __import__('os') 3", None),
        ("human", "!gcd " + "1" * 101 + " 3", None),
    ],
)
def test_research_example_uses_bounded_integer_arithmetic_not_eval_or_bot_loops(kind, content, expected):
    result = gcd_reply({"author_kind": kind, "content": content})
    assert result is None if expected is None else expected in result


def test_research_journal_retries_lost_post_receipts_without_duplicate_messages(fixture, tmp_path):
    store, owner, topics, _, _, bots = fixture
    record = bot(fixture)
    human = store.post_message(owner["user_id"], owner["organization_id"], topics[0], "!gcd 12 18", "gcd")
    lose = [True]

    def respond(request):
        if request.method == "GET":
            result = bots.messages(record["token"], int(request.url.params["after"]), int(request.url.params["limit"]))
        else:
            body = json.loads(request.content)
            result = bots.post(record["token"], topics[0], body["content"], body["client_id"])
            if lose.pop() if lose else False:
                raise httpx.ReadError("lost response secret", request=request)
        return httpx.Response(200, json=result)

    client = BotClient(configuration("https://server.example", record["token"]), transport=httpx.MockTransport(respond))
    journal, binding = tmp_path / "research.json", {"server": client.server, "bot_id": record["id"]}
    with pytest.raises(BotClientError):
        research_step(client, journal, binding)
    assert json.loads(journal.read_text())["pending"]["client_id"] == "gcd-" + human["id"]
    result = research_step(client, journal, binding)
    assert result["cursor"] > human["sequence"]
    assert len(store.messages(owner["user_id"], owner["organization_id"], topics[0])) == 2
    assert research_step(client, journal, binding)["processed"] == 0
    with pytest.raises(BotClientError):
        research_step(client, journal, {"server": client.server, "bot_id": "f" * 32})
    client.close()


def test_asgi_route_contract_with_inline_test_dispatch(fixture, monkeypatch):
    # This exercises ASGI parsing/security/serialization, not production worker threads or TCP.
    # The sandbox cannot wake AnyIO's cross-thread socketpair; confine dispatch substitution to this test.
    import fastapi.dependencies.utils
    import fastapi.routing

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    store, owner, topics, _, _, _ = fixture
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            root = "/api/organizations/" + owner["organization_id"] + "/bots"
            headers = {"authorization": "Bearer " + owner["access_token"]}
            result = await client.post(
                root, headers=headers, json={"name": "API bot", "topic_ids": topics[:1], "scopes": ["messages:write"]}
            )
            assert result.status_code == 201
            bot_headers = {"authorization": "Bearer " + result.json()["token"]}
            token = result.json()["token"]
            assert (await client.get(root, headers=bot_headers)).status_code == 401
            assert (await client.get("/api/bot/messages", headers=bot_headers)).status_code == 403
            sent = await client.post(
                "/api/bot/topics/" + topics[0] + "/messages",
                headers=bot_headers,
                json={"content": "API notification", "client_id": "api-stable"},
            )
            assert sent.status_code == 201 and sent.json()["author_kind"] == "bot"
            assert sent.headers["cache-control"] == "no-store"
            forged = await client.post(
                "/api/bot/topics/" + topics[0] + "/messages",
                headers=bot_headers,
                json={"content": "forged", "client_id": "forged", "author_id": owner["user_id"], "token": token},
            )
            assert forged.status_code == 422 and token not in forged.text
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            assert (await client.get("/api/bot/identity")).status_code == 401
            body = {"name": "Cookie bot", "topic_ids": topics[:1], "scopes": ["messages:read"]}
            assert (await client.post(root, json=body)).status_code == 403
            assert (await client.post(root, headers={"origin": "https://server.example"}, json=body)).status_code == 403
            assert (
                await client.post(root, headers={"origin": "https://server.example", "x-csrf-token": owner["csrf_token"]}, json=body)
            ).status_code == 201
            client.cookies.clear()
            assert (await client.get("/api/bot/identity", headers={**bot_headers, "origin": "https://attacker.example"})).status_code == 403
            assert (await client.delete(root + "/" + result.json()["id"], headers=headers)).status_code == 204
            assert (await client.get("/api/bot/identity", headers=bot_headers)).status_code == 401

    asyncio.run(check())
