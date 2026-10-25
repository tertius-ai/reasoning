"""Human-only feedback, cross-model history, and cutoff-safe beta branches."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_public_server as server_tests
from fastapi.testclient import TestClient
from test_public_inference import MODELS, QUOTES, enqueue, prepare
from test_public_server import invited, structure

from reasoning_ledger.server import store as store_module
from reasoning_ledger.server.chat_feedback import ChatFeedback
from reasoning_ledger.server.feedback_api import install_feedback_routes
from reasoning_ledger.server.feedback_schema import SCHEMA
from reasoning_ledger.server.feedback_single_schema import SCHEMA as SINGLE_REACTION_SCHEMA
from reasoning_ledger.server.openrouter import Usage
from reasoning_ledger.server.reaction_emoji_schema import EMOJIS
from reasoning_ledger.server.reaction_emoji_schema import SCHEMA as REACTION_EMOJI_SCHEMA
from reasoning_ledger.server.research_context import ResearchContext
from reasoning_ledger.server.schema_operations import _reaction_check
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.topic_forks import TopicForks, feedback_export


@pytest.fixture(autouse=True)
def feedback_schema(monkeypatch):
    # Also usable before central route/migration integration in an isolated worktree.
    if SCHEMA not in store_module.MIGRATIONS:
        monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS + (SCHEMA,))


@pytest.fixture
def installation(tmp_path):
    base_installation = server_tests.installation.__wrapped__(tmp_path)
    _, store, client, _ = base_installation
    if not any(route.path.endswith("/model-feedback") for route in client.app.routes):
        route = next(route for route in client.app.routes if route.path == "/api/auth/me")
        install_feedback_routes(client.app, store, route.dependant.dependencies[0].call)
    return base_installation


def response(state, owner, topic, text, model=MODELS[0], client="response"):
    enqueue(state, owner, topic, models=[model], client=client)
    run = state.claim()
    state.progress(run, text, None)
    state.finish(run, "succeeded", usage=Usage(3, 4, 5))
    message = state.store.messages(owner["user_id"], owner["organization_id"], topic)[-1]
    assert message["inference_run_id"] == run["id"]
    return run, message


def test_reactions_toggle_idempotently_and_use_recorded_model(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    run, message = response(state, owner, topic, "Useful result", MODELS[1])
    path = f"/api/organizations/{org}/topics/{topic}/reactions/message/{message['id']}"
    for _ in range(2):
        result = client.put(path, json={"emoji": "up", "active": True})
        assert result.status_code == 200, result.text
        assert result.json()["counts"]["up"] == 1
        assert result.json()["reactors"] == {**dict.fromkeys(EMOJIS, []), "up": [{"id": user, "name": "Owner"}]}
    same = client.get(f"/api/organizations/{org}/topics/{topic}/reactions/run/{run['id']}").json()
    assert same == result.json()
    assert client.get(f"/api/organizations/{org}/model-feedback").json()["models"] == [
        {"model": MODELS[1], **dict.fromkeys(EMOJIS, 0), "up": 1}
    ]
    assert client.put(path, json={"emoji": "up", "active": False}).json()["counts"]["up"] == 0
    human = state.store.messages(user, org, topic)[0]
    feedback = ChatFeedback(state.store)
    assert feedback.set(user, org, topic, "message", human["id"], "upside_down", True)["counts"]["upside_down"] == 1
    assert feedback.totals(user, org)["models"] == []
    assert client.put(path, json={"emoji": "up", "active": "yes"}).status_code == 422


def test_model_replies_take_only_thumbs_and_the_upside_down_smile(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    run, message = response(state, owner, topic, "A model's answer")
    message_path = f"/api/organizations/{org}/topics/{topic}/reactions/message/{message['id']}"
    run_path = f"/api/organizations/{org}/topics/{topic}/reactions/run/{run['id']}"
    for path in (message_path, run_path):
        refused = client.put(path, json={"emoji": "heart", "active": True})
        assert refused.status_code == 400 and "upside-down smile" in refused.text
    for emoji in ("up", "down", "upside_down"):
        assert client.put(message_path, json={"emoji": emoji, "active": True}).json()["mine"] == [emoji]
    # A reaction left before the restriction stays readable and can still be removed.
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE chat_reactions SET emoji='heart' WHERE organization_id=? AND user_id=?", (org, user))
    assert client.get(run_path).json()["mine"] == ["heart"]
    assert client.put(run_path, json={"emoji": "heart", "active": False}).json()["mine"] == []
    # A person's message keeps every reaction.
    human = state.store.messages(user, org, topic)[0]
    assert ChatFeedback(state.store).set(user, org, topic, "message", human["id"], "heart", True)["mine"] == ["heart"]


def test_reactor_identities_are_bounded_and_do_not_expose_contact_information(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]
    message = store.post_message(user, org, topic["id"], "Human feedback", "reactor-identity-message")
    feedback = ChatFeedback(store)
    people = {user: "Owner"}
    feedback.set(user, org, topic["id"], "message", message["id"], "up", True)
    for index in range(4):
        _, _, member = invited(installation, email=f"reactor-{index}@example.org")
        people[member["user_id"]] = "Member"
        feedback.set(member["user_id"], org, topic["id"], "message", message["id"], "up", True)
    path = f"/api/organizations/{org}/topics/{topic['id']}/reactions/message/{message['id']}"
    result = client.get(path).json()
    assert result["counts"]["up"] == 5 and len(result["reactors"]["up"]) == 3
    assert all(actor == {"id": actor["id"], "name": people[actor["id"]]} for actor in result["reactors"]["up"])
    assert "example.org" not in json.dumps(result)


def test_one_reaction_replaces_prior_choice_across_canonical_output_aliases(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    run, message = response(state, owner, topic, "One physical result")
    feedback = ChatFeedback(state.store)
    feedback.set(user, org, topic, "message", message["id"], "up", True)
    changed = feedback.set(user, org, topic, "run", run["id"], "down", True)
    assert changed["mine"] == ["down"]
    assert changed["counts"] == {**dict.fromkeys(EMOJIS, 0), "down": 1}
    assert feedback.totals(user, org)["models"] == [{"model": MODELS[0], **dict.fromkeys(EMOJIS, 0), "down": 1}]
    # A delayed removal for the old choice must not remove its replacement.
    assert feedback.set(user, org, topic, "message", message["id"], "up", False)["mine"] == ["down"]
    assert feedback.set(user, org, topic, "message", message["id"], "down", False)["mine"] == []


def test_concurrent_reaction_changes_leave_exactly_one_choice(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    message = store.post_message(user, org, topic, "Concurrent human feedback", "concurrent-feedback")
    feedback = ChatFeedback(store)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(
            lambda emoji: feedback.set(user, org, topic, "message", message["id"], emoji, True), ["up", "down", "upside_down"] * 4
        ))
    result = feedback.read(user, org, topic, "message", message["id"])
    assert len(result["mine"]) == 1 and sum(result["counts"].values()) == 1


def test_reaction_migration_keeps_latest_choice_and_deterministic_ties(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    first = store.post_message(user, org, topic, "Latest choice", "latest-reaction")
    second = store.post_message(user, org, topic, "Tied choices", "tie-reaction")
    with store.transaction(write=True) as db:
        db.execute("DROP INDEX chat_reaction_person")
        for target, choices in ((first["id"], [("up", 1), ("down", 2), ("upside_down", 3)]),
                                (second["id"], [("up", 4), ("down", 4)])):
            for emoji, timestamp in choices:
                db.execute("INSERT INTO chat_reactions VALUES(?,?,?,?,?,?,?)",
                           (org, topic, "message:" + target, user, emoji, None, timestamp))
        for statement in SINGLE_REACTION_SCHEMA:
            db.execute(statement)
    feedback = ChatFeedback(store)
    assert feedback.read(user, org, topic, "message", first["id"])["mine"] == ["upside_down"]
    assert feedback.read(user, org, topic, "message", second["id"])["mine"] == ["up"]


def test_reactions_require_current_topic_scope_and_human_write_access(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    public = structure(client, org)[2][0]
    private_project = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "secret", private_project["id"])
    private = store.create_entity(user, org, "topics", "private", channel["id"], "alpha")
    message = store.post_message(user, org, private["id"], "Secret", "secret")
    feedback = ChatFeedback(store)
    feedback.set(user, org, private["id"], "message", message["id"], "up", True)
    _, _, member = invited(installation)
    with pytest.raises(StoreError) as denied:
        feedback.read(member["user_id"], org, private["id"], "message", message["id"])
    assert denied.value.status in (403, 404)
    with pytest.raises(StoreError):
        feedback.set(user, org, public["id"], "message", message["id"], "down", True)
    _, _, viewer = invited(installation, role="viewer", email="viewer@example.org")
    public_message = store.post_message(user, org, public["id"], "Public", "public")
    with pytest.raises(StoreError) as denied:
        feedback.set(viewer["user_id"], org, public["id"], "message", public_message["id"], "up", True)
    assert denied.value.status == 403
    with store.transaction(write=True) as db:
        db.execute("UPDATE users SET account_kind='bot' WHERE id=?", (member["user_id"],))
    with pytest.raises(StoreError) as denied:
        feedback.set(member["user_id"], org, public["id"], "message", public_message["id"], "up", True)
    assert denied.value.status in (403, 404)


def test_message_page_reactions_are_one_scoped_read(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    run, answer = response(state, owner, topic, "Useful result", MODELS[1])
    human = state.store.messages(user, org, topic)[0]
    single = f"/api/organizations/{org}/topics/{topic}/reactions/message/"
    assert client.put(single + answer["id"], json={"emoji": "up", "active": True}).status_code == 200
    batch = client.get(f"/api/organizations/{org}/topics/{topic}/reactions/messages",
                       params={"ids": ",".join([human["id"], answer["id"], "0" * 32, answer["id"]])})
    assert batch.status_code == 200, batch.text
    assert batch.headers["cache-control"] == "no-store"
    assert set(batch.json()) == {human["id"], answer["id"]}, "unknown messages have no entry"
    for message in (human, answer):
        assert batch.json()[message["id"]] == client.get(single + message["id"]).json()
    assert batch.json()[answer["id"]]["target_key"] == "run:" + run["id"]
    too_many = ",".join(f"{index:032x}" for index in range(201))
    assert client.get(f"/api/organizations/{org}/topics/{topic}/reactions/messages", params={"ids": too_many}).status_code == 400
    private_project = state.store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = state.store.create_entity(user, org, "channels", "secret", private_project["id"])
    private = state.store.create_entity(user, org, "topics", "private", channel["id"], "alpha")
    secret = state.store.post_message(user, org, private["id"], "Secret", "secret")
    _, _, member = invited(installation)
    with pytest.raises(StoreError) as denied:
        ChatFeedback(state.store).read_messages(member["user_id"], org, private["id"], [secret["id"]])
    assert denied.value.status in (403, 404)


def test_private_model_feedback_is_filtered_for_each_reader(installation):
    _, state, client, owner, topics = prepare(installation)
    org, user = owner["organization_id"], owner["user_id"]
    project = state.store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = state.store.create_entity(user, org, "channels", "private", project["id"])
    topic = state.store.create_entity(user, org, "topics", "private", channel["id"], "beta", MODELS[0])
    state.store.post_message(user, org, topic["id"], "Private request", "private-request")
    _, msg = response(state, owner, topic["id"], "Private result")
    feedback = ChatFeedback(state.store)
    feedback.set(user, org, topic["id"], "message", msg["id"], "down", True)
    _, _, member = invited(installation)
    assert feedback.totals(member["user_id"], org)["models"] == []
    assert feedback.totals(user, org, project["id"])["models"][0]["down"] == 1
    with pytest.raises(StoreError):
        feedback.totals(member["user_id"], org, project["id"])


def test_fork_retains_cross_model_history_settings_and_excludes_future_state(installation):
    _, state, client, owner, topics = prepare(installation)
    store, org, user, topic = state.store, owner["organization_id"], owner["user_id"], topics[1]["id"]
    _, first = response(state, owner, topic, "First model answer", MODELS[0], "first")
    store.post_message(user, org, topic, "Follow up", "follow")
    _, cutoff = response(state, owner, topic, "Second model answer", MODELS[1], "second")
    store.post_message(user, org, topic, "FUTURE_SECRET", "future")
    enqueue(state, owner, topic, models=[MODELS[0]], client="live")
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO research_topic_settings(organization_id,topic_id,tools_json,context_policy_json,tasks_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(organization_id,topic_id) DO UPDATE SET context_policy_json=excluded.context_policy_json,"
            "tasks_json=excluded.tasks_json",
            (org, topic, '["knowledge"]', '{"target_percent":70}', '[{"text":"FUTURE_TASK"}]'),
        )
        db.execute(
            "INSERT INTO research_memories(organization_id,topic_id,revision,through_sequence,body_json,source_digest,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (org, topic, 1, cutoff["sequence"] + 1, '{"secret":"FUTURE_SECRET"}', "test", 1),
        )
    path = f"/api/organizations/{org}/topics/{topic}/forks"
    body = {"message_id": cutoff["id"], "name": "Alternate approach", "client_id": "fork-1"}
    result = client.post(path, json=body)
    assert result.status_code == 201, result.text
    fork = result.json()["topic"]["id"]
    assert client.post(path, json=body).json() == result.json()
    assert client.post(path, json={**body, "name": "Changed"}).status_code == 409
    copied = store.messages(user, org, fork)
    originals = store.messages(user, org, topic)
    assert [row["content"] for row in copied] == [row["content"] for row in originals if row["sequence"] <= cutoff["sequence"]]
    assert [row["assistant_model"] for row in copied if row["author_kind"] == "assistant"] == MODELS
    assert all(row["inference_run_id"] is None for row in copied)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM research_memories WHERE topic_id=?", (fork,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM llm_requests WHERE topic_id=?", (fork,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_goals WHERE topic_id=?", (fork,)).fetchone()[0] == 0
        settings = db.execute("SELECT * FROM research_topic_settings WHERE topic_id=?", (fork,)).fetchone()
        assert json.loads(settings["context_policy_json"]) == {"target_percent": 70}
        assert json.loads(settings["tasks_json"]) == []
        assert db.execute(
            "SELECT count(*) FROM fork_message_sources WHERE message_id IN (SELECT id FROM messages WHERE topic_id=?)", (fork,)
        ).fetchone()[0] == len(copied)
        compiled, _ = ResearchContext(store).compile(db, org, fork, copied[-1]["sequence"], [QUOTES[1]], user=user)
    wire = json.dumps(compiled)
    assert "First model answer" in wire and "Second model answer" in wire and "FUTURE_SECRET" not in wire
    assert MODELS[0] in wire and MODELS[1] in wire
    feedback = ChatFeedback(store)
    feedback.set(user, org, fork, "message", copied[-1]["id"], "up", True)
    assert feedback.totals(user, org)["models"][0]["model"] == MODELS[1]
    assert first["sequence"] < cutoff["sequence"]


def test_fork_authorization_kind_and_message_cutoff_are_enforced(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topics = structure(client, org)[2]
    alpha = store.post_message(user, org, topics[0]["id"], "alpha", "alpha")
    beta = store.post_message(user, org, topics[1]["id"], "beta", "beta")
    forks = TopicForks(store)
    with pytest.raises(StoreError):
        forks.create(user, org, topics[0]["id"], alpha["id"], "No", "alpha-fork")
    with pytest.raises(StoreError):
        forks.create(user, org, topics[1]["id"], alpha["id"], "No", "wrong-cutoff")
    _, _, viewer = invited(installation, role="viewer")
    with pytest.raises(StoreError) as denied:
        forks.create(viewer["user_id"], org, topics[1]["id"], beta["id"], "No", "viewer-fork")
    assert denied.value.status == 403


def test_gamma_node_and_chat_feedback_share_the_retained_actual_model(installation):
    from reasoning_ledger.server.compute_store import ComputeStore
    from reasoning_ledger.server.workflow_runs import WorkflowRuns
    from reasoning_ledger.server.workflow_store import WorkflowStore

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[2]["id"]
    definitions = WorkflowStore(state.store)
    workflows = WorkflowRuns(definitions, ComputeStore(state.store, {}), state)
    node = {
        "id": "answer",
        "kind": "llm",
        "depends_on": [],
        "inputs": {},
        "parameters": {"model": MODELS[1], "prompt": "Compare retained answers", "max_completion_tokens": 128},
    }
    definition = definitions.create(user, org, topic, "gamma-reaction-definition", {"format": 1, "name": "Compare", "nodes": [node]})
    run = workflows.start(user, org, definition["workflow_id"], definition["version"], "gamma-reaction-run")
    action = workflows.advance(run["id"])
    workflows.advance(run["id"], quote_action=action, quotes=[QUOTES[1]])
    child = state.claim()
    state.progress(child, "A retained gamma answer", None)
    state.finish(child, "succeeded", usage=Usage(3, 4, 5))
    workflows.advance(run["id"])
    message = state.store.messages(user, org, topic)[-1]
    feedback = ChatFeedback(state.store)
    node_vote = feedback.set(user, org, topic, "node", run["id"], "up", True, "answer")
    assert feedback.read(user, org, topic, "message", message["id"]) == node_vote
    assert feedback.totals(user, org)["models"] == [{"model": MODELS[1], **dict.fromkeys(EMOJIS, 0), "up": 1}]
    with pytest.raises(StoreError):
        feedback.set(user, org, topics[1]["id"], "node", run["id"], "up", True, "answer")
    with pytest.raises(StoreError):
        feedback.set(user, org, topic, "node", run["id"], "up", True, "missing")


def test_bot_feedback_and_nested_fork_keep_recorded_model_without_new_execution(installation):
    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.bot_store import BotStore

    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][1]["id"]
    bots = BotStore(store)
    bot = bots.create(user, org, "Local researcher", [topic], ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["configured-model"], False, False)
    human = store.post_message(user, org, topic, "Research request", "bot-request")
    model = f"bot/{bot['id']}/configured-model"
    run = runtime.enqueue(user, org, topic, model, "bot-feedback-request", through=human["sequence"])
    claimed = runtime.claim(bot["token"])
    runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "Recorded bot answer")
    message = store.messages(user, org, topic)[-1]
    feedback = ChatFeedback(store)
    first = feedback.set(user, org, topic, "message", message["id"], "up", True)
    assert feedback.read(user, org, topic, "bot", run["id"]) == first
    forged = bots.post(bot["token"], topic, "Not the actual model output", f"bot-run-{run['id']}-123")
    feedback.set(user, org, topic, "message", forged["id"], "upside_down", True)
    assert feedback.totals(user, org)["models"][0]["upside_down"] == 0
    forks = TopicForks(store)
    fork = forks.create(user, org, topic, message["id"], "Bot branch", "bot-branch")["topic"]["id"]
    copied = store.messages(user, org, fork)[-1]
    nested = forks.create(user, org, fork, copied["id"], "Nested branch", "nested-branch")["topic"]["id"]
    nested_message = store.messages(user, org, nested)[-1]
    feedback.set(user, org, nested, "message", nested_message["id"], "down", True)
    assert feedback.totals(user, org)["models"] == [{"model": "configured-model", **dict.fromkeys(EMOJIS, 0), "up": 1, "down": 1}]
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM bot_runs").fetchone()[0] == 1
        exported = feedback_export(db, org, nested)
    assert exported["fork"]["source_topic_id"] == fork
    assert exported["message_sources"][-1]["source_model"] == "configured-model"
    assert exported["reactions"][0]["model"] == "configured-model"


def test_fork_inherits_private_project_access_and_http_writes_require_csrf(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    private = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "Private", private["id"])
    topic = store.create_entity(user, org, "topics", "Private beta", channel["id"], "beta")["id"]
    message = store.post_message(user, org, topic, "Private history", "private-history")
    forks = TopicForks(store)
    fork = forks.create(user, org, topic, message["id"], "Private branch", "private-branch")["topic"]["id"]
    _, _, member = invited(installation)
    assert store.messages(user, org, fork)[0]["content"] == "Private history"
    with pytest.raises(StoreError):
        store.messages(member["user_id"], org, fork)
    with pytest.raises(StoreError):
        forks.create(member["user_id"], org, topic, message["id"], "Unauthorized", "unauthorized-fork")
    cookie = TestClient(client.app, base_url=server_tests.PUBLIC_URL)
    login = cookie.post("/api/auth/login", json={"email": "owner@example.org", "password": server_tests.PASSWORD})
    assert login.status_code == 200
    path = f"/api/organizations/{org}/topics/{topic}"
    reaction = path + "/reactions/message/" + message["id"]
    assert cookie.put(reaction, json={"emoji": "up", "active": True}).status_code == 403
    assert cookie.post(path + "/forks", json={"message_id": message["id"], "name": "No csrf", "client_id": "no-csrf"}).status_code == 403
    cookie.headers["Origin"] = server_tests.PUBLIC_URL
    cookie.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert cookie.put(reaction, json={"emoji": "up", "active": True}).status_code == 200
    assert cookie.get(f"/api/organizations/{org}/topics/{fork}/fork-origin").json()["source_message_id"] == message["id"]


def test_more_reactions_are_accepted_and_older_installations_widen_the_check(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    message = store.post_message(user, org, topic, "Many reactions", "many-reactions")
    feedback = ChatFeedback(store)
    for emoji in EMOJIS:
        assert feedback.set(user, org, topic, "message", message["id"], emoji, True)["mine"] == [emoji]
    path = f"/api/organizations/{org}/topics/{topic}/reactions/message/{message['id']}"
    assert client.put(path, json={"emoji": "unicorn", "active": True}).status_code == 422
    # An installation from before this migration keeps its reactions and accepts the new choices after the upgrade.
    feedback.set(user, org, topic, "message", message["id"], "upside_down", True)
    with store.transaction(write=True) as db:
        _reaction_check(db, " CHECK(emoji IN ('up','down','upside_down'))")
        db.execute("DROP TABLE feedback_reports")
        db.execute("DROP TABLE direct_media_chunks")
        db.execute("DROP TABLE direct_media")
        db.execute("DROP INDEX direct_message_reply_to")
        db.execute("ALTER TABLE direct_messages DROP COLUMN reply_to")
        db.execute("DROP TABLE research_account_versions")
        db.execute("DROP TABLE research_accounts")
        db.execute("DROP TABLE direct_reaction_changes")
        db.execute("DROP TABLE direct_message_reactions")
        db.execute("ALTER TABLE workflow_runs DROP COLUMN capacity_released")
        db.execute("ALTER TABLE research_media DROP COLUMN extraction_json")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE chat_reactions SET emoji='heart'")
        db.execute(f"PRAGMA user_version={store_module.MIGRATIONS.index(REACTION_EMOJI_SCHEMA)}")
    upgraded = store_module.Store(store.data_dir)
    upgraded_feedback = ChatFeedback(upgraded)
    assert upgraded_feedback.read(user, org, topic, "message", message["id"])["mine"] == ["upside_down"]
    assert upgraded_feedback.set(user, org, topic, "message", message["id"], "heart", True)["mine"] == ["heart"]
    with upgraded.transaction() as db:
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE tbl_name='chat_reactions' AND type='index'")}
        assert {"chat_reaction_model", "chat_reaction_person"} <= indexes
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE chat_reactions SET emoji='unicorn'")
