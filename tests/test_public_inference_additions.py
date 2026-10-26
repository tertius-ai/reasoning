"""Later topic messages join a running model reply at its next step, never by replaying a request."""

import json

from test_public_inference import MODELS, enqueue, prepare
from test_public_research_agent import ResearchFixture
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.openrouter import Quote
from reasoning_ledger.server.research_agent import ResearchAgent

QUOTE = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)


def running(installation):
    """A reply to the topic's latest message, claimed by the worker."""
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    state.enqueue(owner["user_id"], org, topic, "question", state.parameters([MODELS[0]], 256, 50, None), [QUOTE])
    return state, client, owner, topic, state.claim()


def post(client, org, topic, content, client_id, **body):
    return client.post(f"/api/organizations/{org}/topics/{topic}/messages", json={"content": content, "client_id": client_id, **body})


def test_a_message_sent_in_thread_mode_is_read_at_the_next_step_and_kept_for_later_steps(installation):
    state, client, owner, topic, row = running(installation)
    org = owner["organization_id"]
    early = post(client, org, topic, "Skip French-language sources.", "early", adds_to=row["id"])
    assert early.status_code in {200, 201}
    assert early.json()["addition"]["run_id"] == row["id"] and early.json()["addition"]["read_at"] is None
    fixture = ResearchFixture("tools")
    original = fixture.handle

    def handle(request):
        if request.method == "POST" and not fixture.requests:
            later = post(client, org, topic, "Cite exact theorem numbers.", "later", adds_to=row["id"])
            assert later.status_code in {200, 201}
        return original(request)

    fixture.provider = type(fixture.provider)(transport=__import__("httpx").MockTransport(handle))
    ResearchAgent(state, fixture.provider, row, QUOTE).execute()
    first, second = fixture.requests[0]["messages"], fixture.requests[1]["messages"]
    added = [message["content"] for message in first if "added to this request" in str(message.get("content"))]
    assert len(added) == 1 and "Skip French-language sources." in added[0] and first[-1]["content"] == added[0]
    prompts = [message["content"] for message in second if message["role"] == "user" and "added to this request" in message["content"]]
    assert len(prompts) == 2, "the first addition stays in the history; the second joins at the next step"
    assert "Skip French-language sources." in prompts[0] and "Cite exact theorem numbers." in prompts[1]
    assert second[-1]["content"] == prompts[1]
    view = state.run(owner["user_id"], org, row["id"])
    assert view["status"] == "succeeded"
    steps = {addition["message_id"]: addition["read_step"] for addition in view["additions"]}
    assert steps == {early.json()["id"]: 1, next(a["message_id"] for a in view["additions"] if a["message_id"] != early.json()["id"]): 2}
    events = [event["event"] for event in state.events(owner["user_id"], org, row["id"], 0)]
    assert [event["step"] for event in events if event.get("phase") == "additions_read"] == [1, 2]
    listed = client.get(f"/api/organizations/{org}/topics/{topic}/messages").json()
    assert next(m for m in listed if m["id"] == early.json()["id"])["addition"]["read_step"] == 1


def test_thread_mode_refuses_a_finished_reply_without_posting_the_message(installation):
    state, client, owner, topic, row = running(installation)
    org = owner["organization_id"]
    state.finish(row, "succeeded", no_charge=True)
    refused = post(client, org, topic, "Too late.", "late", adds_to=row["id"])
    assert refused.status_code == 409 and "finished" in refused.json()["detail"]
    assert all(m["content"] != "Too late." for m in client.get(f"/api/organizations/{org}/topics/{topic}/messages").json())
    assert post(client, org, topic, "Too late.", "late").status_code == 201, "the same client_id posts normally afterwards"


def test_moving_a_message_cancels_its_queued_reply_and_refuses_a_started_one(installation):
    state, client, owner, topic, row = running(installation)
    org, user = owner["organization_id"], owner["user_id"]
    moved = post(client, org, topic, "Skip French-language sources.", "moved").json()
    own = enqueue(state, owner, topic, client="own-reply", models=MODELS[:1])[0]
    assert own["context_through_sequence"] == moved["sequence"] and own["status"] == "queued"
    root = f"/api/organizations/{org}/inference-runs/{row['id']}/additions"
    result = client.post(root, json={"message_id": moved["id"]})
    assert result.status_code == 200 and result.json()["moved"] is True and result.json()["cancelled_runs"] == [own["id"]]
    cancelled = state.run(user, org, own["id"])
    assert cancelled["status"] == "cancelled" and cancelled["accounting_state"] == "released"
    assert cancelled["error_code"] == "moved_into_reply"
    assert client.post(root, json={"message_id": moved["id"]}).status_code == 409, "already added"
    started = post(client, org, topic, "Also the étale realization.", "started").json()
    enqueue(state, owner, topic, client="started-reply", models=MODELS[:1])
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_runs SET status='running' WHERE id=(SELECT id FROM llm_runs ORDER BY rowid DESC LIMIT 1)")
    refused = client.post(root, json={"message_id": started["id"]})
    assert refused.status_code == 409 and "already started" in refused.json()["detail"]
    question = client.get(f"/api/organizations/{org}/topics/{topic}/messages").json()[0]
    assert client.post(root, json={"message_id": question["id"]}).status_code == 409, "not a later message"
    assert any(event["action"] == "inference.addition" for event in state.store.audit(user, org))


def test_only_the_author_moves_a_message_and_undo_is_refused_once_read(installation):
    state, client, owner, topic, row = running(installation)
    org = owner["organization_id"]
    _, member_client, _ = invited(installation)
    theirs = member_client.post(f"/api/organizations/{org}/topics/{topic}/messages",
                                json={"content": "A teammate's note.", "client_id": "teammate"})
    assert theirs.status_code == 201, theirs.text
    refused = client.post(f"/api/organizations/{org}/inference-runs/{row['id']}/additions", json={"message_id": theirs.json()["id"]})
    assert refused.status_code == 403
    added = post(client, org, topic, "Skip French-language sources.", "undo", adds_to=row["id"]).json()
    path = f"/api/organizations/{org}/inference-runs/{row['id']}/additions/{added['id']}"
    undone = client.delete(path)
    assert undone.status_code == 200 and undone.json()["moved"] is False
    assert state.run(owner["user_id"], org, row["id"])["additions"] == []
    again = client.post(f"/api/organizations/{org}/inference-runs/{row['id']}/additions", json={"message_id": added["id"]})
    assert again.status_code in {200, 201}
    assert state.take_additions(row, 3)[0]["text"] == "Skip French-language sources."
    assert client.delete(path).status_code == 409


def test_delegated_runs_leave_additions_to_their_coordinator(installation):
    state, client, owner, topic, row = running(installation)
    org = owner["organization_id"]
    post(client, org, topic, "Skip French-language sources.", "coordinator", adds_to=row["id"])
    with state.store.transaction(write=True) as db:
        snapshot = json.loads(db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?",
                                          (row["request_id"],)).fetchone()[0])
        db.execute("UPDATE research_contexts SET snapshot_json=? WHERE request_id=?",
                   (json.dumps({**snapshot, "agent_task_id": "task-fixture"}), row["request_id"]))
    assert state.take_additions(row, 1) == []
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE research_contexts SET snapshot_json=? WHERE request_id=?", (json.dumps(snapshot), row["request_id"]))
    assert [record["text"] for record in state.take_additions(row, 2)] == ["Skip French-language sources."]
