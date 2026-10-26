"""Durable research goal budgets and approval races, without provider submission."""

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from test_public_server import installation as installation
from test_public_server import structure

from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.knowledge_store import KnowledgeStore
from reasoning_ledger.server.openrouter import Quote, Usage
from reasoning_ledger.server.research_goals import ResearchGoals
from reasoning_ledger.server.research_tools import ResearchProposals, ResearchTools
from reasoning_ledger.server.store import StoreError

QUOTE = Quote("test/math", "0.000001", "0.000002", "0", 20000, 8192, supports_tools=True)


@pytest.fixture
def research(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topics = structure(client, org)[2]
    store.add_key(user, org, "openrouter", "Fixture", "fixture-provider-credential")
    store.set_policy(user, org, [QUOTE.model], 10_000_000)
    store.post_message(user, org, topics[1]["id"], "Prove the lemma", "research-human-message")
    inference = InferenceStore(store)
    return store, client, owner, topics[1]["id"], inference, ResearchGoals(inference)


def create_goal(research, **changes):
    _, _, owner, topic, _, goals = research
    arguments = {
        "client_id": "research-goal-fixture",
        "objective": "Prove the lemma",
        "acceptance": ["A proof with hypotheses"],
        "model": QUOTE.model,
        "budget_microusd": 5_000_000,
        "max_turns": 10,
        "max_tool_calls": 10,
        "duration_seconds": 3600,
    }
    return goals.create(owner["user_id"], owner["organization_id"], topic, **(arguments | changes))


class QuoteProvider:
    def __init__(self, during_quote=None):
        self.during_quote = during_quote

    async def quotes(self, models):
        if self.during_quote:
            self.during_quote()
        return [QUOTE]


def start(research, **changes):
    store, _, owner, topic, inference, goals = research
    goal = create_goal(research, **changes)
    asyncio.run(goals.dispatch(QuoteProvider()))
    row = inference.claim()
    assert row is not None
    return goal, row, ResearchTools(inference, row)


def test_a_free_route_without_usage_has_known_zero_cost(research):
    from reasoning_ledger.server.openrouter import Quote

    store, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    free = Quote(row["model"], "0", "0", "0", 200000, 8192).record()
    assert inference.finish({**row, "quote_json": json.dumps(free)}, "failed", error="provider_network")
    with store.transaction() as db:
        assert db.execute("SELECT accounting_state FROM llm_runs WHERE id=?", (row["id"],)).fetchone()[0] == "released"


def test_goal_replay_validates_duration_and_released_calls_cost_zero(research):
    store, _, owner, topic, inference, goals = research
    goal, row, _ = start(research)
    with pytest.raises(StoreError, match="different input"):
        create_goal(research, duration_seconds=7200)
    assert inference.finish(row, "failed", no_charge=True, error="provider_rejected")
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["used_or_held_microusd"] == 0
    with store.transaction() as db:
        assert db.execute("SELECT reserved_microusd FROM llm_runs").fetchone()[0] > 0
    assert goal["objective"] == "Prove the lemma"


def test_goal_uses_normal_allowance_not_model_output_ceiling(research):
    _, _, _, _, inference, goals = research
    create_goal(research)

    class LargeOutputProvider:
        async def quotes(self, models):
            return [replace(QUOTE, context_length=1048576, max_completion_tokens=943718)]

    asyncio.run(goals.dispatch(LargeOutputProvider()))
    row = inference.claim()
    assert row is not None and json.loads(row["parameters_json"])["max_completion_tokens"] is None
    assert row["reserved_microusd"] < 100000


def test_explicit_resume_after_reconciled_failure_creates_fresh_request(research):
    _, _, owner, topic, inference, goals = research
    goal, row, _ = start(research)
    inference.finish(row, "failed", no_charge=True, error="provider_rejected")
    goals.stop(goal["id"], "blocked", "Review the rejection")
    resumed = goals.control(owner["user_id"], owner["organization_id"], topic, goal["id"], "active")
    assert resumed["run_id"] is None
    asyncio.run(goals.dispatch(QuoteProvider()))
    fresh = inference.claim()
    assert fresh["id"] != row["id"] and fresh["request_id"] != row["request_id"]
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["turns"] == 2


@pytest.mark.parametrize("code", ["agent_incomplete", "agent_empty_response", "agent_invalid_tool_arguments", "output_token_limit"])
def test_known_recoverable_failure_continues_goal_without_replaying_submission(research, code):
    store, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    inference.finish(row, "failed", usage=Usage(8, 20, 10), error=code)
    asyncio.run(goals.dispatch(QuoteProvider()))
    fresh = inference.claim()
    assert fresh is not None and fresh["id"] != row["id"] and fresh["request_id"] != row["request_id"]
    assert code in fresh["context_json"] and "do not replay completed side effects" in fresh["context_json"]
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["status"] == "active"
    with store.transaction() as db:
        old = db.execute("SELECT status,actual_microusd FROM llm_runs WHERE id=?", (row["id"],)).fetchone()
        assert tuple(old) == ("failed", 8)


@pytest.mark.parametrize("known,code", [(False, "agent_incomplete"), (True, "provider_rejected"), (True, "worker_interrupted")])
def test_ambiguous_or_nonrecoverable_failure_blocks_instead_of_spending_again(research, known, code):
    _, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    inference.finish(row, "failed", usage=Usage(8, 20, 10) if known else None, error=code)
    asyncio.run(goals.dispatch(QuoteProvider()))
    goal = goals.current(owner["user_id"], owner["organization_id"], topic)
    assert goal["status"] == "blocked" and goal["reason"] and inference.claim() is None


def test_goal_does_not_stop_after_three_turns_without_a_checkpoint(research):
    _, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    for _ in range(4):
        inference.finish(row, "succeeded", usage=Usage(8, 20, 10))
        asyncio.run(goals.dispatch(QuoteProvider()))
        row = inference.claim()
        assert row is not None
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["turns"] == 5


def test_unresolved_tool_outcome_prevents_automatic_goal_continuation(research):
    store, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO research_tool_receipts(id,organization_id,run_id,sequence,name,arguments_json,status,created_at) "
            "VALUES(?,?,?,1,'ledger_claim','{}','uncertain',?)",
            ("uncertain-receipt", owner["organization_id"], row["id"], int(time.time())),
        )
    inference.finish(row, "failed", usage=Usage(8, 20, 10), error="agent_incomplete")
    asyncio.run(goals.dispatch(QuoteProvider()))
    goal = goals.current(owner["user_id"], owner["organization_id"], topic)
    assert goal["status"] == "blocked" and "tool outcome" in goal["reason"] and inference.claim() is None


