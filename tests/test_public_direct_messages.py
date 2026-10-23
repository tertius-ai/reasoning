"""Direct conversations: participant-only access, unread state, mute, block and isolation from topics and bots."""

from __future__ import annotations

import httpx
import pytest
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.direct_messages import SENDER_BLOCKED, UNAVAILABLE
from reasoning_ledger.server.store import Store, StoreError

SECRET = "Private note about the referee report"


@pytest.fixture
def people(installation):
    """Two members talk directly; the owner and an administrator are organization-wide but not participants."""
    config, store, owner_client, owner = installation
    _, first, first_identity = invited(installation, email="first@example.org")
    _, second, second_identity = invited(installation, email="second@example.org")
    _, admin, admin_identity = invited(installation, role="admin", email="admin@example.org")
    org = owner["organization_id"]
    root = f"/api/organizations/{org}"
    return {
        "config": config, "store": store, "org": org, "root": root,
        "owner": owner_client, "owner_id": owner["user_id"],
        "first": first, "first_id": first_identity["user_id"],
        "second": second, "second_id": second_identity["user_id"],
        "admin": admin, "admin_id": admin_identity["user_id"],
    }


def conversation(people, sender="first", recipient="second_id"):
    response = people[sender].post(people["root"] + "/direct-conversations", json={"user_id": people[recipient]})
    assert response.status_code in {200, 201}, response.text
    return response.json()


def post(people, conversation_id, content, client_id, sender="first"):
    return people[sender].post(people["root"] + f"/direct-conversations/{conversation_id}/messages",
                               json={"content": content, "client_id": client_id})


def listing(people, who):
    response = people[who].get(people["root"] + "/direct-conversations")
    assert response.status_code == 200, response.text
    return response.json()


def test_reactions_are_private_idempotent_replaceable_and_incrementally_visible(people):
    direct = conversation(people)
    message = post(people, direct["id"], SECRET, "reactions").json()
    root = people["root"] + f"/direct-conversations/{direct['id']}"
    path = root + f"/messages/{message['id']}/reactions"
    first, second = people["first"], people["second"]
    assert second.put(path, json={"emoji": "heart", "active": True}).json()["mine"] == ["heart"]
    update = first.get(root + "/message-updates").json()
    assert update["reaction_after"] > 0 and update["messages"][0]["reactions"]["counts"] == {"heart": 1}
    assert update["messages"][0]["reactions"]["mine"] == []
    assert second.put(path, json={"emoji": "heart", "active": True}).status_code == 200
    assert first.get(root + "/message-updates", params={"reaction_after": update["reaction_after"]}).json()["messages"] == []
    for outsider in (people["owner"], people["admin"]):
        assert outsider.get(path).status_code == 404
        assert outsider.put(path, json={"emoji": "up", "active": True}).status_code == 404
    assert second.put(path, json={"emoji": "eyes", "active": True}).json()["counts"] == {"eyes": 1}
    assert second.put(path, json={"emoji": "heart", "active": False}).json()["mine"] == ["eyes"]
    assert first.put(path, json={"emoji": "eyes", "active": True}).json()["counts"] == {"eyes": 2}
    latest = first.get(root + "/message-updates").json()["reaction_after"]
    second.put(path, json={"emoji": "eyes", "active": False}).raise_for_status()
    removed = first.get(root + "/message-updates", params={"reaction_after": latest}).json()
    assert removed["reaction_after"] > latest and removed["messages"][0]["reactions"]["counts"] == {"eyes": 1}
    assert second.put(path, json={"emoji": "not-an-emoji", "active": True}).status_code == 400
    assert second.put(people["root"] + "/direct-blocks/" + people["first_id"], json={}).status_code == 204
    assert first.put(path, json={"emoji": "up", "active": True}).status_code == 403
    assert second.put(path, json={"emoji": "up", "active": True}).status_code == 403
    assert second.get(path).status_code == 200
    with people["store"].transaction(write=True) as db:
        db.execute("UPDATE memberships SET role='viewer' WHERE organization_id=? AND user_id=?", (people["org"], people["first_id"]))
    assert first.put(path, json={"emoji": "up", "active": True}).status_code == 403
    assert first.get(path).status_code == 200


