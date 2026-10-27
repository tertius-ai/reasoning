"""A model's answer is a reply to the message it answers, and a reply to that answer continues the thread."""

import json

from test_public_inference import MODELS, enqueue, prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.openrouter import Usage


def answer(state, owner, topic, text, client):
    enqueue(state, owner, topic, models=[MODELS[0]], client=client)
    run = state.claim()
    state.progress(run, text, None)
    state.finish(run, "succeeded", usage=Usage(3, 4, 5))
    return run, next(m for m in state.store.messages(owner["user_id"], owner["organization_id"], topic)
                     if m.get("inference_run_id") == run["id"])


def test_an_answer_replies_to_its_prompt_and_a_follow_up_continues_the_thread(installation):
    _, state, client, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    root = f"/api/organizations/{org}/topics/{topic}/messages"
    prompt = client.post(root, json={"content": "Survey higher regulators.", "client_id": "prompt"}).json()
    enqueue(state, owner, topic, models=[MODELS[0]], client="first")
    run = state.claim()
    # A teammate's message arrives while the reply runs; the answer still replies to the prompt.
    _, member_client, _ = invited(installation)
    assert member_client.post(root, json={"content": "I'll look at the étale side.", "client_id": "between"}).status_code == 201
    state.progress(run, "Beilinson's regulator.", None)
    state.finish(run, "succeeded", usage=Usage(3, 4, 5))
    first = next(m for m in state.store.messages(user, org, topic) if m.get("inference_run_id") == run["id"])
    assert first["reply_to"] == prompt["id"]

    # Replying to the answer asks a follow-up; its answer replies to the follow-up, in the same thread.
    follow = client.post(root, json={"content": "Does that cover the p-adic case?", "client_id": "follow",
                                     "reply_to": first["id"]}).json()
    request = enqueue(state, owner, topic, models=[MODELS[0]], client="second")[0]
    with state.store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests WHERE id=?", (request["request_id"],)).fetchone()[0])
    records = [json.loads(m["content"]) for m in context if m["role"] == "user" and m["content"].startswith("{")]
    assert {"id": follow["id"], "reply_to": first["id"]}.items() <= next(r for r in records if r.get("id") == follow["id"]).items(), \
        "the model sees which answer the follow-up replies to"
    run = state.claim()
    state.progress(run, "Only partly: the syntomic regulator.", None)
    state.finish(run, "succeeded", usage=Usage(3, 4, 5))
    second = next(m for m in state.store.messages(user, org, topic) if m.get("inference_run_id") == run["id"])
    assert second["reply_to"] == follow["id"]
    thread = client.get(f"{root}/{second['id']}/thread").json()
    assert thread["root_id"] == prompt["id"]
    assert [m["id"] for m in thread["messages"]] == [prompt["id"], first["id"], follow["id"], second["id"]]


def test_an_answer_without_a_human_prompt_replies_to_nothing_and_forks_keep_reply_targets(installation):
    _, state, client, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    _, first = answer(state, owner, topic, "First answer.", "first")
    assert first["reply_to"] is not None, "prepare posts the human prompt this answers"
    _, again = answer(state, owner, topic, "Answer to an answer.", "again")
    assert again["reply_to"] is None, "the context ends at a model answer, not a human message"
    forked = client.post(f"/api/organizations/{org}/topics/{topic}/forks",
                         json={"message_id": first["id"], "name": "Alternate", "client_id": "fork"})
    assert forked.status_code == 201, forked.text
    copies = state.store.messages(user, org, forked.json()["topic"]["id"])
    assert copies[-1]["reply_to"] == copies[0]["id"] and copies[-1]["id"] != first["id"]
