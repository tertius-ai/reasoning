"""The owner's server-side "Who can use this bot" setting on every invocation path; no paid inference.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio

import httpx
import pytest
from test_public_bot_developer import pending, trusted
from test_public_bots import fixture as bot_fixture
from test_public_bots import share
from test_public_workflow_runs import installation as installation  # noqa: F401 - pytest fixture
from test_public_workflow_runs import llm_node

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_sharing import BotSharing, setting
from reasoning_ledger.server.bot_sharing_schema import SCHEMA as BOT_SHARING_SCHEMA
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.store import Store, StoreError

POLICY = "Who can use this bot"


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def person(fixture, email, role="member"):
    store, owner, _, _, _, _ = fixture
    invitation = store.invite(owner["user_id"], owner["organization_id"], email, role, 3600)
    return store.accept_invitation(invitation["token"], user_id=None, name=email.split("@")[0],
                                   password="fixture password long")


def connected(fixture, *, runner_flag=False, local_tools=False):
    store, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Shared", [],
                      ["messages:read", "messages:write"], personal=True, public_access=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], runner_flag, local_tools)
    return runtime, bot, f"bot/{bot['id']}/default"


def attempt(fixture, runtime, bot, user, label):
    """A member's mention and topic-default request; returns both resulting bot runs."""
    store, owner, topics, _, _, _ = fixture
    org = owner["organization_id"]
    mention = store.post_message(user, org, topics[1], f"@[{bot['id']}] {label}", "mention-" + label)
    plain = store.post_message(user, org, topics[1], label, "plain-" + label)
    return (runtime.from_message(bot["token"], mention["id"]),
            runtime.enqueue(user, org, topics[1], f"bot/{bot['id']}/default", "default-" + label,
                            through=plain["sequence"], decline=True))


def declined(run):
    return run["status"] == "cancelled" and run["error_code"] == "requester_not_permitted" and run["output_text"] == ""


def routes(runtime, fixture, user):
    store, owner, topics, _, _, _ = fixture
    with store.transaction() as db:
        channel = db.execute("SELECT channel_id FROM topics WHERE id=?", (topics[1],)).fetchone()[0]
    return (bool(runtime.models(user, owner["organization_id"], topics[1])),
            bool(runtime.channel_models(user, owner["organization_id"], channel)))


@pytest.mark.parametrize("runner_flag", [False, True])
def test_only_me_is_the_default_and_the_runner_flag_never_widens_it(fixture, runner_flag):
    store, owner, topics, _, _, bots = fixture
    org = owner["organization_id"]
    other = person(fixture, "other@example.org")["user_id"]
    runtime, bot, model = connected(fixture, runner_flag=runner_flag)
    assert bots.list(owner["user_id"], org, personal=True)[0]["sharing"] == {"policy": "owner", "members": []}
    assert "sharing" not in bots.identity(bot["token"])
    assert routes(runtime, fixture, other) == (False, False)
    assert routes(runtime, fixture, owner["user_id"]) == (True, True)
    mentioned, selected = attempt(fixture, runtime, bot, other, "first")
    assert declined(mentioned) and declined(selected) and selected["requested_by"] == other
    # Internal paths (goals, delegation, topic agents, workflows) refuse without recording a run.
    with pytest.raises(StoreError, match=POLICY) as error:
        runtime.enqueue(other, org, topics[1], model, "internal", prompt="Delegated work", decline=True)
    assert error.value.status == 403
    assert runtime.claim(bot["token"]) is None
    # A lost response is answered with the same refusal, and nothing is queued.
    plain = store.post_message(other, org, topics[1], "Again", "plain-again")
    retry = runtime.enqueue(other, org, topics[1], model, "default-first", through=plain["sequence"], decline=True)
    assert retry["id"] == selected["id"]
    with pytest.raises(StoreError):
        runtime.enqueue(other, org, topics[2], model, "default-first", through=plain["sequence"], decline=True)
    with pytest.raises(StoreError, match="human request message"):
        runtime.enqueue(other, org, topics[1], model, "not-own-message", through=10**9, decline=True)
    with store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT action FROM audit_events WHERE actor_id=?", (other,))} >= {
            "bot.inference.decline"}
        assert db.execute("SELECT COUNT(*) FROM bot_runs WHERE requested_by=? AND prompt<>''", (other,)).fetchone()[0] == 0


