"""Native beta delegation without paid providers or a separately enrolled worker.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import json

import httpx
import pytest
from test_public_inference import MODELS, prepare
from test_public_research_agent import ResearchFixture
from test_public_server import installation as installation

from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.openrouter import Quote, Usage
from reasoning_ledger.server.research_agent import ResearchAgent
from reasoning_ledger.server.research_export import export_topic
from reasoning_ledger.server.research_tools import ResearchTools
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.topic_agents import TopicAgents


def root(installation):
    _, state, client, owner, topics = prepare(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[1]["id"],
        "ordinary-agent-message",
        state.parameters([quote.model], None, 50, None),
        [quote],
    )
    return state, client, owner, topics, state.claim(), quote


def finish(state, run, text="Retained result", *, unknown=False):
    state.progress(run, text, None)
    state.finish(run, "succeeded", usage=None if unknown else Usage(8, 20, 10), accounting_unknown=unknown)


class DelegatingProvider(ResearchFixture):
    def handle(self, request):
        if request.method == "GET":
            return super().handle(request)
        body = json.loads(request.content)
        self.requests.append(body)
        if len(self.requests) == 1:
            assert "Ordinary questions and discussion" in body["messages"][0]["content"]
            delta = {
                "content": "I'll compare the two approaches.",
                "tool_calls": [
                    {
                        "index": i,
                        "id": f"delegate-{i}",
                        "type": "function",
                        "function": {
                            "name": "agent_delegate",
                            "arguments": json.dumps(
                                {"role": role, "objective": "Investigate " + role, "context": "Only this assigned reference."}
                            ),
                        },
                    }
                    for i, role in enumerate(("librarian", "critic"))
                ],
            }
            reason = "tool_calls"
        elif "Assigned agent role:" in body["messages"][0]["content"]:
            assert "Compute a shared result" not in json.dumps(body["messages"])
            delta, reason = {"content": "A scoped result with unresolved obligations."}, "stop"
        elif any("Delegated work has returned" in str(message.get("content")) for message in body["messages"]):
            delta, reason = {"content": "Integrated comparison; the source claims remain unverified."}, "stop"
        else:
            assert not body.get("tools"), "waiting coordinator must release its slot, not busy-poll"
            delta, reason = {"content": "Two scoped agents are working; I'll integrate their results."}, "stop"
        frames = [
            {"id": "gen-" + str(len(self.requests)), "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]},
            {"choices": [], "usage": {"cost": "0.000008", "prompt_tokens": 20, "completion_tokens": 10}},
        ]
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)


def test_ordinary_beta_request_delegates_and_integrates_without_goal_or_enrollment(installation):
    state, _, owner, topics, parent, quote = root(installation)
    fixture = DelegatingProvider()
    ResearchAgent(state, fixture.provider, parent, quote).execute()
    agents = TopicAgents(state)
    assert state.run(owner["user_id"], owner["organization_id"], parent["id"])["status"] == "succeeded"
    assert len(agents.tasks(parent)["items"]) == 2
    agents.dispatch()
    for _ in range(2):
        child = state.claim()
        assert child and child["id"] != parent["id"]
        tools = ResearchTools(state, child)
        assert not {"topic_history", "topic_search", "tasks", "goal_checkpoint"} & {tool.name for tool in tools.registry.list()}
        assert {"ledger_status", "ledger_claim", "blog_create", "agent_delegate"} <= {tool.name for tool in tools.registry.list()}
        ResearchAgent(state, fixture.provider, child, quote).execute()
    agents.dispatch()
    agents.dispatch()  # Exactly one integration intent, including a lost dispatch reply.
    continuation = state.claim()
    assert continuation
    ResearchAgent(state, fixture.provider, continuation, quote).execute()
    agents.dispatch()
    assert state.claim() is None
    assert len(fixture.requests) == 4
    messages = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"], 0, 50)
    assert {message.get("agent_role") for message in messages} >= {"critic", "librarian"}
    assert messages[-1]["content"].startswith("Integrated comparison")
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM agent_goals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM work_workers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM topic_agent_handoffs").fetchone()[0] == 1
        assert db.execute("SELECT status FROM topic_agent_threads").fetchone()[0] == "complete"
    exported = export_topic(state, owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert len(exported["agent_tasks"]) == 2
    assert exported["agent_threads"][0]["status"] == "complete"
    assert exported["agent_handoffs"] == [{"parent_run": parent["id"], "next_run": continuation["id"]}]


def test_delegation_replays_exact_input_and_never_expands_model_or_context(installation):
    state, _, _, _, parent, _ = root(installation)
    agents = TopicAgents(state)
    first = agents.delegate(parent, "same-task", "librarian", "Find sources", "Exact supplied context")
    assert agents.delegate(parent, "same-task", "librarian", "Find sources", "Exact supplied context") == first
    with pytest.raises(StoreError, match="different input"):
        agents.delegate(parent, "same-task", "critic", "Different task", "")
    with state.store.transaction() as db:
        child = db.execute(state._joined() + "WHERE r.id=?", (first["child_run"],)).fetchone()
        text = child["context_json"]
        assert "Exact supplied context" in text and "Compute a shared result" not in text
        assert json.loads(child["parameters_json"])["models"] == [parent["model"]]
        assert json.loads(child["parameters_json"])["max_completion_tokens"] is None


def test_handoff_survives_executor_restart_and_unknown_child_charge_holds_it(installation):
    state, _, owner, topics, parent, _ = root(installation)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "child", "worker", "Investigate", "")
    finish(state, parent)
    child = state.claim()
    assert child["id"] == task["child_run"]
    finish(state, child, unknown=True)
    restarted = InferenceStore(state.store)
    TopicAgents(restarted).dispatch()
    assert restarted.claim() is None
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_runs SET accounting_state='settled',actual_microusd=8 WHERE id=?", (child["id"],))
    TopicAgents(restarted).control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "active")
    TopicAgents(restarted).dispatch()
    assert restarted.claim() is not None


def test_pause_resume_and_cancel_are_topic_scoped_and_preserve_queue(installation):
    state, client, owner, topics, parent, _ = root(installation)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "child", "worker", "Investigate", "")
    finish(state, parent)
    prefix = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/agents/{parent['id']}/control"
    assert client.post(prefix, json={"status": "paused"}).status_code == 200
    assert state.claim() is None
    assert client.post(prefix, json={"status": "active"}).status_code == 200
    child = state.claim()
    assert child["id"] == task["child_run"]
    assert client.post(prefix, json={"status": "cancelled"}).status_code == 200
    assert state.stopped(child) == "cancel_requested"
    agents.dispatch()
    assert state.claim() is None
    wrong = prefix.replace(topics[1]["id"], topics[2]["id"])
    assert client.post(wrong, json={"status": "active"}).status_code == 404


def test_paused_known_assignment_resumes_once_without_replaying_provider_intent(installation):
    state, _, owner, topics, parent, _ = root(installation)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "child", "worker", "Investigate", "")
    finish(state, parent)
    child = state.claim()
    agents.control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "paused")
    assert state.stopped(child) == "agents_paused"
    state.finish(child, "cancelled", usage=Usage(8, 20, 10), error="agents_paused")
    agents.control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "active")
    agents.control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "active")
    resumed = state.claim()
    assert resumed and resumed["id"] != task["child_run"]
    assert state.claim() is None
    assert "do not replay writes" in resumed["context_json"]


def test_many_agents_are_queued_without_eight_run_or_twenty_request_ceiling(installation):
    state, _, owner, _, parent, _ = root(installation)
    state.store.set_policy(owner["user_id"], owner["organization_id"], MODELS, 100_000_000)
    agents = TopicAgents(state)
    for index in range(100):
        agents.delegate(parent, f"child-{index}", "worker", f"Investigate independent item {index}", "")
    first = agents.tasks(parent, limit=60)
    assert len(first["items"]) == 60 and first["next_after"]
    second = agents.tasks(parent, after=first["next_after"], limit=60)
    assert len(second["items"]) == 40 and second["next_after"] is None
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}


def test_delegate_cannot_inspect_an_unrelated_assignment(installation):
    state, _, _, _, parent, _ = root(installation)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "child", "worker", "Investigate", "")
    finish(state, parent)
    child = state.claim()
    with pytest.raises(StoreError, match="outside this assignment"):
        agents.read(child, task["id"])


def test_handoff_pages_do_not_starve_ready_parents_behind_waiting_agents(installation):
    state, _, owner, _, parent, _ = root(installation)
    state.store.set_policy(owner["user_id"], owner["organization_id"], MODELS, 100_000_000)
    branches = []
    for index in range(101):
        agents = TopicAgents(state)
        task = agents.delegate(parent, f"branch-{index}", "coordinator", "Inspect one branch", "")
        with state.store.transaction(write=True) as db:
            db.execute("UPDATE llm_runs SET status='running' WHERE id=?", (task["child_run"],))
            coordinator = dict(db.execute(state._joined() + "WHERE r.id=?", (task["child_run"],)).fetchone())
        worker = agents.delegate(coordinator, "leaf", "worker", "Investigate one reference", "")
        finish(state, coordinator)
        branches.append((coordinator["id"], worker["child_run"]))
    ready_parent, ready_child = max(branches)
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_runs SET status='running' WHERE id=?", (ready_child,))
        child = dict(db.execute(state._joined() + "WHERE r.id=?", (ready_child,)).fetchone())
    finish(state, child)
    TopicAgents(state).dispatch()
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM topic_agent_handoffs").fetchone()[0] == 0
    # The runtime constructs a new service each tick; pagination must survive it.
    TopicAgents(state).dispatch()
    with state.store.transaction() as db:
        assert db.execute("SELECT parent_run FROM topic_agent_handoffs").fetchone()[0] == ready_parent
    TopicAgents(state).dispatch()  # Wrapping the scan must not repeat its handoff.
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM topic_agent_handoffs").fetchone()[0] == 1


def test_nested_coordinator_waits_for_subordinate_integration(installation):
    state, _, _, _, parent, _ = root(installation)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "coordinator", "coordinator", "Investigate a branch", "")
    finish(state, parent)
    coordinator = state.claim()
    branch = agents.delegate(coordinator, "leaf", "librarian", "Find evidence", "")
    finish(state, coordinator)
    agents.dispatch()
    leaf = state.claim()
    assert leaf["id"] == branch["child_run"]
    finish(state, leaf)
    agents.dispatch()
    integrated = state.claim()
    assert integrated and integrated["id"] not in {parent["id"], leaf["id"], coordinator["id"]}
    assert agents.tasks(integrated)["items"][0]["id"] == branch["id"]
    finish(state, integrated)
    agents.dispatch()
    final = state.claim()
    assert final and agents.tasks(final)["items"][0]["id"] == task["id"]
    finish(state, final)
    agents.dispatch()
    assert state.claim() is None


def test_bot_delegate_uses_owner_policy_narrow_tools_and_returns_to_coordinator(installation):
    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.bot_store import BotStore
    from reasoning_ledger.server.bot_tools import BotTools

    state, _, owner, topics, parent, _ = root(installation)
    bots = BotStore(state.store)
    bot = bots.create(
        owner["user_id"],
        owner["organization_id"],
        "Local collaborator",
        [topics[1]["id"]],
        ["messages:read", "messages:write", "knowledge:read", "knowledge:write"],
        personal=True,
    )
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, True)
    agents = TopicAgents(state)
    task = agents.delegate(parent, "bot", "worker", "Investigate locally", "Only the supplied reference", f"bot/{bot['id']}/default")
    assert task["child_bot_run"] and task["child_run"] is None
    finish(state, parent)
    agents.dispatch()
    assert state.claim() is None
    run = runtime.claim(bot["token"])
    assert run["id"] == task["child_bot_run"]
    tools = BotTools(runtime).context(bot["token"], run["id"], run["lease"])["tools"]
    assert "ledger_claim" in {tool["name"] for tool in tools}
    assert not {"topic_history", "messages_search", "topics_search"} & {tool["name"] for tool in tools}
    approval = runtime.request_execution(bot["token"], run["id"], run["lease"], ["python", "-c", "print(1+1)"])
    assert approval["owner_id"] == owner["user_id"] and approval["status"] == "pending"
    runtime.finish(bot["token"], run["id"], run["lease"], "succeeded", "A local result, not a proof certificate.")
    agents.dispatch()
    assert state.claim() is not None


def test_queue_capacity_does_not_block_new_human_messages(installation):
    state, _, owner, topics, parent, quote = root(installation)
    agents = TopicAgents(state)
    state.store.set_policy(owner["user_id"], owner["organization_id"], MODELS, 100_000_000)
    for index in range(30):
        agents.delegate(parent, str(index), "worker", "Independent branch", "")
    state.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[1]["id"],
        "new-human-message",
        state.parameters([quote.model], None, 50, None),
        [quote],
    )
    claimed = state.claim()
    assert json.loads(claimed["context_json"])[-1]["content"].startswith("Respond to the latest source message")


def test_unknown_interrupted_charge_refuses_resume(installation):
    state, _, owner, topics, parent, _ = root(installation)
    agents = TopicAgents(state)
    agents.delegate(parent, "child", "worker", "Investigate", "")
    finish(state, parent)
    child = state.claim()
    agents.control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "paused")
    state.finish(child, "cancelled", error="agents_paused", accounting_unknown=True)
    with pytest.raises(StoreError, match="reconcile"):
        agents.control(owner["user_id"], owner["organization_id"], topics[1]["id"], parent["id"], "active")
    assert agents.threads(owner["user_id"], owner["organization_id"], topics[1]["id"])[0]["status"] == "paused"


def test_handoff_failure_pauses_one_graph_without_losing_other_graph_progress(installation, monkeypatch):
    state, _, owner, topics, first, quote = root(installation)
    agents = TopicAgents(state)
    agents.delegate(first, "first-child", "worker", "First branch", "")
    finish(state, first)
    state.enqueue(
        owner["user_id"], owner["organization_id"], topics[1]["id"], "second-root", state.parameters([quote.model], None, 50, None), [quote]
    )
    second = state.claim()
    agents.delegate(second, "second-child", "worker", "Second branch", "")
    finish(state, second)
    finish(state, state.claim())
    finish(state, state.claim())
    enqueue = state._enqueue

    def unavailable(db, user, org, topic, client, *args, **kwargs):
        if client == "handoff-" + first["id"]:
            raise StoreError(409, "fixture refuses a retained continuation")
        return enqueue(db, user, org, topic, client, *args, **kwargs)

    monkeypatch.setattr(state, "_enqueue", unavailable)
    agents.dispatch()
    headers = {item["root_run"]: item for item in agents.threads(owner["user_id"], owner["organization_id"], topics[1]["id"])}
    assert headers[first["id"]]["status"] == "paused"
    assert headers[first["id"]]["reason"] == "agent_handoff_refused"
    assert headers[second["id"]]["status"] == "active"
    assert state.claim() is not None


@pytest.mark.parametrize("language", ["sage", "lean"])
@pytest.mark.parametrize("allowance", [0, 1000])
def test_ordinary_agent_computation_waits_for_approval_and_execution_without_paid_polling(installation, tmp_path, language, allowance):
    from test_public_compute import IMAGE

    from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, canonical, load_catalog
    from reasoning_ledger.server.compute_store import ComputeStore
    from reasoning_ledger.server.research_tools import ResearchProposals
    from reasoning_ledger.server.security import private_file

    state, _, owner, topics, parent, _ = root(installation)
    image = {**IMAGE, "languages": ["sage", "lean"]}
    catalog = tmp_path / "agent-images.json"
    private_file(catalog, canonical([image]).encode())
    compute = ComputeStore(state.store, load_catalog(catalog))
    compute.set_policy(owner["user_id"], owner["organization_id"], [image["id"]], 10000, 2)
    with state.store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO research_topic_settings(organization_id,topic_id,tools_json,compute_allowance_cpu_seconds) VALUES(?,?,?,?)",
            (
                owner["organization_id"],
                topics[1]["id"],
                json.dumps(["history", "arxiv", "web", "knowledge", "workflows", "compute"]),
                allowance,
            ),
        )
    tools = ResearchTools(state, parent, compute=compute)
    source = "example : 2 = 2 := rfl" if language == "lean" else "print(2+2)"
    invoked = tools.invoke(
        "lean_check" if language == "lean" else "compute_submit",
        {
            "image_id": image["id"],
            "program": source,
            "resources": DEFAULT_RESOURCES,
            **({"language": language} if language != "lean" else {}),
        },
    )["result"]
    agents = TopicAgents(state, compute)
    finish(state, parent, "Waiting for the approved computation.")
    agents.dispatch()
    assert state.claim() is None
    if allowance == 0:
        approved = ResearchProposals(state, compute, None).review(
            owner["user_id"],
            owner["organization_id"],
            topics[1]["id"],
            invoked["proposal_id"],
            True,
        )
        job = approved["result"]
    else:
        job = invoked
    agents.dispatch()
    assert state.claim() is None
    worker = compute.register_worker(
        owner["user_id"], owner["organization_id"], "Non-executing fixture", [image["id"]], image["maximum_resources"]
    )
    claimed = compute.claim(worker["token"])
    assert claimed["id"] == job["id"]
    compute.finish(worker["token"], claimed["id"], claimed["attempt_id"], claimed["lease"], "succeeded", 1, 0, None)
    agents.dispatch()
    resumed = state.claim()
    assert resumed
    packet = json.loads(json.loads(resumed["context_json"])[-1]["content"])
    assert packet["external_results"][0]["id"] == job["id"] and packet["external_results"][0]["status"] == "succeeded"
    assert state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"], 0, 20)
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM inference_calls").fetchone()[0] == 0


def test_ordinary_agent_review_returns_to_the_conversation_without_a_goal(installation):
    from test_organization_research_workbench import ReviewerFixture

    from reasoning_ledger.server.research_reviews import ResearchReviews

    state, _, owner, _, parent, _ = root(installation)
    tools = ResearchTools(state, parent)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "By reflexivity of equality."})["result"]
    tools.invoke("ledger_request_review", {"proof_id": proof["proof_id"]})
    finish(state, parent, "The exact proof revision is awaiting independent review.")
    agents = TopicAgents(state)
    agents.dispatch()
    assert state.claim() is None
    fixture = ReviewerFixture()
    asyncio.run(ResearchReviews(state).dispatch(fixture.provider))
    reviewer = state.claim()
    assert reviewer
    ResearchAgent(state, fixture.provider, reviewer, Quote(**json.loads(reviewer["quote_json"]))).execute()
    agents.dispatch()
    resumed = state.claim()
    assert resumed
    packet = json.loads(json.loads(resumed["context_json"])[-1]["content"])
    assert packet["external_results"][0]["kind"] == "review" and packet["external_results"][0]["status"] == "complete"
    assert state.run(owner["user_id"], owner["organization_id"], reviewer["id"])["status"] == "succeeded"
    assert len(fixture.requests) == 1


def test_agent_controls_work_before_delegation_and_resume_a_known_interruption(installation):
    state, client, owner, topics, parent, _ = root(installation)
    path = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/agents/control"
    assert client.post(path, json={"status": "paused"}).status_code == 200
    assert state.stopped(parent) == "agents_paused"
    state.finish(parent, "cancelled", usage=Usage(8, 20, 10), error="agents_paused")
    assert client.post(path, json={"status": "active"}).status_code == 200
    resumed = state.claim()
    assert resumed and resumed["id"] != parent["id"]
    assert client.post(path, json={"status": "cancelled"}).status_code == 200
    assert state.stopped(resumed) == "cancel_requested"


def test_conversational_controls_do_not_mutate_another_users_graph(installation):
    from test_public_server import invited

    state, _, owner, topics, parent, _ = root(installation)
    _, client, member = invited(installation)
    path = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/agents"
    assert client.post(path + "/control", json={"status": "paused"}).json()["threads"] == []
    assert client.post(path + f"/{parent['id']}/control", json={"status": "paused"}).status_code == 403
    assert not state.stopped(parent)
    assert member["user_id"] != owner["user_id"]
