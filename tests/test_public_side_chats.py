"""Private side chats: owner-only access, private model replies, shared ledger statements, and bringing replies to a topic."""

from __future__ import annotations

import json

import pytest
from test_public_inference import MODELS, QUOTES, prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.openrouter import Quote, Usage
from reasoning_ledger.server.research_tools import ResearchTools
from reasoning_ledger.server.side_chat import INSTRUCTION, SIDE_CHAT_TOOLS, SideChats
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.topic_events import signature

SECRET = "PRIVATE SIDE QUESTION about the motivic regulator"
ANSWER = "PRIVATE SIDE ANSWER: it follows from the Borel case."
QUOTE = Quote(**{**QUOTES[0].record(), "supports_tools": True})


@pytest.fixture
def side(installation):
    """The owner keeps a side chat in a beta topic; a member and an administrator read the same topic."""
    _, state, owner_client, owner, topics = prepare(installation)
    _, member, member_identity = invited(installation)
    _, admin, _ = invited(installation, role="admin", email="admin@example.org")
    org, topic = owner["organization_id"], topics[1]["id"]
    return {
        "state": state, "org": org, "topic": topic, "topics": topics, "owner_id": owner["user_id"],
        "owner": owner_client, "member": member, "member_id": member_identity["user_id"], "admin": admin,
        "root": f"/api/organizations/{org}", "path": f"/api/organizations/{org}/topics/{topic}",
    }


def ask(side, content=SECRET, *, number=1):
    """Post a side question as the owner and queue its private reply; returns the question and the queued run."""
    posted = side["owner"].post(side["path"] + "/side-chat/messages", json={"content": content, "client_id": f"side-{number}"})
    assert posted.status_code == 201, posted.text
    parameters = side["state"].parameters([MODELS[0]], 256, 50, None)
    runs = SideChats(side["state"]).enqueue(side["owner_id"], side["org"], side["topic"], f"side-reply-{number}", parameters,
                                            [QUOTE], posted.json()["id"])
    return posted.json(), runs[0]


def answer(side, text=ANSWER):
    row = side["state"].claim()
    assert row is not None
    assert side["state"].progress(row, text, None)
    assert side["state"].finish(row, "succeeded", usage=Usage(10, 20, 5))
    return row


def test_a_side_reply_reads_the_topic_and_stays_in_its_owners_side_chat(side):
    question, run = ask(side)
    assert run["side_chat"] == {"question_id": question["id"]}
    with side["state"].store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests WHERE id=?", (run["request_id"],)).fetchone()[0])
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?",
                                         (run["request_id"],)).fetchone()[0])
    text = json.dumps(context)
    assert "Compute a shared result" in text and SECRET in text
    assert context[-1] == {"role": "user", "content": INSTRUCTION}
    assert snapshot["side_chat"] is True and snapshot["compact_through_sequence"] == snapshot["memory_through_sequence"] == 0
    answer(side)
    mine = side["owner"].get(side["path"] + "/side-chat").json()
    assert [(item["author_kind"], item["content"]) for item in mine["messages"]] == [("human", SECRET), ("assistant", ANSWER)]
    assert mine["messages"][1]["reply_to"] == question["id"] and mine["messages"][1]["inference_run_id"] == run["id"]
    assert [item["id"] for item in mine["runs"]] == [run["id"]] and mine["runs"][0]["status"] == "succeeded"
    assert side["owner"].get(side["path"] + "/side-chat", params={"after": mine["messages"][0]["sequence"]}).json()["messages"] \
        == mine["messages"][1:]
    topic_messages = side["owner"].get(side["path"] + "/messages").json()
    assert all(SECRET not in item["content"] and ANSWER not in item["content"] for item in topic_messages)
    with side["state"].store.transaction() as db:
        assert db.execute("SELECT message_id FROM llm_runs WHERE id=?", (run["id"],)).fetchone()[0] is None