def test_specific_members_admit_only_listed_current_members(fixture):
    store, owner, topics, _, _, bots = fixture
    org, owner_id = owner["organization_id"], owner["user_id"]
    listed, unlisted = person(fixture, "listed@example.org")["user_id"], person(fixture, "unlisted@example.org")["user_id"]
    viewer = person(fixture, "viewer@example.org", "viewer")["user_id"]
    runtime, bot, model = connected(fixture)
    sharing = BotSharing(bots)
    for members, message in (([], "at least one"), ([owner_id], "always use"), ([viewer], "can write"),
                             (["f" * 32], "can write"), ([listed, listed], "distinct")):
        with pytest.raises(StoreError, match=message):
            sharing.configure(owner_id, org, bot["id"], "members", members)
    with pytest.raises(StoreError, match="only for the specific-members"):
        sharing.configure(owner_id, org, bot["id"], "topic_writers", [listed])
    with pytest.raises(StoreError, match="choose only me"):
        sharing.configure(owner_id, org, bot["id"], "everyone")
    with pytest.raises(StoreError) as error:
        sharing.configure(listed, org, bot["id"], "topic_writers")  # Only the owner chooses for a personal bot.
    assert error.value.status == 404
    assert share(fixture, bot["id"], "members", [listed]) == {"policy": "members", "members": [listed]}
    assert routes(runtime, fixture, listed) == (True, True) and routes(runtime, fixture, unlisted) == (False, False)
    admitted = attempt(fixture, runtime, bot, listed, "listed")
    assert [run["status"] for run in admitted] == ["queued", "queued"]
    assert all(declined(run) for run in attempt(fixture, runtime, bot, unlisted, "unlisted"))
    with store.transaction() as db:
        assert db.execute("SELECT action FROM audit_events WHERE target_id=? ORDER BY sequence DESC LIMIT 1",
                          (bot["id"],)).fetchone()[0] == "bot.sharing"
    # Leaving the organization removes the member from the list; rejoining does not restore it.
    store.change_member(owner_id, org, listed, None)
    with store.transaction() as db:
        assert setting(db, bot["id"]) == {"policy": "members", "members": []}
        assert {row["status"] for row in db.execute("SELECT status FROM bot_runs WHERE requested_by=?", (listed,))} == {"queued"}
    claimed = runtime.claim(bot["token"])
    assert claimed is None or claimed["requested_by"] != listed
    with store.transaction() as db:
        assert {row["error_code"] for row in db.execute("SELECT error_code FROM bot_runs WHERE requested_by=?",
                                                        (listed,))} == {"access_changed"}


def test_everyone_who_can_write_in_its_topics_still_needs_topic_write_access(fixture):
    store, owner, topics, _, _, _ = fixture
    org = owner["organization_id"]
    writer = person(fixture, "writer@example.org")["user_id"]
    viewer = person(fixture, "viewer@example.org", "viewer")["user_id"]
    runtime, bot, model = connected(fixture)
    share(fixture, bot["id"])
    assert routes(runtime, fixture, writer) == (True, True)
    assert [run["status"] for run in attempt(fixture, runtime, bot, writer, "writer")] == ["queued", "queued"]
    assert runtime.enqueue(writer, org, topics[1], model, "goal-turn", prompt="Continue the goal")["status"] == "queued"
    with pytest.raises(StoreError) as error:
        runtime.enqueue(viewer, org, topics[1], model, "viewer", prompt="Not a writer")
    assert error.value.status == 403


