"""Direct conversations with your own bot: private to you, answered by a bot run that belongs to the conversation."""

from test_public_direct_messages import conversation, listing, people, post  # noqa: F401
from test_public_server import installation, invited  # noqa: F401

from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.direct_messages import DirectMessages
from reasoning_ledger.server.store import StoreError


def own_bot(people, *, owner="first_id"):  # noqa: F811
    bots = BotStore(people["store"])
    bot = bots.create(people[owner], people["org"], "Laptop", [], ["messages:read", "messages:write"], personal=True, public_access=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["gpt", "mini"], False, False, {"efforts": {"gpt": ["low", "high"]}})
    return bot, runtime


def test_you_message_your_own_bot_and_its_reply_arrives_in_the_conversation(people):  # noqa: F811
    root, store = people["root"], people["store"]
    bot, runtime = own_bot(people)
    opened = people["first"].post(root + "/direct-conversations", json={"user_id": bot["id"]})
    assert opened.status_code == 201, opened.text
    direct = opened.json()
    assert direct["other_user"] == {"id": bot["id"], "name": "Laptop", "kind": "bot"} and direct["other_member"] is True
    path = root + f"/direct-conversations/{direct['id']}"
    body = {"content": "Does the census still run?", "client_id": "bot-dm-1", "bot_model": "gpt", "effort": "high"}
    sent = people["first"].post(path + "/messages", json=body)
    assert sent.status_code == 201, sent.text
    [run] = people["first"].get(path + "/bot-runs").json()
    assert run["status"] == "queued" and run["model"] == "gpt" and run["effort"] == "high"
    assert run["direct_conversation_id"] == direct["id"]
    # A replayed send returns the same message and queues nothing new.
    assert people["first"].post(path + "/messages", json=body).json()["id"] == sent.json()["id"]
    assert len(people["first"].get(path + "/bot-runs").json()) == 1
    # The runner gets the conversation as its history and a private workspace named after the conversation.
    claimed = runtime.claim(bot["token"])
    assert claimed["id"] == run["id"] and claimed["prompt"] == "Does the census still run?"
    assert {claimed[key] for key in ("project_id", "channel_id", "topic_id")} == {direct["id"]}
    assert [entry["text"] for entry in claimed["context"]] == ["Does the census still run?"]
    # No topic, so no Reasoning research tools; the scope names the conversation, as the claim does.
    context = BotTools(runtime).context(bot["token"], run["id"], claimed["lease"])
    assert context["tools"] == [] and context["scope"]["topic_id"] == direct["id"] and context["scope"]["topic_kind"] == "direct"
    runtime.progress(bot["token"], run["id"], claimed["lease"], "using_tools")
    finished = runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "It runs in 41 seconds.")
    assert finished["status"] == "succeeded"
    messages = people["first"].get(path + "/messages").json()
    assert [(message["author_kind"], message["content"]) for message in messages] == [
        ("human", "Does the census still run?"), ("bot", "It runs in 41 seconds.")]
    assert listing(people, "first")[0]["unread_count"] == 1
    # A bot's reply takes only the three model reactions.
    reply = messages[-1]["id"]
    assert people["first"].put(path + f"/messages/{reply}/reactions", json={"emoji": "heart", "active": True}).status_code == 400
    assert people["first"].put(path + f"/messages/{reply}/reactions", json={"emoji": "upside_down", "active": True}).status_code == 200
    # Nobody else can open the bot conversation, read it or see its runs; administrators included.
    for other in ("second", "admin", "owner"):
        assert people[other].get(path + "/messages").status_code == 404
        assert people[other].get(path + "/bot-runs").status_code == 404
    assert people["second"].post(root + "/direct-conversations", json={"user_id": bot["id"]}).status_code == 404
    assert store.presence(people["second_id"], people["org"])["bots"][0].get("efforts") is None
    assert store.presence(people["first_id"], people["org"])["bots"][0]["efforts"] == {"gpt": ["low", "high"]}


def test_an_offline_bot_queues_and_an_unadvertised_model_or_effort_is_refused(people):  # noqa: F811
    root = people["root"]
    bot, runtime = own_bot(people)
    direct = people["first"].post(root + "/direct-conversations", json={"user_id": bot["id"]}).json()
    path = root + f"/direct-conversations/{direct['id']}"
    with people["store"].transaction(write=True) as db:
        db.execute("UPDATE bot_runtimes SET last_seen_at=0 WHERE bot_id=?", (bot["id"],))
    queued = people["first"].post(path + "/messages", json={"content": "Summarize the ledger", "client_id": "offline-1"})
    assert queued.status_code == 201, queued.text
    [run] = people["first"].get(path + "/bot-runs").json()
    assert run["status"] == "queued" and run["model"] == "gpt"
    other_model = {"content": "x", "client_id": "bad-model", "bot_model": "other"}
    assert people["first"].post(path + "/messages", json=other_model).status_code == 400
    other_effort = {"content": "x", "client_id": "bad-effort", "bot_model": "mini", "effort": "high"}
    assert people["first"].post(path + "/messages", json=other_effort).status_code == 400
    # Only a participant can stop the run; stopping a queued run cancels it.
    try:
        runtime.stop(people["second_id"], people["org"], run["id"])
    except StoreError as error:
        assert error.status == 404
    else:
        raise AssertionError("another member must not stop a run in someone else's bot conversation")
    assert runtime.stop(people["first_id"], people["org"], run["id"])["status"] == "cancelled"


def test_a_revoked_bot_cannot_be_messaged(people):  # noqa: F811
    root = people["root"]
    bot, _ = own_bot(people)
    direct = people["first"].post(root + "/direct-conversations", json={"user_id": bot["id"]}).json()
    path = root + f"/direct-conversations/{direct['id']}/messages"
    assert people["first"].post(path, json={"content": "Hi", "client_id": "revoke-1"}).status_code == 201
    BotStore(people["store"]).revoke(people["first_id"], people["org"], bot["id"], personal=True)
    assert people["first"].post(path, json={"content": "Hi?", "client_id": "revoke-2"}).status_code == 403
    assert listing(people, "first")[0]["other_member"] is False
    with people["store"].transaction() as db:
        assert DirectMessages._own_bot(db, people["org"], people["first_id"], bot["id"]) is None
