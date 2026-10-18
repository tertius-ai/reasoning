"""Hierarchy deletion authorization, atomic admission fences, and retained accounting."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.openrouter import Quote
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.workflow_store import WorkflowStore


def deletion(client, root, kind, entity):
    path = f"{root}/{kind}/{entity['id']}"
    plan = client.get(path + "/deletion")
    assert plan.status_code == 200, plan.text
    return path, {"confirmation": entity["name"], "plan_token": plan.json()["plan_token"]}


def test_delete_project_hides_descendants_revokes_bot_grants_and_preserves_records(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    root = f"/api/organizations/{org}"
    project, channel, topics = structure(client, org)
    message = client.post(root + f"/topics/{topics[0]['id']}/messages", json={"content": "hidden proof", "client_id": "proof"}).json()
    knowledge = client.post(root + "/knowledge", json={
        "client_id": "deletion-knowledge-001", "kind": "ledger", "title": "Hidden lemma", "body": "Proof",
        "state": "note", "scope_kind": "topic", "scope_id": topics[0]["id"],
    }).json()
    bots = BotStore(store)
    bot = bots.create(user, org, "Proof reader", [topic["id"] for topic in topics], ["messages:read", "messages:write"])
    path, body = deletion(client, root, "projects", project)
    plan = client.get(path + "/deletion").json()
    assert plan["counts"] == {"projects": 1, "channels": 1, "topics": 3, "messages": 1, "workflows": 0, "compute_jobs": 0,
                              "knowledge_entries": 1, "model_runs": 0}
    assert client.request("DELETE", path, json=body).status_code == 204
    assert client.get(root + "/hierarchy").json() == {"projects": [], "channels": [], "topics": [], "topic_state": [],
                                                       "direct_conversations": []}
    assert client.get(root + "/search", params={"q": "proof"}).json() == []
    assert client.get(root + "/knowledge").json() == []
    assert client.get(root + f"/knowledge/{knowledge['id']}").status_code == 404
    assert client.get(root + f"/topics/{topics[0]['id']}/messages").status_code == 404
    assert client.post(root + "/channels", json={"name": "hidden", "project_id": project["id"]}).status_code == 404
    assert client.post(root + "/topics", json={"name": "hidden", "kind": "alpha", "channel_id": channel["id"]}).status_code == 404
    assert bots.topics(bot["token"]) == [] and bots.messages(bot["token"]) == []
    with pytest.raises(StoreError, match="topic"):
        bots.create(user, org, "Cannot regrant", [topics[0]["id"]], ["messages:read"])
    with store.transaction() as db:
        assert db.execute("SELECT content FROM messages WHERE id=?", (message["id"],)).fetchone()[0] == "hidden proof"
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='projects.delete'").fetchone()[0] == 1
        assert db.execute("SELECT name FROM hierarchy_deletions").fetchone()[0] == project["name"]
        assert db.execute("SELECT count(*) FROM knowledge_versions").fetchone()[0] == 1


def test_private_project_owner_deletes_only_channels_and_topics(installation):
    _, _, administrator, owner = installation
    _, project_owner, project_user = invited(installation, email="project-owner@example.org")
    _, member, member_user = invited(installation, email="other@example.org")
    root = f"/api/organizations/{owner['organization_id']}"
    project = administrator.post(root + "/projects", json={"name": "Private", "visibility": "private",
        "owner_id": project_user["user_id"], "member_ids": [member_user["user_id"]]}).json()
    channel = administrator.post(root + "/channels", json={"name": "private", "project_id": project["id"]}).json()
    topic = administrator.post(root + "/topics", json={"name": "proof", "kind": "alpha", "channel_id": channel["id"]}).json()
    for kind, entity in (("projects", project), ("channels", channel), ("topics", topic)):
        assert member.get(root + f"/{kind}/{entity['id']}/deletion").status_code == 403
    assert project_owner.get(root + f"/projects/{project['id']}/deletion").status_code == 403
    path, body = deletion(project_owner, root, "topics", topic)
    assert project_owner.request("DELETE", path, json=body).status_code == 204
    path, body = deletion(project_owner, root, "channels", channel)
    assert project_owner.request("DELETE", path, json=body).status_code == 204
    # Archival channel identities do not prevent reusing a deleted name.
    assert project_owner.post(root + "/channels", json={"name": "private", "project_id": project["id"]}).status_code == 201
    ordinary, _, _ = structure(administrator, owner["organization_id"])
    assert project_owner.get(root + f"/projects/{ordinary['id']}/deletion").status_code == 403


def test_deletion_requires_exact_confirmation_and_current_counts(installation):
    _, _, client, owner = installation
    project, channel, topics = structure(client, owner["organization_id"])
    root = f"/api/organizations/{owner['organization_id']}"
    path, body = deletion(client, root, "channels", channel)
    assert client.request("DELETE", path, json={**body, "confirmation": "wrong"}).status_code == 400
    client.post(root + f"/topics/{topics[0]['id']}/messages", json={"content": "new", "client_id": "new"}).raise_for_status()
    assert client.request("DELETE", path, json=body).status_code == 409
    assert client.get(root + "/hierarchy").json()["projects"][0]["id"] == project["id"]


def add_job(db, org, user, topic):
    db.execute("INSERT INTO compute_jobs(id,organization_id,topic_id,requested_by,client_id,parameters_json,image_json,"
        "program_digest,status,created_at,reserved_cpu_seconds) VALUES(?,?,?,?,?,'{}','{}','digest','queued',1,1)",
        ("d" * 32, org, topic, user, "deletion-race"))


@pytest.mark.parametrize("scope", ["projects", "channels", "topics"])
def test_active_compute_blocks_every_deletion_scope(installation, scope):
    _, store, client, owner = installation
    project, channel, topics = structure(client, owner["organization_id"])
    root, org, user = f"/api/organizations/{owner['organization_id']}", owner["organization_id"], owner["user_id"]
    entity = {"projects": project, "channels": channel, "topics": topics[2]}[scope]
    with store.transaction(write=True) as db:
        add_job(db, org, user, topics[2]["id"])
    path, body = deletion(client, root, scope, entity)
    assert client.get(path + "/deletion").json()["active"]["compute"] == 1
    assert client.request("DELETE", path, json=body).status_code == 409
    with store.transaction(write=True) as db:
        db.execute("UPDATE compute_jobs SET status='succeeded',used_cpu_seconds=1,accounted_at=1")
    assert client.request("DELETE", path, json=body).status_code == 204
    with store.transaction() as db:
        assert db.execute("SELECT used_cpu_seconds FROM compute_jobs").fetchone()[0] == 1


def test_deletion_and_job_admission_share_atomic_fence(installation):
    _, store, client, owner = installation
    _, _, topics = structure(client, owner["organization_id"])
    org, user, topic = owner["organization_id"], owner["user_id"], topics[2]
    plan = store.deletion_plan(user, org, "topics", topic["id"])
    start = Barrier(2)
    def admission():
        start.wait()
        try:
            with store.transaction(write=True) as db:
                store._topic_access(db, user, org, topic["id"], write=True)
                add_job(db, org, user, topic["id"])
            return "admitted"
        except StoreError as exc:
            return exc.status
    def remove():
        start.wait()
        try:
            store.delete_entity(user, org, "topics", topic["id"], topic["name"], plan["plan_token"])
            return "deleted"
        except StoreError as exc:
            return exc.status
    with ThreadPoolExecutor(2) as pool:
        results = [future.result() for future in [pool.submit(admission), pool.submit(remove)]]
    assert results in [["admitted", 409], [404, "deleted"]]


def test_active_inference_and_workflow_block_deletion_and_completed_cost_survives(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    project, _, topics = structure(client, org)
    store.add_key(user, org, "openrouter", "Test", "fixture-provider-credential")
    store.set_policy(user, org, ["test/math"], 1_000_000)
    store.post_message(user, org, topics[1]["id"], "Explain the lemma", "deletion-human-message")
    inference = InferenceStore(store)
    request = inference.enqueue(user, org, topics[1]["id"], "deletion-inference",
        inference.parameters(["test/math"], 128, 50, None), [Quote("test/math", "0.000001", "0.000002", "0", 20000, 8192)])
    saved = WorkflowStore(store).create(user, org, topics[2]["id"], "deletion-workflow-001", {
        "format": 1, "name": "Approval", "nodes": [{"id": "review", "kind": "approval", "depends_on": [], "inputs": {},
        "parameters": {"message": "Review", "required_role": "admin"}}],
    })
    run = client.app.state.workflow_runs.start(user, org, saved["workflow_id"], 1, "deletion-workflow-run-001")
    plan = store.deletion_plan(user, org, "projects", project["id"])
    assert plan["active"]["inference"] == 1 and plan["active"]["workflows"] == 1
    with pytest.raises(StoreError, match="stop active"):
        store.delete_entity(user, org, "projects", project["id"], project["name"], plan["plan_token"])
    with store.transaction(write=True) as db:
        db.execute("UPDATE llm_runs SET status='failed',accounting_state='settled',actual_microusd=42 WHERE id=?", (request[0]["id"],))
        db.execute("UPDATE workflow_runs SET status='cancelled' WHERE id=?", (run["id"],))
    store.delete_entity(user, org, "projects", project["id"], project["name"], plan["plan_token"])
    with store.transaction() as db:
        assert db.execute("SELECT actual_microusd FROM llm_runs").fetchone()[0] == 42


def test_topic_deletion_hides_only_its_scoped_knowledge(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    project, _, topics = structure(client, org)
    root = f"/api/organizations/{org}"
    entries = [client.post(root + "/knowledge", json={"client_id": "scope-deletion-" + scope,
        "kind": "ledger", "title": scope, "body": "Proof", "state": "note", "scope_kind": scope, "scope_id": identifier}).json()
        for scope, identifier in [("topic", topics[0]["id"]), ("project", project["id"])]]
    path, body = deletion(client, root, "topics", topics[0])
    assert client.request("DELETE", path, json=body).status_code == 204
    assert [entry["id"] for entry in client.get(root + "/knowledge").json()] == [entries[1]["id"]]
    assert client.get(root + "/knowledge/" + entries[0]["id"]).status_code == 404
    assert client.get(root + "/knowledge/" + entries[1]["id"]).status_code == 200


def test_active_agent_goal_requires_stop_before_topic_deletion(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][1]
    with store.transaction(write=True) as db:
        db.execute("INSERT INTO agent_goals(id,organization_id,topic_id,owned_by,client_id,objective,acceptance_json,model,status,"
            "budget_microusd,max_turns,max_tool_calls,deadline_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'Prove the lemma','[]','test/math','active',1,1,1,1,1,1)",
            ("e" * 32, org, topic["id"], user, "deletion-goal"))
    plan = store.deletion_plan(user, org, "topics", topic["id"])
    assert plan["active"]["agents"] == 1
    with pytest.raises(StoreError, match="stop active"):
        store.delete_entity(user, org, "topics", topic["id"], topic["name"], plan["plan_token"])
    with store.transaction(write=True) as db:
        db.execute("UPDATE agent_goals SET status='paused'")
    store.delete_entity(user, org, "topics", topic["id"], topic["name"], plan["plan_token"])