@pytest.mark.parametrize("reader", ["member", "admin"])
def test_nobody_else_can_see_or_control_a_side_reply(side, reader):
    question, run = ask(side)
    answer(side)
    client, run_path = side[reader], side["root"] + "/inference-runs/" + run["id"]
    assert client.get(side["path"] + "/side-chat").json() == {"messages": [], "runs": []}
    assert run["id"] not in [item["id"] for item in client.get(side["path"] + "/inference-runs").json()]
    assert client.get(run_path).status_code == 404
    assert client.get(run_path + "/events").status_code == 404
    assert client.post(run_path + "/cancel", json={}).status_code == 404
    assert side["owner"].get(run_path).status_code == 200
    # The topic's own listing leaves the reply out for its owner too; it belongs to the side chat.
    assert run["id"] not in [item["id"] for item in side["owner"].get(side["path"] + "/inference-runs").json()]
    for path in ("/research-export", "/export?detail=raw", "/research-context", "/research-tools", "/messages"):
        response = client.get(side["path"] + path)
        assert response.status_code == 200, (path, response.text)
        assert SECRET not in response.text and ANSWER not in response.text, path
    assert SECRET not in client.get(side["root"] + "/search", params={"q": "PRIVATE"}).text
    assert client.post(side["path"] + "/side-chat/bring", json={"message_ids": [question["id"]], "client_id": "steal"}).status_code == 404


def test_the_topic_event_signature_ignores_side_replies(side):
    with side["state"].store.transaction() as db:
        before = signature(db, side["org"], side["topic"], True)["runs"]
    ask(side)
    with side["state"].store.transaction() as db:
        assert signature(db, side["org"], side["topic"], True)["runs"] == before


def test_side_replies_record_ledger_statements_and_write_nothing_else(side):
    question, run = ask(side)
    row = side["state"].claim()
    tools = ResearchTools(side["state"], row)
    names = {tool.name for tool in tools.registry.list()}
    assert names <= SIDE_CHAT_TOOLS | {"tool_search", "tool_call"}
    assert {"ledger_claim", "topic_history", "ledger_search"} <= names
    assert not names & {"artifact_write", "agent_delegate", "compute_propose", "compute_submit", "goal_checkpoint",
                        "account_update", "ledger_request_review", "knowledge_propose", "workflow_propose_run"}
    claim = tools.invoke("ledger_claim", {"statement": "The motivic regulator is injective for number fields."})["result"]
    with side["state"].store.transaction(write=True) as db:
        side["state"]._event(db, side["org"], run["id"], {"type": "progress", "phase": "round", "round": 1})
    assert side["state"].progress(row, ANSWER, None)
    assert side["state"].finish(row, "succeeded", usage=Usage(10, 20, 5))
    reply = side["owner"].get(side["path"] + "/side-chat").json()["messages"][-1]
    # As under a topic reply, the answer carries its run's steps and usage; statement identifiers stay in the ledger.
    assert "statements" not in reply and claim["claim_id"] not in json.dumps(reply)
    assert [step["phase"] for step in reply["steps"]] == ["round"] and reply["usage"]["completion_tokens"] == 5
    # The statement belongs to the topic's ledger as usual, so other readers see it.
    ledger = side["member"].get(side["root"] + "/mathematics", params={"topic_id": side["topic"], "scope": "topic"}).json()
    assert claim["claim_id"] in json.dumps(ledger)
    with side["state"].store.transaction() as db:
        receipt = db.execute("SELECT id FROM research_tool_receipts WHERE run_id=?", (run["id"],)).fetchone()[0]
    # A shared reply cannot read the side reply's tool receipts; a later reply in the same side chat can.
    side["owner"].post(side["path"] + "/messages", json={"content": "A shared follow-up", "client_id": "shared"}).raise_for_status()
    side["state"].enqueue(side["owner_id"], side["org"], side["topic"], "shared-reply",
                          side["state"].parameters([MODELS[0]], 256, 50, None), [QUOTE])
    shared = ResearchTools(side["state"], side["state"].claim())
    with pytest.raises(StoreError) as refused:
        shared.registry.invoke("tool_receipt_read", {"receipt_id": receipt})
    assert refused.value.status == 404
    ask(side, "A second private question", number=2)
    later = ResearchTools(side["state"], side["state"].claim())
    assert later.registry.invoke("tool_receipt_read", {"receipt_id": receipt})["run_id"] == run["id"]


