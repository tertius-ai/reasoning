"""A reply names a message of its own topic, and a thread collects a message with every reply around it."""

from test_public_references import private_structure
from test_public_server import installation as installation
from test_public_server import invited, structure


def post(client, org, topic, content, client_id, reply_to=None):
    body = {"content": content, "client_id": client_id}
    if reply_to is not None:
        body["reply_to"] = reply_to
    return client.post(f"/api/organizations/{org}/topics/{topic}/messages", json=body)


def thread(client, org, topic, message):
    return client.get(f"/api/organizations/{org}/topics/{topic}/messages/{message}/thread")


def test_replies_name_a_message_of_the_same_topic_and_retry_exactly(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    topic, other = topics[0]["id"], topics[1]["id"]
    original = post(client, org, topic, "Can you check Lemma 3.2?", "original").json()
    assert original["reply_to"] is None
    reply = post(client, org, topic, "The constant should be 2.", "reply", original["id"])
    assert reply.status_code == 201, reply.text
    assert reply.json()["reply_to"] == original["id"]
    listed = client.get(f"/api/organizations/{org}/topics/{topic}/messages").json()
    assert [message["reply_to"] for message in listed if message["id"] in {original["id"], reply.json()["id"]}] == [None, original["id"]]
    # An exact retry returns the same message; the same client_id for a different target is a conflict.
    assert post(client, org, topic, "The constant should be 2.", "reply", original["id"]).json()["id"] == reply.json()["id"]
    assert post(client, org, topic, "The constant should be 2.", "reply").status_code == 409
    elsewhere = post(client, org, other, "Unrelated.", "elsewhere").json()
    for target in (elsewhere["id"], "0" * 32):
        refused = post(client, org, topic, "Crossing topics.", "cross-" + target[:4], target)
        assert refused.status_code == 400, refused.text
    assert post(client, org, topic, "Malformed.", "malformed", "not-an-id").status_code == 422


def test_a_thread_holds_the_root_and_every_reply_in_order(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    topic = topics[0]["id"]
    root = post(client, org, topic, "Root", "root").json()
    first = post(client, org, topic, "First reply", "first", root["id"]).json()
    unrelated = post(client, org, topic, "Unrelated", "unrelated").json()
    nested = post(client, org, topic, "Reply to the reply", "nested", first["id"]).json()
    second = post(client, org, topic, "Second reply", "second", root["id"]).json()
    expected = [root["id"], first["id"], nested["id"], second["id"]]
    for start in expected:
        result = thread(client, org, topic, start)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["root_id"] == root["id"] and body["message_id"] == start and body["truncated"] is False
        assert [message["id"] for message in body["messages"]] == expected
    alone = thread(client, org, topic, unrelated["id"]).json()
    assert [message["id"] for message in alone["messages"]] == [unrelated["id"]]
    assert thread(client, org, topic, "0" * 32).status_code == 404
    # A message id from another topic is not found through this topic.
    assert thread(client, org, topics[1]["id"], root["id"]).status_code == 404


def test_threads_follow_the_topic_access_check(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, hidden_topic = private_structure(client, org, owner["user_id"])
    secret = post(client, org, hidden_topic["id"], "Hidden root", "secret").json()
    _, outsider, _ = invited(installation, role="member", email="outsider@example.org")
    refused = thread(outsider, org, hidden_topic["id"], secret["id"])
    listed = outsider.get(f"/api/organizations/{org}/topics/{hidden_topic['id']}/messages")
    assert refused.status_code == listed.status_code and refused.status_code >= 400
    assert "Hidden root" not in refused.text