def test_participants_exchange_messages_with_unread_state_and_edits(people):
    root = people["root"]
    created = people["first"].post(root + "/direct-conversations", json={"user_id": people["second_id"]})
    assert created.status_code == 201
    direct = created.json()
    assert direct["other_user"] == {"id": people["second_id"], "name": "Member"} and direct["unread_count"] == 0
    again = people["second"].post(root + "/direct-conversations", json={"user_id": people["first_id"]})
    assert again.status_code == 200 and again.json()["id"] == direct["id"]
    # An empty conversation is listed only for the person who opened it.
    assert [item["id"] for item in listing(people, "first")] == [direct["id"]]
    assert listing(people, "second") == []

    path = root + f"/direct-conversations/{direct['id']}"
    first = post(people, direct["id"], SECRET, "dm-1")
    assert first.status_code == 201
    message = first.json()
    assert message["author_kind"] == "human" and message["conversation_id"] == direct["id"]
    assert post(people, direct["id"], SECRET, "dm-1").json() == message
    assert post(people, direct["id"], "Different", "dm-1").status_code == 409
    [summary] = listing(people, "second")
    assert summary["id"] == direct["id"] and summary["unread_count"] == 1 and summary["other_user"]["id"] == people["first_id"]
    assert summary["other_member"] is True and summary["muted"] is False and summary["blocked"] is False
    # The author's own message does not count as unread for them.
    assert listing(people, "first")[0]["unread_count"] == 0
    assert people["second"].get(path + "/messages").json() == [message]
    assert people["second"].put(path + "/read", json={"sequence": message["sequence"]}).status_code == 204
    assert people["second"].put(path + "/read", json={"sequence": 0}).status_code == 204
    assert listing(people, "second")[0]["unread_count"] == 0
    assert listing(people, "second")[0]["last_read_sequence"] == message["sequence"]

    reply = post(people, direct["id"], "Thanks, I will read it tonight.", "dm-2", sender="second").json()
    assert listing(people, "first")[0]["unread_count"] == 1
    assert people["first"].get(path + "/messages", params={"after": message["sequence"]}).json() == [reply]
    assert people["first"].get(path + "/messages", params={"before": reply["sequence"], "limit": 1}).json() == [message]

    edit = people["first"].patch(path + f"/messages/{message['id']}", json={"content": SECRET + " (revised)", "revision": 0})
    assert edit.status_code == 200 and edit.json()["revision"] == 1
    assert people["first"].patch(path + f"/messages/{message['id']}", json={"content": "stale", "revision": 0}).status_code == 409
    assert people["second"].patch(path + f"/messages/{message['id']}", json={"content": "not mine", "revision": 1}).status_code == 403
    history = people["second"].get(path + f"/messages/{message['id']}/history").json()
    assert [item["content"] for item in history] == [SECRET, SECRET + " (revised)"]
    updates = people["second"].get(path + "/message-updates").json()
    assert [item["id"] for item in updates["messages"]] == [message["id"]] and updates["after"] > 0
    assert people["second"].get(path + "/message-updates", params={"after": updates["after"]}).json()["messages"] == []
    # The original client ID still replays the original message after an edit.
    assert post(people, direct["id"], SECRET, "dm-1").json()["id"] == message["id"]