def exchanges(side, count):
    """``count`` answered side questions; returns the side chat's messages, oldest first."""
    for number in range(1, count + 1):
        ask(side, SECRET if number == 1 else f"Private question {number}?", number=number)
        answer(side, ANSWER if number == 1 else f"Private answer {number}.")
    return side["owner"].get(side["path"] + "/side-chat").json()["messages"]


def bring(side, ids, client_id, **fields):
    return side["owner"].post(side["path"] + "/side-chat/bring", json={"message_ids": ids, "client_id": client_id, **fields})


def test_bringing_posts_your_words_and_one_folded_group_and_counts_what_stays_private(side):
    messages = exchanges(side, 2)
    identities = [item["id"] for item in messages]
    runs_before = len(side["owner"].get(side["path"] + "/inference-runs").json())
    brought = bring(side, identities[:1:-1], "bring-second", words="  Here is the **second** case.  ")
    assert brought.status_code == 201, brought.text
    post = brought.json()
    assert post["author_id"] == side["owner_id"] and post["author_kind"] == "human" and post["reply_to"] is None
    assert post["content"].startswith("Here is the **second** case.\n\nFrom my side chat (2 messages):")
    assert "_Builds on 2 side messages that stay private._" in post["content"] and SECRET not in post["content"]
    group = post["side_chat"]
    assert [item["id"] for item in group["messages"]] == identities[2:]
    assert group["words"] == "Here is the **second** case." and group["owner_id"] == side["owner_id"]
    assert group["omitted"] == 2 and group["linked"] == []
    # Members read the brought group in the topic; bringing starts no reply there.
    shared = side["member"].get(side["path"] + "/messages").json()
    assert next(item for item in shared if item["id"] == post["id"])["side_chat"]["messages"][1]["content"] == "Private answer 2."
    assert len(side["owner"].get(side["path"] + "/inference-runs").json()) == runs_before
    # A repeated request returns its post; the same client_id with other words is refused.
    assert bring(side, identities[2:], "bring-second", words="Here is the **second** case.").json()["id"] == post["id"]
    assert bring(side, identities[2:], "bring-second", words="Other words").status_code == 409
    # The side chat names the post that carried each brought message.
    view = side["owner"].get(side["path"] + "/side-chat").json()["messages"]
    assert [item["brought"] and item["brought"]["message_id"] for item in view] == [None, None, post["id"], post["id"]]
    edit = side["owner"].patch(side["path"] + "/messages/" + post["id"], json={"content": "Changed", "revision": 0})
    assert edit.status_code == 400


def test_a_side_message_reaches_the_topic_once_and_later_posts_link_to_it(side):
    messages = exchanges(side, 3)
    identities = [item["id"] for item in messages]
    first = bring(side, identities[2:4], "bring-second").json()
    for repeat in (identities[2:4], identities[3:6]):
        refused = bring(side, repeat, "again-" + repeat[0])
        assert refused.status_code == 409 and "already in the topic" in refused.text
    question = side["member"].post(side["path"] + "/messages", json={"content": "And the motivic case?", "client_id": "asks"}).json()
    words = f"@[{side['member_id']}] yes, see below."
    answered = bring(side, identities[4:], "bring-third", words=words, reply_to=question["id"])
    assert answered.status_code == 201, answered.text
    post = answered.json()
    assert post["reply_to"] == question["id"]
    # The third reply read all of the side chat before it: the second exchange is linked, the first only counted.
    assert post["side_chat"]["linked"] == [{"message_id": first["id"], "created_at": first["created_at"]}]
    assert post["side_chat"]["omitted"] == 2
    link = f"/organization.html?organization={side['org']}&topic={side['topic']}&message={first['id']}"
    assert f"_Builds on [my earlier post]({link}) and 2 side messages that stay private._" in post["content"]
    with side["state"].store.transaction() as db:
        mentioned = db.execute("SELECT user_id FROM message_mentions WHERE message_id=?", (post["id"],)).fetchall()
    assert [row[0] for row in mentioned] == [side["member_id"]]
    # The first exchange is still free to bring; nothing before it was read, so its post builds on nothing.
    earliest = bring(side, identities[:2], "bring-first").json()
    assert earliest["side_chat"]["omitted"] == 0 and earliest["side_chat"]["linked"] == []
    assert "Builds on" not in earliest["content"]
    other = side["owner"].post(side["root"] + f"/topics/{side['topics'][0]['id']}/messages",
                               json={"content": "Elsewhere", "client_id": "elsewhere"}).json()
    ask(side, "Private question 4?", number=4)
    answer(side, "Private answer 4.")
    latest = side["owner"].get(side["path"] + "/side-chat").json()["messages"][-2:]
    refused = bring(side, [item["id"] for item in latest], "cross", reply_to=other["id"])
    assert refused.status_code == 400 and "same topic" in refused.text