@pytest.mark.parametrize("name,arguments,continues", [
    ("web_fetch", {}, True), ("tool_call", {"name": "web_fetch", "arguments": {}}, True), ("ledger_claim", {}, False),
])
def test_read_only_source_failure_does_not_require_human_review_but_unknown_write_does(research, name, arguments, continues):
    store, _, owner, topic, inference, goals = research
    _, row, _ = start(research)
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO research_tool_receipts(id,organization_id,run_id,sequence,name,arguments_json,status,created_at) "
            "VALUES(?,?,?,1,?,?,'failed',?)",
            ("failed-tool-receipt", owner["organization_id"], row["id"], name, json.dumps(arguments), int(time.time())),
        )
    inference.finish(row, "failed", usage=Usage(8, 20, 10), error="agent_incomplete")
    asyncio.run(goals.dispatch(QuoteProvider()))
    goal = goals.current(owner["user_id"], owner["organization_id"], topic)
    assert goal["status"] == ("active" if continues else "blocked")
    assert (inference.claim() is not None) == continues


def test_uncertain_failure_cannot_resume(research):
    _, _, owner, topic, inference, goals = research
    goal, row, _ = start(research)
    inference.finish(row, "failed", error="provider_network")
    goals.stop(goal["id"], "blocked", "Review uncertain charge")
    with pytest.raises(StoreError, match="reconcile"):
        goals.control(owner["user_id"], owner["organization_id"], topic, goal["id"], "active")