def test_only_participants_can_reach_a_direct_conversation_on_any_route(people, installation):
    root, store = people["root"], people["store"]
    direct = conversation(people)
    message = post(people, direct["id"], SECRET, "dm-secret").json()
    path = root + f"/direct-conversations/{direct['id']}"
    # Organization owners and administrators gain no access through their role.
    for who in ("owner", "admin"):
        client = people[who]
        for response in (
            client.get(path),
            client.get(path + "/messages"),
            client.get(path + "/messages", params={"before": 2**62}),
            client.get(path + "/message-updates"),
            client.get(path + f"/messages/{message['id']}/history"),
            client.post(path + "/messages", json={"content": "intrusion", "client_id": "intrusion"}),
            client.patch(path + f"/messages/{message['id']}", json={"content": "intrusion", "revision": 0}),
            client.put(path + "/read", json={"sequence": message["sequence"]}),
            client.put(path + "/mute", json={"muted": True}),
            client.get(path + "/events"),
        ):
            assert response.status_code == 404, (who, response.request.url, response.text)
            assert SECRET not in response.text
        assert client.get(root + "/direct-conversations").json() == []
        # Navigation, unread state, search, knowledge and members carry no trace of the conversation.
        for route in ("/hierarchy", "/members", "/presence", "/knowledge", "/search?q=referee", "/audit"):
            response = client.get(root + route)
            assert response.status_code == 200, (route, response.text)
            assert SECRET not in response.text and direct["id"] not in response.text and message["id"] not in response.text
        assert client.get(root + "/hierarchy").json()["direct_conversations"] == []
    # Participants see their own summary in navigation, never message content, and organization search excludes it.
    for who in ("first", "second"):
        assert people[who].get(root + "/search", params={"q": "referee"}).json() == []
        hierarchy = people[who].get(root + "/hierarchy").json()
        assert [item["id"] for item in hierarchy["direct_conversations"]] == [direct["id"]]
        assert hierarchy["topic_state"] == [] and SECRET not in str(hierarchy)
    # The topic-only store hierarchy, which agent tools read, never includes direct conversations.
    assert "direct_conversations" not in store.hierarchy(people["first_id"], people["org"])
    # Another organization's path cannot name this conversation, even for its participant.
    other = people["owner"].post("/api/organizations", json={"name": "Elsewhere", "credential_mode": "none"}).json()
    assert people["owner"].get(f"/api/organizations/{other['id']}/direct-conversations/{direct['id']}/messages").status_code == 404
    assert people["first"].get(f"/api/organizations/{other['id']}/direct-conversations/{direct['id']}/messages").status_code == 404
    # The messages never enter topic tables, so topic exports, bots and webhooks cannot reach them.
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM bot_webhook_deliveries").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 0


def test_topic_and_project_access_never_resolve_a_direct_conversation(people):
    """Topic-scoped features (links, references, exports, streams) authorize through these helpers."""
    root, store, org = people["root"], people["store"], people["org"]
    direct = conversation(people)
    message = post(people, direct["id"], SECRET, "dm-helpers").json()
    with store.transaction() as db:
        for who in ("owner_id", "admin_id", "first_id", "second_id"):
            for check in (Store._topic_access, Store._channel_access, Store._project_access):
                with pytest.raises(StoreError) as refused:
                    check(db, people[who], org, direct["id"])
                assert refused.value.status == 404
    for who in ("owner", "admin", "first", "second"):
        topic = root + f"/topics/{direct['id']}"
        for response in (people[who].get(topic + "/messages"), people[who].get(topic + "/events"),
                         people[who].get(topic + f"/messages/{message['id']}/history"), people[who].get(topic + "/export")):
            assert response.status_code == 404, (who, response.request.url)
            assert SECRET not in response.text