def test_bringing_needs_a_reply_distinct_messages_and_short_words(side):
    messages = exchanges(side, 2)
    first, second = messages[0], messages[2]
    assert bring(side, [first["id"], second["id"]], "questions").status_code == 400
    assert bring(side, [first["id"], first["id"]], "twice").status_code == 400
    assert bring(side, [], "none").status_code == 422
    assert bring(side, [messages[1]["id"]], "long", words="x" * 4001).status_code == 422
    assert bring(side, [messages[1]["id"]], "old", note=True).status_code == 422


def test_a_side_question_can_be_about_a_message_of_the_topic(side):
    shared = side["member"].post(side["path"] + "/messages", json={"content": "SHARED POST about K-groups", "client_id": "shared"}).json()
    asked = side["owner"].post(side["path"] + "/side-chat/messages",
                               json={"content": "Does the rank match?", "client_id": "about", "about_message_id": shared["id"]})
    assert asked.status_code == 201, asked.text
    question = asked.json()
    assert question["about"]["message_id"] == shared["id"] and question["about"]["author_id"] == side["member_id"]
    assert question["about"]["created_at"] == shared["created_at"] and question["about"]["author_name"]
    # The same client_id about nothing is another question; a message of another topic cannot be named.
    again = side["owner"].post(side["path"] + "/side-chat/messages", json={"content": "Does the rank match?", "client_id": "about"})
    assert again.status_code == 409
    elsewhere = side["owner"].post(side["root"] + f"/topics/{side['topics'][0]['id']}/messages",
                                   json={"content": "Elsewhere", "client_id": "elsewhere"}).json()
    refused = side["owner"].post(side["path"] + "/side-chat/messages",
                                 json={"content": "Hm?", "client_id": "about-elsewhere", "about_message_id": elsewhere["id"]})
    assert refused.status_code == 404
    # The reply reads a note naming the message, with its text, just before the instruction.
    parameters = side["state"].parameters([MODELS[0]], 256, 50, None)
    runs = SideChats(side["state"]).enqueue(side["owner_id"], side["org"], side["topic"], "about-reply", parameters, [QUOTE],
                                            question["id"])
    with side["state"].store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests WHERE id=?", (runs[0]["request_id"],)).fetchone()[0])
    assert context[-1] == {"role": "user", "content": INSTRUCTION}
    assert context[-2]["role"] == "system" and "is about this message" in context[-2]["content"]
    assert "SHARED POST about K-groups" in context[-2]["content"] and shared["id"] in context[-2]["content"]


def add(side, run, content, client_id, client=None):
    return (client or side["owner"]).post(side["path"] + "/side-chat/messages",
                                          json={"content": content, "client_id": client_id, "adds_to": run["id"]})


