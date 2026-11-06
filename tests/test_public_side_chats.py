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
    assert side["state"].progress(row, ANSWER, None)
    assert side["state"].finish(row, "succeeded", usage=Usage(10, 20, 5))
    reply = side["owner"].get(side["path"] + "/side-chat").json()["messages"][-1]
    assert reply["statements"] == [{"claim_id": claim["claim_id"], "statement_text": claim["statement_text"]}]
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


def test_bringing_replies_posts_one_folded_group_from_the_owner(side):
    first, _ = ask(side)
    answer(side)
    second, _ = ask(side, "And the second private question?", number=2)
    answer(side, "The second private answer.")
    messages = side["owner"].get(side["path"] + "/side-chat").json()["messages"]
    identities = [item["id"] for item in messages]
    runs_before = len(side["owner"].get(side["path"] + "/inference-runs").json())
    brought = side["owner"].post(side["path"] + "/side-chat/bring", json={"message_ids": identities[::-1], "client_id": "bring-all"})
    assert brought.status_code == 201, brought.text
    group = brought.json()
    assert group["author_id"] == side["owner_id"] and group["author_kind"] == "human"
    assert group["content"].startswith("From my side chat (4 messages):") and SECRET in group["content"]
    assert [item["id"] for item in group["side_chat"]["messages"]] == identities
    assert group["side_chat"]["omitted"] == 0 and group["side_chat"]["owner_id"] == side["owner_id"]
    # Members read the brought group in the topic; bringing starts no reply there.
    shared = side["member"].get(side["path"] + "/messages").json()
    assert next(item for item in shared if item["id"] == group["id"])["side_chat"]["messages"][1]["content"] == ANSWER
    assert len(side["owner"].get(side["path"] + "/inference-runs").json()) == runs_before
    replay = side["owner"].post(side["path"] + "/side-chat/bring", json={"message_ids": identities, "client_id": "bring-all"})
    assert replay.json()["id"] == group["id"]
    noted = side["owner"].post(side["path"] + "/side-chat/bring",
                               json={"message_ids": identities[2:], "note": True, "client_id": "bring-note"}).json()
    assert noted["side_chat"]["omitted"] == 2 and len(noted["side_chat"]["messages"]) == 2
    assert "builds on 2 earlier side messages that stay private" in noted["content"] and SECRET not in noted["content"]
    assert side["owner"].post(side["path"] + "/side-chat/bring",
                              json={"message_ids": [first["id"], second["id"]], "client_id": "questions"}).status_code == 400
    assert side["owner"].post(side["path"] + "/side-chat/bring",
                              json={"message_ids": [first["id"], first["id"]], "client_id": "twice"}).status_code == 400
    assert side["owner"].post(side["path"] + "/side-chat/bring", json={"message_ids": [], "client_id": "none"}).status_code == 422


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