def test_bots_cannot_participate_or_read_direct_conversations(people):
    root, store = people["root"], people["store"]
    _, _, topics = structure(people["first"], people["org"])
    bots = BotStore(store)
    follower = bots.create(people["first_id"], people["org"], "Follower", [], ["messages:read", "messages:write"],
                           personal=True, public_access=True)
    direct = conversation(people)
    post(people, direct["id"], SECRET + " @[" + follower["id"] + "]", "dm-bot").raise_for_status()
    # A bot that follows its owner's access sees the owner's topics but never their direct conversations.
    assert {item["id"] for item in bots.topics(follower["token"])} == {topic["id"] for topic in topics}
    assert bots.messages(follower["token"]) == []
    # Bot credentials are not human sessions, and a bot cannot be a recipient.
    bearer = {"Authorization": "Bearer " + follower["token"]}
    assert people["first"].get(root + f"/direct-conversations/{direct['id']}/messages", headers=bearer).status_code == 401
    assert people["first"].get(root + "/direct-conversations", headers=bearer).status_code == 401
    assert people["first"].post(root + "/direct-conversations", json={"user_id": follower["id"]}).status_code == 404
    assert people["first"].put(root + f"/direct-blocks/{follower['id']}", json={}).status_code == 404
    assert people["first"].post(root + "/direct-conversations", json={"user_id": people["first_id"]}).status_code == 400
    assert people["first"].post(root + "/direct-conversations", json={"user_id": "f" * 32}).status_code == 404


def test_blocks_refuse_new_and_continuing_messages_without_revealing_the_block(people):
    root = people["root"]
    direct = conversation(people)
    message = post(people, direct["id"], "Hello", "dm-before").json()
    path = root + f"/direct-conversations/{direct['id']}"
    assert people["second"].put(root + f"/direct-blocks/{people['first_id']}", json={}).status_code == 204
    assert people["second"].put(root + f"/direct-blocks/{people['first_id']}", json={}).status_code == 204
    assert people["second"].get(root + "/direct-blocks").json()[0]["id"] == people["first_id"]
    assert people["first"].get(root + "/direct-blocks").json() == []
    refused = post(people, direct["id"], "Are you there?", "dm-blocked")
    assert refused.status_code == 403 and refused.json() == {"detail": UNAVAILABLE}
    edited = people["first"].patch(path + f"/messages/{message['id']}", json={"content": "Changed", "revision": 0})
    assert edited.status_code == 403 and edited.json() == {"detail": UNAVAILABLE}
    # A replay of a message accepted before the block still returns that message.
    assert post(people, direct["id"], "Hello", "dm-before").json()["id"] == message["id"]
    # The blocker cannot send while blocking, and sees that the block is theirs.
    own = post(people, direct["id"], "One last word", "dm-own", sender="second")
    assert own.status_code == 403 and own.json() == {"detail": SENDER_BLOCKED}
    assert listing(people, "second")[0]["blocked"] is True and listing(people, "first")[0]["blocked"] is False
    # History stays readable for both participants.
    assert [item["id"] for item in people["first"].get(path + "/messages").json()] == [message["id"]]
    # A block also prevents starting a conversation; the refusal matches a recipient who left the organization.
    _, third, third_identity = invited(_installation(people), email="third@example.org")
    assert third.put(root + f"/direct-blocks/{people['first_id']}", json={}).status_code == 204
    blocked_start = people["first"].post(root + "/direct-conversations", json={"user_id": third_identity["user_id"]})
    assert blocked_start.status_code == 403 and blocked_start.json() == {"detail": UNAVAILABLE}
    assert people["second"].delete(root + f"/direct-blocks/{people['first_id']}").status_code == 204
    assert post(people, direct["id"], "Are you there?", "dm-after").status_code == 201
    assert people["second"].put(path + "/mute", json={"muted": True}).status_code == 204
    assert people["owner"].delete(root + f"/members/{people['second_id']}").status_code == 204
    departed = post(people, direct["id"], "Still there?", "dm-departed")
    assert departed.status_code == 403 and departed.json() == {"detail": UNAVAILABLE}
    # The remaining participant keeps the history; the departed member has no organization access.
    assert len(people["first"].get(path + "/messages").json()) == 2
    assert listing(people, "first")[0]["other_member"] is False
    assert people["second"].get(path + "/messages").status_code == 404
    # Removing the membership cleared the departed member's read and mute state.
    with people["store"].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM direct_conversation_state WHERE user_id=?", (people["second_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM direct_conversation_state WHERE user_id=?", (people["first_id"],)).fetchone()[0] == 1