def test_quote_wait_rechecks_deadline_and_preserves_user_pause(research):
    store, _, owner, topic, _, goals = research
    goal = create_goal(research)

    def pause():
        goals.control(owner["user_id"], owner["organization_id"], topic, goal["id"], "paused")
        goals.stop(goal["id"], "blocked", "Stale dispatcher observation")

    asyncio.run(goals.dispatch(QuoteProvider(pause)))
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["status"] == "paused"
    goals.control(owner["user_id"], owner["organization_id"], topic, goal["id"], "active")

    def expire():
        with store.transaction(write=True) as db:
            db.execute("UPDATE agent_goals SET deadline_at=? WHERE id=?", (int(time.time()) - 1, goal["id"]))

    asyncio.run(goals.dispatch(QuoteProvider(expire)))
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["status"] == "budget_limited"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM llm_runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_goal_events WHERE event_json LIKE '%Stale dispatcher%'").fetchone()[0] == 0


def test_failed_tool_attempt_consumes_budget_before_execution(research):
    store, _, owner, topic, _, goals = research
    _, _, tools = start(research, max_tool_calls=1)
    with pytest.raises(ValueError, match="tool refused"):
        tools.invoke("not_a_granted_tool", {})
    assert goals.current(owner["user_id"], owner["organization_id"], topic)["tool_calls"] == 1
    with pytest.raises(StoreError, match="tool budget exhausted"):
        tools.invoke("topic_history", {})
    with store.transaction() as db:
        receipts = db.execute("SELECT sequence,status FROM research_tool_receipts").fetchall()
    assert [tuple(receipt) for receipt in receipts] == [(1, "failed")]


def test_reconstructed_tool_registry_continues_receipt_sequence(research):
    store, _, _, _, inference, _ = research
    _, row, tools = start(research)
    tools.invoke("topic_history", {})
    ResearchTools(inference, row).invoke("topic_history", {})
    with store.transaction() as db:
        assert [row[0] for row in db.execute("SELECT sequence FROM research_tool_receipts ORDER BY sequence")] == [1, 2]


def proposal(research):
    _, client, _, _, inference, _ = research
    _, _, tools = start(research)
    item = tools._proposal(
        {
            "action": "knowledge",
            "kind": "ledger",
            "title": "Lemma",
            "body": "Proposed proof",
            "state": "note",
            "scope_kind": "topic",
            "scope_id": tools.topic,
        }
    )
    return item["proposal_id"], ResearchProposals(inference, client.app.state.compute, client.app.state.workflow_runs)


def test_durable_approval_claim_blocks_concurrent_rejection(research, monkeypatch):
    store, _, owner, topic, _, _ = research
    identifier, proposals = proposal(research)
    entered, release = Event(), Event()
    original = KnowledgeStore.create

    def held_create(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "create", held_create)
    with ThreadPoolExecutor(1) as pool:
        approval = pool.submit(proposals.review, owner["user_id"], owner["organization_id"], topic, identifier, True)
        assert entered.wait(10)
        try:
            with pytest.raises(StoreError, match="approval is already recorded"):
                proposals.review(owner["user_id"], owner["organization_id"], topic, identifier, False)
        finally:
            release.set()
        assert approval.result()["status"] == "approved"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 1
        assert db.execute("SELECT status FROM research_proposals").fetchone()[0] == "approved"


def test_rejection_before_approval_never_executes(research):
    store, _, owner, topic, _, _ = research
    identifier, proposals = proposal(research)
    args = owner["user_id"], owner["organization_id"], topic, identifier
    assert proposals.review(*args, False)["status"] == "rejected"
    assert proposals.review(*args, True)["status"] == "rejected"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 0