def test_narrowing_withdraws_pending_runs_and_approvals_immediately(fixture):
    store, owner, topics, _, _, bots = fixture
    org, owner_id = owner["organization_id"], owner["user_id"]
    kept, dropped = person(fixture, "kept@example.org")["user_id"], person(fixture, "dropped@example.org")["user_id"]
    runtime, bot, model = connected(fixture, local_tools=True)
    share(fixture, bot["id"], "members", [kept, dropped])
    running = runtime.enqueue(dropped, org, topics[1], model, "dropped-running", prompt="Run locally")
    claimed = runtime.claim(bot["token"])
    assert claimed["id"] == running["id"]
    approval = runtime.request_execution(bot["token"], running["id"], claimed["lease"], ["python3", "-c", "print(1)"])
    queued = runtime.enqueue(dropped, org, topics[1], model, "dropped-queued", prompt="Later")
    other = runtime.enqueue(kept, org, topics[1], model, "kept-queued", prompt="Mine")
    owned = runtime.enqueue(owner_id, org, topics[1], model, "owner-queued", prompt="Owner")
    assert [row["id"] for row in runtime.approvals(owner_id, org)] == [approval["id"]]
    share(fixture, bot["id"], "members", [kept])
    runs = {run["id"]: run for run in runtime.listing(owner_id, org, topics[1])}
    assert runs[running["id"]]["status"] == "outcome_unknown" and runs[running["id"]]["error_code"] == "requester_not_permitted"
    assert declined(runs[queued["id"]])
    assert runs[other["id"]]["status"] == runs[owned["id"]]["status"] == "queued"
    assert runtime.approvals(owner_id, org) == []
    with pytest.raises(StoreError):
        runtime.approve(owner_id, org, approval["id"], True)
    with pytest.raises(StoreError):
        runtime.progress(bot["token"], running["id"], claimed["lease"], "thinking")
    with store.transaction() as db:
        assert db.execute("SELECT status FROM bot_execution_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == "denied"
        withdrawn = {row[0] for row in db.execute("SELECT target_id FROM audit_events WHERE action='bot.inference.withdraw' "
                                                  "AND actor_id=?", (owner_id,))}
    assert withdrawn == {running["id"], queued["id"]}
    # Widening never revives withdrawn work.
    share(fixture, bot["id"])
    assert {runtime.claim(bot["token"])["id"]} <= {other["id"], owned["id"]}


def test_self_approved_issues_still_require_the_sharing_setting(fixture):
    store, owner, _, _, _, _ = fixture
    developer, bot, other, run, args = trusted(fixture)
    org = owner["organization_id"]
    approval = developer.request(*args, "research/bench", "Title", "Body", "self-approval")
    assert [row["id"] for row in pending(developer, other, org)] == [approval["id"]]
    share(fixture, bot["id"], "owner")
    assert pending(developer, other, org) == []
    with pytest.raises(StoreError):
        developer.approve(other, org, approval["id"], True)
    with pytest.raises(StoreError, match=POLICY):
        developer.receipt(*args, approval["id"], consume=True)
    with store.transaction() as db:
        assert db.execute("SELECT status FROM bot_issue_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == "denied"
    # The developer grant is unchanged, but a new request from the member is not run.
    assert developer.runtime.bots.list(owner["user_id"], org, personal=True)[0]["developer"]["issue_self_approvers"] == [other]
    plain = store.post_message(other, org, run["topic_id"], "Another issue", "plain-after")
    assert declined(developer.runtime.enqueue(other, org, run["topic_id"], f"bot/{bot['id']}/default", "after",
                                              through=plain["sequence"], decline=True))


def test_workflow_bot_nodes_follow_the_owners_setting(installation):  # noqa: F811
    from reasoning_ledger.server.bot_store import BotStore

    env = installation
    invitation = env.store.invite(env.user, env.org, "bot-owner@example.org", "member", 3600)
    bot_owner = env.store.accept_invitation(invitation["token"], user_id=None, name="Bot owner",
                                            password="fixture password long")["user_id"]
    bots = BotStore(env.store)
    bot = bots.create(bot_owner, env.org, "Owner's Codex", [env.topic], ["messages:read", "messages:write"], personal=True)
    BotRuntime(bots).advertise(bot["token"], "codex", ["default"], True, False)
    node = llm_node()
    node["parameters"]["model"] = f"bot/{bot['id']}/default"
    with pytest.raises(StoreError, match=POLICY):
        env.start([node], client="workflow-not-shared-001")
    BotSharing(bots).configure(bot_owner, env.org, bot["id"], "members", [env.user])
    assert env.start([node], client="workflow-shared-0001")["status"] == "running"


@pytest.mark.parametrize("runner_flag", [True, False])
def test_existing_bots_keep_their_runner_sharing_when_migrated(fixture, runner_flag):
    store, owner, topics, _, _, _ = fixture
    other = person(fixture, "other@example.org")["user_id"]
    runtime, bot, _ = connected(fixture, runner_flag=runner_flag)
    with store.transaction(write=True) as db:
        db.execute("DROP TABLE bot_invokers")
        db.execute("DROP TABLE bot_sharing")
        # Later migrations are undone as well, so the upgrade replays them after bot sharing.
        db.execute("ALTER TABLE workflow_runs DROP COLUMN capacity_released")
        db.execute("DROP INDEX messages_reply_to")
        db.execute("ALTER TABLE messages DROP COLUMN reply_to")
        db.execute(f"PRAGMA user_version={MIGRATIONS.index(BOT_SHARING_SCHEMA)}")
    migrated = Store(store.data_dir)
    with migrated.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT allow_other_users FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone()[0] == int(runner_flag)
        expected = {"policy": "topic_writers", "members": []} if runner_flag else {"policy": "owner", "members": []}
        assert setting(db, bot["id"]) == expected
        if runner_flag:
            assert db.execute("SELECT updated_by FROM bot_sharing WHERE bot_id=?", (bot["id"],)).fetchone()[0] == owner["user_id"]
    runs = attempt(fixture, runtime, bot, other, "after-migration")
    assert all(not declined(run) for run in runs) if runner_flag else all(declined(run) for run in runs)


def test_sharing_routes_for_owners_and_administrators(fixture, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    store, owner, topics, _, _, bots = fixture
    org = owner["organization_id"]
    member = person(fixture, "member@example.org")
    personal = bots.create(member["user_id"], org, "Member bot", topics[1:2], ["messages:read", "messages:write"], personal=True)
    administered = bots.create(owner["user_id"], org, "Organization bot", topics[1:2], ["messages:read", "messages:write"])
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))
    root = f"/api/organizations/{org}"

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            def session(account):
                client.cookies.set("__Host-reasoning_session", account["access_token"])
                return {"origin": "https://server.example", "x-csrf-token": account["csrf_token"]}

            headers = session(member)
            body = {"policy": "members", "members": [owner["user_id"]]}
            response = await client.put(root + f"/my-bots/{personal['id']}/sharing", json=body, headers=headers)
            assert response.status_code == 200 and response.json() == body
            listed = (await client.get(root + "/my-bots")).json()
            assert listed[0]["sharing"] == body
            assert (await client.put(root + f"/my-bots/{personal['id']}/sharing", json={"policy": "anyone"},
                                     headers=headers)).status_code == 422
            assert (await client.put(root + f"/bots/{administered['id']}/sharing", json={"policy": "topic_writers"},
                                     headers=headers)).status_code == 403
            headers = session(owner)
            # Administrators manage organization bots, but only an owner chooses for a personal bot.
            assert (await client.put(root + f"/bots/{personal['id']}/sharing", json={"policy": "owner"},
                                     headers=headers)).status_code == 403
            response = await client.put(root + f"/bots/{administered['id']}/sharing", json={"policy": "topic_writers"},
                                        headers=headers)
            assert response.status_code == 200 and response.json() == {"policy": "topic_writers", "members": []}
            assert {bot["id"]: bot["sharing"]["policy"] for bot in (await client.get(root + "/bots")).json()} == {
                personal["id"]: "members", administered["id"]: "topic_writers"}

    asyncio.run(check())