def _installation(people):
    return people["config"], people["store"], people["owner"], {"organization_id": people["org"]}


def test_mute_viewers_and_read_cursor_validation(people):
    root = people["root"]
    direct = conversation(people)
    other = conversation(people, sender="second", recipient="admin_id")
    stray = post(people, other["id"], "Elsewhere", "dm-stray", sender="second").json()
    message = post(people, direct["id"], "Muted?", "dm-mute").json()
    path = root + f"/direct-conversations/{direct['id']}"
    assert people["second"].put(path + "/read", json={"sequence": stray["sequence"]}).status_code == 400
    assert people["second"].put(path + "/mute", json={"muted": True}).status_code == 204
    state = next(item for item in listing(people, "second") if item["id"] == direct["id"])
    assert state["muted"] is True and state["unread_count"] == 1
    assert listing(people, "first")[0]["muted"] is False
    assert people["second"].put(path + "/mute", json={"muted": False}).status_code == 204
    assert next(item for item in listing(people, "second") if item["id"] == direct["id"])["muted"] is False

    # Viewers cannot write organization content: they can receive and read, mute and block, but not send or start.
    _, viewer, viewer_identity = invited(_installation(people), role="viewer", email="viewer@example.org")
    to_viewer = people["first"].post(root + "/direct-conversations", json={"user_id": viewer_identity["user_id"]}).json()
    note = post(people, to_viewer["id"], "For your information", "dm-viewer").json()
    viewer_path = root + f"/direct-conversations/{to_viewer['id']}"
    assert viewer.get(viewer_path + "/messages").json() == [note]
    assert viewer.put(viewer_path + "/read", json={"sequence": note["sequence"]}).status_code == 204
    assert viewer.put(viewer_path + "/mute", json={"muted": True}).status_code == 204
    reply = viewer.post(viewer_path + "/messages", json={"content": "Reply", "client_id": "viewer-reply"})
    assert reply.status_code == 403
    assert viewer.post(root + "/direct-conversations", json={"user_id": people["second_id"]}).status_code == 403
    assert viewer.post(root + "/direct-conversations", json={"user_id": people["first_id"]}).status_code == 200
    assert viewer.put(root + f"/direct-blocks/{people['first_id']}", json={}).status_code == 204
    assert message["sequence"] > 0


def test_direct_event_stream_is_participant_only_and_announces_messages(people):
    from test_topic_events import Stream, live

    direct = conversation(people)
    path = people["root"] + f"/direct-conversations/{direct['id']}/events"

    def credential(who):
        return {"Authorization": people[who].headers["Authorization"]}

    with live(people["config"]) as (app, base):
        for who in ("owner", "admin"):
            assert httpx.get(base + path, headers=credential(who)).status_code == 404
        assert httpx.get(base + path).status_code == 401
        stream = Stream(base, path, credential("second"))
        try:
            assert stream.next("ready")[1]["channels"] == ["messages"]
            assert stream.next("messages")[1] == {"sequence": 0, "updates": 0, "reactions": 0}
            sent = httpx.post(base + people["root"] + f"/direct-conversations/{direct['id']}/messages",
                              headers=credential("first"), json={"content": SECRET, "client_id": "dm-stream"})
            assert sent.status_code == 201, sent.text
            watermark = stream.next("messages", where=lambda data: data["sequence"] > 0)[1]
            assert watermark == {"sequence": sent.json()["sequence"], "updates": 0, "reactions": 0}
            reacted = httpx.put(base + people["root"] + f"/direct-conversations/{direct['id']}/messages/{sent.json()['id']}/reactions",
                               headers=credential("first"), json={"emoji": "heart", "active": True})
            assert reacted.status_code == 200
            assert stream.next("messages", where=lambda data: data["reactions"] > 0)[1]["sequence"] == sent.json()["sequence"]
            assert app.state.topic_events.watchers == 1
        finally:
            stream.close()