def test_approval_retry_after_committed_action_does_not_duplicate_entry(research, monkeypatch):
    store, _, owner, topic, _, _ = research
    identifier, proposals = proposal(research)
    original, crash = KnowledgeStore.create, [True]

    def crash_after_commit(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if crash[0]:
            crash[0] = False
            raise RuntimeError("Fixture crash after the action commit")
        return result

    monkeypatch.setattr(KnowledgeStore, "create", crash_after_commit)
    args = owner["user_id"], owner["organization_id"], topic, identifier
    with pytest.raises(RuntimeError, match="Fixture crash"):
        proposals.review(*args, True)
    first = proposals.review(*args, True)
    second = proposals.review(*args, True)
    assert first == second and first["status"] == "approved"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='research.proposal.approve'").fetchone()[0] == 1


def test_history_previews_recover_exact_slices_and_deny_alpha_sources(research):
    store, client, owner, topic, _, _ = research
    _, _, tools = start(research)
    content = "λ" * 16000
    for index in range(20):
        store.post_message(owner["user_id"], owner["organization_id"], topic, content, "long-source-" + str(index))
    page = tools.invoke("topic_history", {"limit": 50})["result"]
    assert page["truncated"] and len(json.dumps(page).encode()) < 128 * 1024
    source = next(message for message in page["messages"] if message["content_characters"] == len(content))
    exact = tools.invoke("topic_message_read", {"message_id": source["id"], "offset": 1200, "length": 1200})["result"]
    assert exact["content"] == content[1200:2400] and exact["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    tree = client.get(f"/api/organizations/{owner['organization_id']}/hierarchy").json()
    alpha = next(item for item in tree["topics"] if item["kind"] == "alpha")
    private_source = store.post_message(owner["user_id"], owner["organization_id"], alpha["id"], "Human-only", "alpha-source")
    with pytest.raises(ValueError, match="tool refused"):
        tools.invoke("topic_message_read", {"message_id": private_source["id"], "topic_id": alpha["id"]})
    with pytest.raises(ValueError, match="tool refused"):
        tools.invoke("topic_message_read", {"message_id": private_source["id"]})


def test_tool_receipt_recovery_does_not_cross_topic_boundaries(research):
    store, client, owner, topic, inference, _ = research
    _, _, tools = start(research)
    captured = tools.invoke("topic_history", {})
    recovered = tools.invoke("tool_receipt_read", {"receipt_id": captured["receipt_id"]})["result"]
    assert recovered["run_id"] == tools.row["id"] and recovered["encoding"] == "json"
    assert json.loads(recovered["text"]) == captured["result"]
    root = f"/api/organizations/{owner['organization_id']}"
    original_topic = next(item for item in client.get(root + "/hierarchy").json()["topics"] if item["id"] == topic)
    other = client.post(root + "/topics", json={"name": "other beta", "kind": "beta", "channel_id": original_topic["channel_id"]}).json()
    store.post_message(owner["user_id"], owner["organization_id"], other["id"], "Separate source", "separate-topic-source")
    inference.enqueue(
        owner["user_id"],
        owner["organization_id"],
        other["id"],
        "other-topic-inference",
        inference.parameters([QUOTE.model], 128, 50, None),
        [QUOTE],
    )
    other_row = inference.claim()
    other_receipt = ResearchTools(inference, other_row).invoke("topic_history", {})
    with pytest.raises(ValueError, match="tool refused"):
        tools.invoke("tool_receipt_read", {"receipt_id": other_receipt["receipt_id"]})


def test_agent_revision_proposal_uses_exact_revision_after_human_review(research):
    store, client, owner, topic, inference, _ = research
    _, _, tools = start(research)
    knowledge = KnowledgeStore(store)
    entry = knowledge.create(
        owner["user_id"],
        owner["organization_id"],
        client_id="research-revision-original",
        kind="ledger",
        title="Lemma",
        body="First conjecture",
        state="conjecture",
        scope_kind="topic",
        scope_id=topic,
    )
    args = {
        "kind": "ledger",
        "title": "Lemma",
        "body": "Refined conjecture",
        "state": "conjecture",
        "entry_id": entry["id"],
        "expected_revision": 1,
    }
    proposed = tools.invoke("knowledge_propose", args)["result"]
    assert knowledge.get(owner["user_id"], owner["organization_id"], entry["id"])["revision"] == 1
    proposals = ResearchProposals(inference, client.app.state.compute, client.app.state.workflow_runs)
    reviewed = proposals.review(owner["user_id"], owner["organization_id"], topic, proposed["proposal_id"], True)
    assert reviewed["result"]["revision"] == 2 and reviewed["result"]["body"] == "Refined conjecture"
    assert knowledge.get(owner["user_id"], owner["organization_id"], entry["id"], 1)["body"] == "First conjecture"
    with pytest.raises(ValueError, match="tool refused"):
        tools.invoke("knowledge_propose", args)
