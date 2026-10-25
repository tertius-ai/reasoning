"""Mark unread from a message: a reader's own read point moves back, for topics and direct messages."""

from test_public_direct_messages import conversation, listing, people, post  # noqa: F401
from test_public_server import installation, invited, structure  # noqa: F401


def topic_state(store, user, org, topic):
    return next(state for state in store.hierarchy(user, org)["topic_state"] if state["topic_id"] == topic)


def test_marking_a_topic_unread_moves_only_this_readers_point_back(installation):  # noqa: F811
    _, store, client, owner = installation
    _, member, member_identity = invited(installation)
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    root = f"/api/organizations/{org}/topics/{topic}"
    messages = [member.post(root + "/messages", json={"content": f"Note {index}", "client_id": f"note-{index}"}).json()
                for index in range(3)]
    for reader in (client, member):
        assert reader.put(root + "/read", json={"sequence": messages[-1]["sequence"]}).status_code == 204
    assert topic_state(store, user, org, topic)["unread_count"] == 0
    marked = client.put(root + "/unread", json={"message_id": messages[1]["id"]})
    assert marked.status_code == 200, marked.text
    assert marked.json() == {"last_read_sequence": messages[1]["sequence"] - 1}
    assert topic_state(store, user, org, topic)["unread_count"] == 2
    # Marking a later message never moves the point forward; reading again clears it.
    assert client.put(root + "/unread", json={"message_id": messages[2]["id"]}).json()["last_read_sequence"] == messages[1]["sequence"] - 1
    assert topic_state(store, member_identity["user_id"], org, topic)["unread_count"] == 0, "other readers are unchanged"
    assert client.put(root + "/read", json={"sequence": messages[-1]["sequence"]}).status_code == 204
    assert topic_state(store, user, org, topic)["unread_count"] == 0
    assert client.put(root + "/unread", json={"message_id": "f" * 32}).status_code == 404


def test_marking_a_direct_message_unread_counts_from_there(people):  # noqa: F811
    direct = conversation(people)
    path = people["root"] + f"/direct-conversations/{direct['id']}"
    sent = [post(people, direct["id"], f"Message {index}", f"unread-{index}").json() for index in range(3)]
    assert people["second"].put(path + "/read", json={"sequence": sent[-1]["sequence"]}).status_code == 204
    assert listing(people, "second")[0]["unread_count"] == 0
    marked = people["second"].put(path + "/unread", json={"message_id": sent[0]["id"]})
    assert marked.status_code == 200 and marked.json() == {"last_read_sequence": sent[0]["sequence"] - 1}
    assert listing(people, "second")[0]["unread_count"] == 3
    assert listing(people, "first")[0]["unread_count"] == 0, "the other participant is unchanged"
    # Only participants can mark a conversation unread.
    assert people["admin"].put(path + "/unread", json={"message_id": sent[0]["id"]}).status_code == 404
