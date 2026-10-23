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


def test_claim_citation_refusal_is_actionable_and_nonmutating(installation):
    state, owner, _, row, tools = source_run(installation)
    with pytest.raises(ValueError, match="unknown-reference-id"):
        tools.invoke("ledger_claim", {"statement": "An attributed statement.", "reference_id": "A URL or prose citation"})
    with state.store.transaction() as db:
        receipt = dict(db.execute("SELECT * FROM research_tool_receipts WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                                  (row["id"],)).fetchone())
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
    posted = knowledge.revise(owner["user_id"], owner["organization_id"], draft["id"], client_id="human-blog-publication-001",
                              kind="journal", title=draft["title"], body=draft["body"], state="published", expected_revision=1)
    revision = tools.invoke("blog_revise", {"entry_id": posted["id"], "title": "A result", "body": "Unpublished revision",
                                           "expected_revision": posted["revision"]})["result"]
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
        db.execute("UPDATE research_topic_settings SET context_policy_json=? WHERE organization_id=? AND topic_id=?",
                   (json.dumps({"review_enabled": False}), owner["organization_id"], topics[1]["id"]))
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
    verdict = {"verdict": "verified", "obligations": [{"statement": "2=2", "status": "holds"}],
               "strongest_objection": "None", "given_concerns": [], "source_access": "none", "report": "Reflexivity."}
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
    goals.create(user, org, topic, client_id="review-handoff-goal", objective="Prove reflexivity",
                 acceptance=["A reviewed proof"], model=MODELS[0], budget_microusd=1000000,
                 max_turns=10, max_tool_calls=100, duration_seconds=3600)
    asyncio.run(goals.dispatch(fixture.provider))
    row = state.claim()
    tools = ResearchTools(state, row)
    claim = tools.invoke("ledger_claim", {"statement": "The integer 2 equals itself."})["result"]
    proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
    review = ResearchReviews(state).request(tools, proof_id=proof["proof_id"])
    agent = ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"])))
    payload = {"messages": [{"role": "user", "content": "Continue"}], "tools": [
        {"type": "function", "function": {"name": tool.name}} for tool in tools.available()], "tool_choice": "auto"}
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
        settings = client.put(root + "/research-settings", json={"tools": ["history"], "prompt_profile": profile,
                              "additional_instructions": "Keep the exact hypotheses.", "review_enabled": True})
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
@pytest.mark.parametrize("status,text,expected", [("unavailable", "", False), ("failed", "", False),
                                                 ("partial", "One readable page", False), ("ready", "", False),
                                                 ("ready", "The exact source text", True)])
def test_unreadable_or_partial_pdf_receipt_cannot_attest_full_source_access(nested, status, text, expected):
    from reasoning_ledger.server.research_reviews import completed_source_evidence

    result = {"text": text, "extraction": {"status": status}}
    receipt = {"name": "tool_call" if nested else "web_fetch", "arguments_json": json.dumps({"name": "web_fetch"}),
               "result_json": json.dumps({"result": result} if nested else result)}
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
    assert not names & {"topic_history", "ledger_claim", "artifact_write", "blog_create", "compute_submit",
                        "media_begin", "media_chunk", "media_finish", "media_download_chunk"}
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