def test_a_side_message_joins_a_side_reply_at_work_which_reads_it_at_its_next_step(side):
    question, run = ask(side)
    added = add(side, run, "ALSO consider K_3", "added")
    assert added.status_code == 201, added.text
    added = added.json()
    assert added["addition"] == {"run_id": run["id"], "read_at": None, "read_step": None}
    assert add(side, run, "ALSO consider K_3", "added").json()["id"] == added["id"], "a replay returns the same message"
    [listed] = side["owner"].get(side["path"] + "/side-chat").json()["runs"]
    assert [(item["message_id"], item["content"], item["read_at"]) for item in listed["additions"]] == [
        (added["id"], "ALSO consider K_3", None)]
    # The reply reads it once, before its next provider call, as a side message of its owner.
    row = side["state"].claim()
    name = side["owner"].get("/api/auth/me").json()["name"]
    assert side["state"].take_additions(row, 2) == [
        {"id": added["id"], "side_sequence": added["sequence"], "kind": "human", "text": "ALSO consider K_3", "sender": name}]
    assert side["state"].take_additions(row, 3) == []
    assert side["owner"].delete(side["path"] + "/side-chat/additions/" + added["id"]).status_code == 409, "it was read"
    assert side["state"].progress(row, ANSWER, None)
    assert side["state"].finish(row, "succeeded", usage=Usage(10, 20, 5))
    reply = side["owner"].get(side["path"] + "/side-chat").json()["messages"][-1]
    assert reply["author_kind"] == "assistant" and reply["reply_to"] == question["id"]
    assert [(item["message_id"], item["read_step"]) for item in reply["additions"]] == [(added["id"], 2)]
    assert [step["message_ids"] for step in reply["steps"] if step["phase"] == "additions_read"] == [[added["id"]]]
    # A reply that finished takes nothing more, and nothing is posted.
    late = add(side, run, "TOO LATE for that reply", "late")
    assert late.status_code == 409
    assert "TOO LATE" not in side["owner"].get(side["path"] + "/side-chat").text


def test_an_unread_addition_can_be_taken_back_and_only_your_own_side_reply_takes_one(side):
    _, run = ask(side)
    added = add(side, run, "Never mind", "added").json()
    member = side["member"]
    assert member.delete(side["path"] + "/side-chat/additions/" + added["id"]).status_code == 404
    assert add(side, run, "Mine too", "member-added", client=member).status_code == 404
    taken = side["owner"].delete(side["path"] + "/side-chat/additions/" + added["id"])
    assert taken.status_code == 200 and taken.json()["addition"] is None and taken.json()["id"] == added["id"]
    assert side["owner"].delete(side["path"] + "/side-chat/additions/" + added["id"]).status_code == 404
    assert side["state"].take_additions(side["state"].claim(), 2) == []
    # A message joins a side reply of this side chat, and is about nothing else.
    assert add(side, {"id": "0" * 32}, "Where?", "nowhere").status_code == 404
    about = side["owner"].post(side["path"] + "/side-chat/messages",
                               json={"content": "Both?", "client_id": "both", "adds_to": run["id"], "about_message_id": "a" * 32})
    assert about.status_code == 400


def test_side_chats_need_a_beta_topic_write_access_and_their_own_questions(installation):
    _, state, client, owner, topics = prepare(installation)
    _, viewer, _ = invited(installation, role="viewer", email="viewer@example.org")
    _, member, _ = invited(installation)
    org = owner["organization_id"]
    alpha, beta = (f"/api/organizations/{org}/topics/{topics[index]['id']}" for index in (0, 1))
    assert client.get(alpha + "/side-chat").status_code == 400
    assert viewer.get(beta + "/side-chat").json() == {"messages": [], "runs": []}
    assert viewer.post(beta + "/side-chat/messages", json={"content": "hello", "client_id": "v"}).status_code == 403
    question = client.post(beta + "/side-chat/messages", json={"content": SECRET, "client_id": "q"}).json()
    assert client.post(beta + "/side-chat/messages", json={"content": "different", "client_id": "q"}).status_code == 409
    request = {"question_id": question["id"], "models": [MODELS[0]], "client_id": "foreign"}
    assert member.post(beta + "/side-chat/replies", json=request).status_code == 404
    assert client.post(beta + "/side-chat/replies", json={**request, "models": MODELS}).status_code == 422
