"""Researcher/reviewer integration with deterministic, free HTTP fixtures."""

import asyncio
import json

import httpx
import pytest
from test_public_inference import MODELS, QUOTES, prepare
from test_public_server import installation as installation

from reasoning_ledger.server.mathematical_store import MathematicalStore
from reasoning_ledger.server.openrouter import OpenRouter, Quote, Usage
from reasoning_ledger.server.research_agent import ResearchAgent, TurnSuspended
from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.research_context import ResearchContext
from reasoning_ledger.server.research_reviews import ResearchReviews
from reasoning_ledger.server.research_tools import ResearchTools


def source_run(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    state.store.post_message(owner["user_id"], org, topic, "PRIVATE AUTHOR DISCUSSION: search strategy.", "discussion")
    quote = Quote(**{**QUOTES[0].record(), "supports_tools": True})
    state.enqueue(owner["user_id"], org, topic, "research-source", state.parameters([MODELS[0]], 512, 50, None), [quote])
    row = state.claim()
    tools = ResearchTools(state, row)
    client.put(
        f"/api/organizations/{org}/topics/{topic}/research-settings", json={"tools": sorted(tools.enabled), "reviewer_model": MODELS[1]}
    ).raise_for_status()
    return state, owner, topics, row, tools


def test_question_account_keeps_exact_candidates_retries_and_conflicts(installation):
    from reasoning_ledger.server.research_accounts import ResearchAccounts
    from reasoning_ledger.server.store import StoreError

    state, owner, topics, _, tools = source_run(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    accounts = ResearchAccounts(state.store)
    assert accounts.read(user, org, topic) is None
    first = accounts.update(
        user,
        org,
        topic,
        {
            "question": "Can H be removed?",
            "argument": "Step 1 uses H.",
            "gaps": "Find a replacement for step 1.",
            "failed_approaches": "A naive limit fails.",
        },
        0,
        "account-first",
    )
    second = accounts.update(user, org, topic, {"argument": "H is essential: a counterexample."}, 1, "account-second")
    assert second["body"]["gaps"] == first["body"]["gaps"]
    assert (
        accounts.update(
            user,
            org,
            topic,
            {
                "question": "Can H be removed?",
                "argument": "Step 1 uses H.",
                "gaps": "Find a replacement for step 1.",
                "failed_approaches": "A naive limit fails.",
            },
            0,
            "account-first",
        )["revision"]
        == 1
    )
    assert accounts.read(user, org, topic, 1)["body"]["argument"] == "Step 1 uses H."
    with pytest.raises(StoreError, match="changed"):
        accounts.update(user, org, topic, {"gaps": "Silently overwrite"}, 1, "stale-account")
    with pytest.raises(StoreError, match="identity"):
        accounts.update(user, org, topic, {"question": "Another question"}, 2, "account-first")
    with pytest.raises(StoreError, match="beta"):
        accounts.read(user, org, topics[0]["id"])
    with pytest.raises(StoreError, match="person"):
        accounts.update(user, org, topic, {"approach": "Another method"}, 2, "model-reset", run_id=tools.row["id"], new_approach=True)
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM mathematical_records").fetchone()[0] == 0
    # Discovery and bounded exact field readers share the same persistent account.
    result = tools.invoke("account_read", {"revision": 1, "field": "argument", "length": 6})["result"]
    assert result["text"] == "Step 1" and result["truncated"] and result["revision"] == 1

    from fastapi.testclient import TestClient
    from test_public_server import PASSWORD, PUBLIC_URL

    client = installation[2]
    invitation = client.post(f"/api/organizations/{org}/invitations", json={"email": "reader@example.org", "role": "viewer"}).json()
    reader = TestClient(client.app, base_url=PUBLIC_URL)
    session = reader.post(
        "/api/invitations/accept", json={"token": invitation["token"], "name": "Reader", "password": PASSWORD, "transport": "bearer"}
    ).json()
    reader.headers["Authorization"] = "Bearer " + session["access_token"]
    path = f"/api/organizations/{org}/topics/{topic}/research-account"
    assert reader.get(path).json()["revision"] == 2
    assert (
        reader.put(path, json={"expected_revision": 2, "changes": {"question": "Overwrite"}, "client_id": "reader-write"}).status_code
        == 403
    )
    assert (
        client.put(path, json={"expected_revision": True, "changes": {"question": "Overwrite"}, "client_id": "bool-version"}).status_code
        == 422
    )


def test_account_working_scope_preserves_premises_and_allows_explicit_expansion(installation):
    from reasoning_ledger.server.research_accounts import ResearchAccounts
    from reasoning_ledger.server.store import StoreError

    state, owner, topics, _, tools = source_run(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    premise = tools.invoke("ledger_claim", {"statement": "Hypothesis H holds."})["result"]
    target = tools.invoke("ledger_claim", {"statement": "The desired conclusion follows from H."})["result"]
    unrelated = tools.invoke("ledger_claim", {"statement": "An unrelated question remains open."})["result"]
    tools.invoke(
        "ledger_prove",
        {
            "claim_id": target["claim_id"],
            "argument": "Apply the hypothesis.",
            "premises": [{"claim_id": premise["claim_id"], "revision_id": premise["revision_id"]}],
        },
    )
    accounts = ResearchAccounts(state.store)
    with pytest.raises(StoreError, match="revision"):
        accounts.update(user, org, topic, {"question": "Prove the conclusion", "claim_ids": [target["claim_id"]]}, 0, "unread-pin")
    saved = accounts.update(
        user,
        org,
        topic,
        {"question": "Prove the conclusion", "claim_ids": [target["claim_id"]]},
        0,
        "root-pin",
        expected_links={target["claim_id"]: target["revision_id"]},
    )
    exact_links = tools.invoke("account_read", {"field": "links"})["result"]
    assert json.loads(exact_links["text"])["claim_ids"] == saved["body"]["pins"]["claim_ids"]
    scoped = tools.invoke("ledger_status", {"statements": "all"})["result"]
    wide = tools.invoke("ledger_status", {"scope": "project", "statements": "all"})["result"]
    assert {item["claim_id"] for item in scoped["claims"]} == {target["claim_id"], premise["claim_id"]}
    assert unrelated["claim_id"] in {item["claim_id"] for item in wide["claims"]} and scoped["knowledge_scope"]["dependency_count"] == 1
    assert (
        next(item["status"] for item in scoped["claims"] if item["claim_id"] == target["claim_id"])
        == next(item["status"] for item in wide["claims"] if item["claim_id"] == target["claim_id"])
        != "established"
    )
    assert tools.invoke("ledger_read", {"ledger_id": unrelated["claim_id"]})["result"]["claim_id"] == unrelated["claim_id"]
    revised = tools.invoke("ledger_refine", {"claim_id": target["claim_id"], "statement": "A changed conclusion."})["result"]
    # An account does not silently adopt a changed linked statement.
    assert accounts.read(user, org, topic)["linked_changes"] == [{"id": target["claim_id"], "changed": True, "unavailable": False}]
    material = accounts.read(user, org, topic)["body"]["pins"]["claim_ids"][0]
    assert (
        material["revision_id"]
        == saved["body"]["pins"]["claim_ids"][0]["revision_id"]
        != revised.get("revision_id", revised.get("current_revision_id"))
    )
    with pytest.raises(StoreError, match="revision"):
        accounts.update(
            user, org, topic, {"claim_ids": [target["claim_id"]]}, 1, "old-pin", expected_links={target["claim_id"]: target["revision_id"]}
        )
    with state.store.transaction() as db:
        packet = accounts.material(db, user, org, topic, 1)[1]
    assert packet["linked_evidence"][0]["record"]["statement_text"] == "The desired conclusion follows from H."


def test_scoped_entries_use_published_versions_and_keyset_pages(installation):
    from reasoning_ledger.server.knowledge_store import KnowledgeStore
    from reasoning_ledger.server.research_accounts import ResearchAccounts
    from reasoning_ledger.server.research_scope import KnowledgeWindow

    state, owner, topics, _, tools = source_run(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    knowledge = KnowledgeStore(state.store)
    created = []
    for index in range(4):
        created.append(
            knowledge.create(
                user,
                org,
                client_id=f"shared-source-{index:04d}",
                kind="bibliography",
                title=f"Source {index}",
                body="Read the full source.",
                state="active",
                scope_kind="topic",
                scope_id=topic,
            )
        )
    accounts = ResearchAccounts(state.store)
    accounts.update(
        user,
        org,
        topic,
        {"question": "Compare these sources", "entry_ids": [created[0]["id"]]},
        0,
        "entry-scope",
        expected_links={created[0]["id"]: 1},
    )
    small = tools.invoke("knowledge_search", {})["result"]
    assert [entry["id"] for entry in small["entries"]] == [created[0]["id"]]
    with state.store.transaction() as db:
        window = KnowledgeWindow(state.store, db, user, org, topic, "project")
        first = window.search_entries({"bibliography"}, limit=2)
        second = window.search_entries({"bibliography"}, limit=2, before=first["next_before"])
    assert len(first["entries"]) == len(second["entries"]) == 2
    assert {entry["id"] for entry in first["entries"]}.isdisjoint(entry["id"] for entry in second["entries"])
    draft = tools.invoke("blog_create", {"title": "A draft", "body": "Still working."})["result"]
    assert tools.invoke("blog_read", {"entry_id": draft["id"]})["result"]["state"] == "draft"
    assert draft["id"] not in [entry["id"] for entry in tools.invoke("knowledge_search", {"scope": "project"})["result"]["entries"]]


def test_account_critique_is_exact_bounded_and_does_not_certify(installation):
    from reasoning_ledger.server.research_accounts import ResearchAccounts

    state, owner, topics, row, tools = source_run(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    accounts, reviews = ResearchAccounts(state.store), ResearchReviews(state)
    claim = tools.invoke("ledger_claim", {"statement": "An unchecked assertion."})["result"]
    first = accounts.update(
        user,
        org,
        topic,
        {"question": "Why is 2=2?", "argument": "By reflexivity.", "gaps": "Check the exact hypotheses."},
        0,
        "critique-account",
    )
    admitted = reviews.request(tools, account_revision=1)
    assert admitted["status"] == "pending"
    accounts.update(user, org, topic, {"next_steps": "Await critique."}, 1, "critique-next-step")
    repeated = reviews.request(tools, account_revision=2)
    assert repeated["id"] == admitted["id"] and repeated["reused"]
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(reviews.dispatch(fixture.provider))
    child = state.claim()
    ResearchAgent(state, fixture.provider, child, Quote(**json.loads(child["quote_json"]))).execute()
    returned = reviews.list(user, org, topic)[0]
    assert returned["stale"] and returned["subject"]["account_revision"] == first["revision"]
    assert returned["result"]["canonical_status_changed"] is False
    assert "PRIVATE AUTHOR DISCUSSION" not in json.dumps(fixture.requests)
    with state.store.transaction() as db:
        assert MathematicalStore(state.store, db, user, org).state().claim_status[claim["claim_id"]]["status"] != "established"
        assert db.execute("SELECT COUNT(*) FROM mathematical_records WHERE record_type='verification'").fetchone()[0] == 0
    accounts.update(user, org, topic, {"argument": "An alternative explicit derivation."}, 2, "repair-one")
    second = reviews.request(tools, account_revision=3)
    assert second["id"] != admitted["id"]
    accounts.update(user, org, topic, {"argument": "Rephrasing alone cannot buy another critique."}, 3, "repair-two")
    assert reviews.request(tools, account_revision=4)["status"] == "review_limit"
    assert accounts.read(user, org, topic)["reviews_used"] == 2
    accounts.update(
        user, org, topic, {"approach": "Seek a counterexample instead of extending the argument."}, 4, "human-new-method", new_approach=True
    )
    assert accounts.read(user, org, topic)["review_cycle"] == 2 and accounts.read(user, org, topic)["reviews_used"] == 0

    # The configured automatic allowance covers critiques too; zero keeps the account itself.
    with state.store.transaction(write=True) as db:
        policy = json.loads(
            db.execute(
                "SELECT context_policy_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic)
            ).fetchone()[0]
        )
        policy["ledger_context_limit"] = 0
        db.execute(
            "UPDATE research_topic_settings SET context_policy_json=? WHERE organization_id=? AND topic_id=?",
            (json.dumps(policy), org, topic),
        )
    with state.store.transaction() as db:
        snapshot = ResearchContext(state.store).compile(db, org, topic, row["context_through_sequence"], QUOTES, user=user)[1]
    assert snapshot["layers"]["independent_reviews"] == 0 and not snapshot["ledger_revisions"]
    assert snapshot["account_revision"] == 5


def test_scoped_reads_reject_other_projects_alpha_and_revoked_sources(installation):
    from reasoning_ledger.server.knowledge_store import KnowledgeStore
    from reasoning_ledger.server.research_scope import authorize_knowledge_sources
    from reasoning_ledger.server.store import StoreError

    state, owner, topics, row, tools = source_run(installation)
    user, org = owner["user_id"], owner["organization_id"]
    knowledge = KnowledgeStore(state.store)
    alpha = knowledge.create(
        user,
        org,
        client_id="alpha-private-source",
        kind="bibliography",
        title="Never expose alpha",
        body="Private alpha text",
        state="active",
        scope_kind="topic",
        scope_id=topics[0]["id"],
    )
    client = installation[2]
    other = client.post(
        f"/api/organizations/{org}/projects", json={"name": "Another private project", "visibility": "private", "owner_id": user}
    ).json()
    hidden = knowledge.create(
        user,
        org,
        client_id="other-private-source",
        kind="bibliography",
        title="Other project secret",
        body="Not in this project's view",
        state="active",
        scope_kind="project",
        scope_id=other["id"],
    )
    assert hidden["id"] not in json.dumps(tools.invoke("knowledge_search", {"scope": "project"})["result"])
    with pytest.raises(ValueError):
        tools.invoke("knowledge_read", {"entry_id": hidden["id"]})
    assert alpha["id"] not in json.dumps(tools.invoke("knowledge_search", {"scope": "project"})["result"])
    with pytest.raises(ValueError):
        tools.invoke("knowledge_read", {"entry_id": alpha["id"]})
    with state.store.transaction(write=True) as db:
        captured = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
        captured["knowledge_source_topics"] = [topics[0]["id"]]
        db.execute("UPDATE research_contexts SET snapshot_json=? WHERE request_id=?", (json.dumps(captured), row["request_id"]))
    with pytest.raises(StoreError, match="knowledge source"):
        authorize_knowledge_sources(state.store, row)


def test_claim_citation_refusal_is_actionable_and_nonmutating(installation):
    state, owner, _, row, tools = source_run(installation)
    with pytest.raises(ValueError, match="unknown-reference-id"):
        tools.invoke("ledger_claim", {"statement": "An attributed statement.", "reference_id": "A URL or prose citation"})
    with state.store.transaction() as db:
        receipt = dict(
            db.execute("SELECT * FROM research_tool_receipts WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (row["id"],)).fetchone()
        )
        assert json.loads(receipt["result_json"])["code"] == "unknown_reference_id"
        assert db.execute("SELECT COUNT(*) FROM mathematical_records").fetchone()[0] == 0
    from reasoning_ledger.server.research_goals import failed_read

    assert failed_read(receipt)
    reference = tools.invoke("ledger_reference", {"author": "Author", "title": "Paper", "url": "https://example.org/paper"})["result"]
    claim = tools.invoke("ledger_claim", {"statement": "An attributed statement.", "reference_id": reference["reference_id"]})["result"]
    assert claim["reference_id"] == reference["reference_id"]


def test_agent_edits_published_blog_as_draft_without_replacing_publication(installation):
    state, owner, _, row, tools = source_run(installation)
    draft = tools.invoke("blog_create", {"title": "A result", "body": "Published result"})["result"]
    from reasoning_ledger.server.knowledge_store import KnowledgeStore
    from reasoning_ledger.server.research_blog import published_post

    knowledge = KnowledgeStore(state.store)
    posted = knowledge.revise(
        owner["user_id"],
        owner["organization_id"],
        draft["id"],
        client_id="human-blog-publication-001",
        kind="journal",
        title=draft["title"],
        body=draft["body"],
        state="published",
        expected_revision=1,
    )
    revision = tools.invoke(
        "blog_revise",
        {"entry_id": posted["id"], "title": "A result", "body": "Unpublished revision", "expected_revision": posted["revision"]},
    )["result"]
    assert revision["state"] == "draft" and revision["created_by_kind"] == "bot"
    assert published_post(state.store, owner["user_id"], owner["organization_id"], draft["id"])["body"] == "Published result"
    with pytest.raises(ValueError):
        tools.invoke("blog_revise", {"entry_id": posted["id"], "title": "Stale result", "body": "Stale revision", "expected_revision": 2})


class ReviewerFixture:
    def __init__(self, maximum=8192):
        self.requests = []
        self.maximum = maximum
        self.provider = OpenRouter(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": model,
                            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                            "context_length": 200000,
                            "supported_parameters": ["max_completion_tokens", "tools"],
                            "top_provider": {"max_completion_tokens": self.maximum},
                        }
                        for model in MODELS
                    ]
                },
            )
        self.requests.append(json.loads(request.content))
        verdict = {
            "verdict": "verified",
            "obligations": [{"statement": "2 equals 2", "status": "holds", "note": "Reflexivity."}],
            "strongest_objection": "None.",
            "given_concerns": [],
            "source_access": "full",
        }
        content = json.dumps(verdict) + "\nThe inference follows by reflexivity."
        events = [
            {"id": "gen-independent-review", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"cost": "0.000011", "prompt_tokens": 30, "completion_tokens": 20}},
        ]
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=stream, headers={"Content-Type": "text/event-stream"})


def test_independent_review_is_fresh_priced_and_establishes_exact_proof(installation):
    state, owner, topics, row, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "By reflexivity of equality, 2 = 2."})["result"]
    review = ResearchReviews(state).request(tools, proof_id=proof["proof_id"])
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture(maximum=131072)
    asyncio.run(ResearchReviews(state).dispatch(fixture.provider))
    child = state.claim()
    assert child and child["model"] != row["model"]
    assert json.loads(child["parameters_json"])["max_completion_tokens"] is None
    ResearchAgent(state, fixture.provider, child, Quote(**json.loads(child["quote_json"]))).execute()
    assert len(fixture.requests) == 1
    captured = json.dumps(fixture.requests[0])
    assert "PRIVATE AUTHOR DISCUSSION" not in captured
    assert "ledger_claim" not in captured and "topic_history" not in captured
    result = ResearchReviews(state).list(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert result[0]["id"] == review["id"] and result[0]["status"] == "complete", result
    with state.store.transaction() as db:
        ledger = MathematicalStore(state.store, db, owner["user_id"], owner["organization_id"])
        status = ledger.state().claim_status[claim["claim_id"]]
        assert status["status"] == "established" and status["authority"] == "review"
        call = db.execute("SELECT purpose FROM inference_calls WHERE run_id=?", (child["id"],)).fetchone()
        assert call[0] == "review"
        assert not db.execute("SELECT 1 FROM messages WHERE inference_run_id=?", (child["id"],)).fetchone()


@pytest.mark.parametrize("single_model", [False, True])
def test_review_selects_allowed_fresh_context_automatically_without_topic_setup(installation, single_model):
    state, owner, topics, row, tools = source_run(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    with state.store.transaction(write=True) as db:
        db.execute("DELETE FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic))
    if single_model:
        state.store.set_policy(user, org, [row["model"]], 10000000)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    reviews = ResearchReviews(state)
    admitted = reviews.request(tools, proof_id=proof["proof_id"])
    assert admitted["status"] == "pending" and admitted["fresh_context"]
    assert admitted["distinct_model"] == (not single_model)
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(reviews.dispatch(fixture.provider))
    child = state.claim()
    assert child and child["model"] == admitted["reviewer_model"], reviews.list(user, org, topic)
    ResearchAgent(state, fixture.provider, child, Quote(**json.loads(child["quote_json"]))).execute()
    assert "PRIVATE AUTHOR DISCUSSION" not in json.dumps(fixture.requests)
    result = reviews.list(user, org, topic)[0]
    assert result["status"] == "complete" and result["result"]["distinct_model"] == (not single_model)
    assert result["result"]["authority"] == "LLM-reviewed"


def test_review_can_be_explicitly_disabled_without_changing_tool_grants(installation):
    state, owner, topics, row, tools = source_run(installation)
    with state.store.transaction(write=True) as db:
        db.execute(
            "UPDATE research_topic_settings SET context_policy_json=? WHERE organization_id=? AND topic_id=?",
            (json.dumps({"review_enabled": False}), owner["organization_id"], topics[1]["id"]),
        )
    assert ResearchReviews(state).request(tools, claim_id="not-looked-up")["status"] == "disabled"
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM mathematical_reviews").fetchone()[0] == 0


def test_review_handoff_requests_checkpoint_then_yields_without_polling(installation):
    state, _, _, row, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    ResearchReviews(state).request(tools, proof_id=proof["proof_id"])
    agent = ResearchAgent(state, ReviewerFixture().provider, row, Quote(**json.loads(row["quote_json"])))
    with pytest.raises(TurnSuspended, match="independent review"):
        agent._transform({"messages": [{"role": "user", "content": "Continue"}], "tools": [], "tool_choice": "auto"})


@pytest.mark.parametrize("tool", ["review_submit", "tool_call"])
def test_structured_review_is_retained_through_direct_and_discovery_tools(installation, tool):
    state, owner, topics, row, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    reviews = ResearchReviews(state)
    reviews.request(tools, proof_id=proof["proof_id"])
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(reviews.dispatch(fixture.provider))
    reviewer = state.claim()
    review_tools = ResearchTools(state, reviewer)
    verdict = {
        "verdict": "verified",
        "obligations": [{"statement": "2=2", "status": "holds"}],
        "strongest_objection": "None",
        "given_concerns": [],
        "source_access": "none",
        "report": "Reflexivity.",
    }
    arguments = verdict if tool == "review_submit" else {"name": "review_submit", "arguments": verdict}
    review_tools.invoke(tool, arguments)
    with pytest.raises(ValueError, match="tool refused"):
        review_tools.invoke(tool, arguments)
    agent = ResearchAgent(state, fixture.provider, reviewer, Quote(**json.loads(reviewer["quote_json"])))
    with pytest.raises(TurnSuspended, match="structured review submitted"):
        agent._transform({"messages": [], "tools": []})
    state.finish(reviewer, "succeeded", usage=Usage(7, 20, 5))
    reviews.finish(reviewer)
    result = reviews.list(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert result[0]["status"] == "complete", result


def test_goal_checkpoints_waits_for_review_then_resumes_with_exact_result(installation):
    from reasoning_ledger.server.research_goals import ResearchGoals

    _, state, _, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    goals, fixture = ResearchGoals(state), ReviewerFixture()
    goals.create(
        user,
        org,
        topic,
        client_id="review-handoff-goal",
        objective="Prove reflexivity",
        acceptance=["A reviewed proof"],
        model=MODELS[0],
        budget_microusd=1000000,
        max_turns=10,
        max_tool_calls=100,
        duration_seconds=3600,
    )
    asyncio.run(goals.dispatch(fixture.provider))
    row = state.claim()
    tools = ResearchTools(state, row)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    review = ResearchReviews(state).request(tools, proof_id=proof["proof_id"])
    agent = ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"])))
    payload = {
        "messages": [{"role": "user", "content": "Continue"}],
        "tools": [{"type": "function", "function": {"name": tool.name}} for tool in tools.available()],
        "tool_choice": "auto",
    }
    checkpoint = agent._transform(payload)
    assert checkpoint["tool_choice"]["function"]["name"] == "goal_checkpoint"
    assert len(checkpoint["tools"]) == 1
    tools.invoke("goal_checkpoint", {"summary": "Pending review " + review["id"], "status": "active"})
    with pytest.raises(TurnSuspended, match="independent review"):
        agent._transform(payload)
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    asyncio.run(goals.dispatch(fixture.provider))
    assert state.claim() is None  # No polling turn while review is pending.
    asyncio.run(ResearchReviews(state).dispatch(fixture.provider))
    reviewer = state.claim()
    before = goals.current(user, org, topic)["tool_calls"]
    ResearchTools(state, reviewer).invoke("tool_search", {"query": "arxiv_read"})
    assert goals.current(user, org, topic)["tool_calls"] == before + 1
    ResearchAgent(state, fixture.provider, reviewer, Quote(**json.loads(reviewer["quote_json"]))).execute()
    asyncio.run(goals.dispatch(fixture.provider))
    continuation = state.claim()
    assert continuation and continuation["id"] != row["id"]
    assert review["id"] in continuation["context_json"] and "LLM-reviewed" in continuation["context_json"]


def test_prompt_profiles_pin_exact_context_and_do_not_expand_tool_grants(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    root = f"/api/organizations/{org}/topics/{topic}"
    for profile in ("workbench", "blueprint", "fact_graph", "author_critic", "lean"):
        settings = client.put(
            root + "/research-settings",
            json={
                "tools": ["history"],
                "prompt_profile": profile,
                "additional_instructions": "Keep the exact hypotheses.",
                "review_enabled": True,
            },
        )
        assert settings.status_code == 200
        current = client.get(root + "/research-instructions").json()
        assert current["prompt_profile"] == profile and "Keep the exact hypotheses." in current["system_prompt"]
        with state.store.transaction() as db:
            through = db.execute("SELECT MAX(sequence) FROM messages WHERE organization_id=? AND topic_id=?", (org, topic)).fetchone()[0]
            messages, snapshot = ResearchContext(state.store).compile(db, org, topic, through, [QUOTES[0]], user=owner["user_id"])
        assert messages[0]["content"] == current["system_prompt"]
        assert snapshot["prompt_version"] == current["prompt_version"] and snapshot["tool_groups"] == ["history"]
    # A partial tools update must not reset the chosen strategy or review policy.
    client.put(root + "/research-settings", json={"tools": ["history"], "context_target_percent": 70}).raise_for_status()
    assert client.get(root + "/research-settings").json()["prompt_profile"] == "lean"
    assert client.put(root + "/research-settings", json={"tools": [], "prompt_profile": "unknown"}).status_code == 422
    assert client.put(root + "/research-settings", json={"tools": [], "review_enabled": "yes"}).status_code == 422


def test_automatic_ledger_context_contains_only_matching_records_with_an_explicit_limit(installation):
    state, owner, topics, _, tools = source_run(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    for index in range(10):
        tools.invoke("ledger_claim", {"statement": f"Tensor kernel example {index} has explicit hypotheses."})
    tools.invoke("ledger_claim", {"statement": "An unrelated observation about prime numbers."})
    message = state.store.post_message(user, org, topic, "Please explain the tensor kernel.", "tensor-question")

    def compile_context():
        with state.store.transaction() as db:
            return ResearchContext(state.store).compile(db, org, topic, message["sequence"], QUOTES, user=user)[1]

    snapshot = compile_context()
    assert snapshot["ledger_context_limit"] == 8 and len(snapshot["ledger_revisions"]) == 8
    # Change the persisted topic policy through the authenticated API.
    _, _, client, _ = installation
    root = f"/api/organizations/{org}/topics/{topic}"
    for limit in (0, 2):
        client.put(root + "/research-settings", json={"tools": sorted(tools.enabled), "ledger_context_limit": limit}).raise_for_status()
        assert len(compile_context()["ledger_revisions"]) == limit
    assert client.put(root + "/research-settings", json={"tools": [], "ledger_context_limit": 21}).status_code == 422


def test_citation_cannot_pass_without_an_actual_source_read(installation):
    state, owner, topics, row, tools = source_run(installation)
    reference = tools.invoke("ledger_reference", {"author": "Example Author", "title": "A source", "url": "https://example.com/paper"})[
        "result"
    ]
    claim = tools.invoke(
        "ledger_claim", {"statement": "Every example has a property.", "reference_id": reference["reference_id"], "locator": "Theorem 1"}
    )["result"]
    ResearchReviews(state).request(tools, claim_id=claim["claim_id"])
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(ResearchReviews(state).dispatch(fixture.provider))
    child = state.claim()
    ResearchAgent(state, fixture.provider, child, Quote(**json.loads(child["quote_json"]))).execute()
    reviews = ResearchReviews(state).list(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert reviews[0]["result"]["verdict"]["verdict"] == "inconclusive", reviews


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "status,text,expected",
    [
        ("unavailable", "", False),
        ("failed", "", False),
        ("partial", "One readable page", False),
        ("ready", "", False),
        ("ready", "The exact source text", True),
    ],
)
def test_unreadable_or_partial_pdf_receipt_cannot_attest_full_source_access(nested, status, text, expected):
    from reasoning_ledger.server.research_reviews import completed_source_evidence

    result = {"text": text, "extraction": {"status": status}}
    receipt = {
        "name": "tool_call" if nested else "web_fetch",
        "arguments_json": json.dumps({"name": "web_fetch"}),
        "result_json": json.dumps({"result": result} if nested else result),
    }
    assert completed_source_evidence(receipt) is expected


def test_artifact_edits_have_compare_and_set_and_retained_revisions(installation):
    state, owner, topics, _, _ = source_run(installation)
    files = ResearchArtifacts(state.store)
    first = files.write(owner["user_id"], owner["organization_id"], topics[1]["id"], "proof.md", "First argument.")
    second = files.write(owner["user_id"], owner["organization_id"], topics[1]["id"], "proof.md", "Repaired argument.", first["sha256"])
    assert first["id"] == second["id"] and second["revision"] == 2
    old = files.read(owner["user_id"], owner["organization_id"], topics[1]["id"], "proof.md", revision=1)
    assert old["text"] == "First argument."


def test_cancelled_queued_review_never_executes_or_certifies(installation):
    state, owner, topics, row, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 4 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    reviews = ResearchReviews(state)
    review = reviews.request(tools, proof_id=proof["proof_id"])
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    fixture = ReviewerFixture()
    asyncio.run(reviews.dispatch(fixture.provider))
    reviews.cancel(owner["user_id"], owner["organization_id"], topics[1]["id"], review["id"])
    assert state.claim() is None
    asyncio.run(reviews.dispatch(fixture.provider))
    assert reviews.list(owner["user_id"], owner["organization_id"], topics[1]["id"])[0]["status"] == "cancelled"
    assert fixture.requests == []


def test_discovery_loads_exact_granted_schema_and_plan_reaches_next_context(installation):
    state, owner, topics, row, tools = source_run(installation)
    found = tools.invoke("tool_search", {"query": "lmfdb_query"})["result"]
    assert found[0]["name"] == "lmfdb_query"
    plan = [{"title": "Check the missing hypothesis.", "status": "in_progress"}]
    tools.invoke("tool_call", {"name": "tasks", "arguments": {"tasks": plan}})
    from reasoning_ledger.server.research_context import ResearchContext

    with state.store.transaction() as db:
        messages, snapshot = ResearchContext(state.store).compile(
            db,
            owner["organization_id"],
            topics[1]["id"],
            row["context_through_sequence"],
            [Quote(**json.loads(row["quote_json"]))],
            user=owner["user_id"],
        )
    assert "Check the missing hypothesis." in json.dumps(messages)
    assert snapshot["context_strategy"] == "layered-retrieval-v2"


def test_bibliography_exports_bibtex_and_fixed_arithmetic_never_executes_code(installation):
    _, _, _, _, tools = source_run(installation)
    reference = tools.invoke("bibliography_add", {"author": "Example Author", "title": "A mathematical paper.", "arxiv": "2401.00001"})[
        "result"
    ]
    text = tools.invoke("bibliography_export", {})["result"]
    assert text["format"] == "bibtex" and reference["reference_id"] in text["text"]
    result = tools.invoke("tool_call", {"name": "scientific_calculate", "arguments": {"expression": "1/3 + 1/6"}})["result"]
    assert result["result"]["exact"] == "1/2"


def test_independent_reviewer_receives_only_read_tools(installation):
    state, owner, topics, row, tools = source_run(installation)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 5 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    reviews = ResearchReviews(state)
    reviews.request(tools, proof_id=proof["proof_id"])
    state.finish(row, "succeeded", usage=Usage(7, 20, 5))
    asyncio.run(reviews.dispatch(ReviewerFixture().provider))
    reviewer = ResearchTools(state, state.claim())
    names = {tool.name for tool in reviewer.registry.list()}
    assert "tool_call" in names and "web_fetch" in names
    assert not names & {
        "topic_history",
        "ledger_claim",
        "artifact_write",
        "blog_create",
        "compute_submit",
        "media_begin",
        "media_chunk",
        "media_finish",
        "media_download_chunk",
    }
    assert "media_read" in names


def test_native_tool_round_retains_streamed_opaque_reasoning(installation):
    from test_public_research_agent import ResearchFixture, turn

    class ReasoningFixture(ResearchFixture):
        def handle(self, request):
            response = super().handle(request)
            if request.method == "GET" or len(self.requests) != 1:
                return response
            wire = response.content.decode().split("\n\n")
            first = json.loads(wire[0].removeprefix("data: "))
            first["choices"][0]["delta"]["reasoning_details"] = [
                {"index": 0, "type": "reasoning.encrypted", "id": "opaque-1", "data": "exact-", "format": "fixture-v1"}
            ]
            wire[0] = "data: " + json.dumps(first)
            wire.insert(
                1,
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "reasoning_details": [
                                        {
                                            "index": 0,
                                            "type": "reasoning.encrypted",
                                            "id": "opaque-1",
                                            "data": "continuation",
                                            "format": "fixture-v1",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ),
            )
            return httpx.Response(200, content="\n\n".join(wire), headers={"Content-Type": "text/event-stream"})

    fixture = ReasoningFixture()
    state, owner, _, row = turn(installation, fixture)
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "succeeded"
    assistant = next(message for message in fixture.requests[1]["messages"] if message.get("tool_calls"))
    assert assistant["reasoning_details"] == [
        {"index": 0, "type": "reasoning.encrypted", "id": "opaque-1", "data": "exact-continuation", "format": "fixture-v1"}
    ]
