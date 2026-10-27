"""Research turns use the shared native loop, exact source memory and priced calls."""

import asyncio
import json

import httpx
import pytest
from test_public_inference import KEY, MODELS, prepare
from test_public_server import installation as installation

from reasoning_ledger.server.inference_worker import InferenceWorker
from reasoning_ledger.server.openrouter import OpenRouter, Quote
from reasoning_ledger.server.research_agent import ResearchAgent
from reasoning_ledger.server.research_context import MEMORY_FIELDS, ResearchContext
from reasoning_ledger.server.research_goals import ResearchGoals
from reasoning_ledger.server.research_provider import cache_breakpoints
from reasoning_ledger.server.usage_store import UsageStore


def test_web_fetch_reads_pdf_text_and_reports_unreadable_originals(installation, monkeypatch):
    from test_organization_research_workbench import source_run
    from test_public_media import text_pdf

    _, _, _, _, tools = source_run(installation)
    body = text_pdf("An exact public source.")

    async def fetch(url):
        return body, {"content-type": "application/pdf"}

    monkeypatch.setattr("reasoning_ledger.server.research_tools.public_get", fetch)
    result = tools.invoke("web_fetch", {"url": "https://example.org/paper.pdf"})["result"]
    assert "An exact public source." in result["text"] and result["extraction"]["status"] == "ready"
    body = text_pdf(password="private")
    result = tools.invoke("web_fetch", {"url": "https://example.org/locked.pdf"})["result"]
    assert result["text"] == "" and result["extraction"]["error"] == "password_required"


def test_saved_tex_has_a_working_bounded_preview_and_complete_pinned_download(installation):
    import hashlib

    _, state, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/research-files"
    text = "\\documentclass{article}\n" + "Unicode α and a precise hypothesis.\n" * 1000
    saved = client.post(root, json={"path": "roadmap.tex", "text": text}).json()
    assert saved["revision"] == 1 and saved["url"] in saved["markdown"]
    preview = client.get(root + "/read", params={"path": "roadmap.tex"})
    assert preview.status_code == 200 and preview.json()["truncated"] is True
    assert len(preview.json()["text"]) == 16000
    download = client.get(saved["download_url"])
    assert download.status_code == 200 and download.content == text.encode("utf8")
    assert download.headers["ETag"] == '"' + hashlib.sha256(download.content).hexdigest() + '"'
    assert "attachment" in download.headers["Content-Disposition"] and "roadmap.tex" in download.headers["Content-Disposition"]
    second = client.post(root, json={"path": "roadmap.tex", "text": "A newer version.\n", "expected_sha256": saved["sha256"]})
    assert second.status_code == 200 and second.json()["revision"] == 2
    assert client.get(saved["url"]).content == text.encode("utf8")
    with state.store.transaction(write=True) as db:
        db.execute("DELETE FROM memberships WHERE organization_id=? AND user_id=?", (owner["organization_id"], owner["user_id"]))
    denied = client.get(saved["download_url"])
    assert denied.status_code in {403, 404} and text not in denied.text


def test_chunked_project_file_download_is_complete_and_checks_its_retained_digest(installation):
    from reasoning_ledger.server.research_artifacts import ResearchArtifacts

    _, state, client, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    files = ResearchArtifacts(state.store)
    text = "An exact UTF-8 source line: αβγ\n" * 14000
    with state.store.transaction(write=True) as db:
        project = state.store._topic_access(db, user, org, topic)[1]["id"]
        files._write(db, user, org, project, "long.tex", text, topic=topic)
    saved = files.read(user, org, topic, "long.tex")
    assert saved["truncated"] is True and client.get(saved["download_url"]).content == text.encode()
    with state.store.transaction(write=True) as db:
        # Inject storage corruption past the normal immutability guard.
        db.execute("DROP TRIGGER research_chunk_immutable")
        db.execute("UPDATE research_blob_chunks SET text='corrupt' WHERE organization_id=? AND ordinal=0", (org,))
    assert client.get(saved["download_url"]).status_code == 409


class ResearchFixture:
    def __init__(self, mode="tools"):
        self.mode, self.requests, self.turn = mode, [], 0
        self.provider = OpenRouter(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": MODELS[0],
                            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                            "context_length": 200000,
                            "supported_parameters": ["max_completion_tokens", "tools"],
                            "top_provider": {"max_completion_tokens": 8192},
                        }
                    ]
                },
            )
        assert request.headers["Authorization"] == "Bearer " + KEY
        body = json.loads(request.content)
        self.requests.append(body)
        if self.mode == "credit_first" or (
            len(self.requests) > 1 and self.mode in {"credit_later", "credit_continuation", "checkpoint_credit"}
        ):
            return httpx.Response(402, json={"error": {"message": KEY, "metadata": {
                "limit_source": "openrouter_credits", "reason": "weight_exceeds_budget", "remedy_hint": KEY,
            }}})
        generation = "gen-fixture-" + str(len(self.requests))
        compacting = "Compact mathematical research" in body["messages"][0]["content"]
        if compacting:
            sources = json.loads(body["messages"][-1]["content"])
            memory = {field: [] for field in MEMORY_FIELDS}
            memory["definitions"] = [
                {"text": "V is a vector space over Q; its dimension is not established.", "sources": [sources["source_messages"][0]["id"]]}
            ]
            if self.mode == "fenced_compaction":
                # Near-valid output: fenced, with one item citing an invented message ID and a field left out.
                memory["claims"] = [{"text": "An invented consensus.", "sources": ["not-a-message"]}]
                memory.pop("next_steps")
                content = "Here is the memory:\n```json\n" + json.dumps(memory) + "\n```"
            else:
                content = json.dumps(memory) if self.mode != "bad_compaction" else "unsupported ungrounded summary"
            # The output limit ended the reply; whatever it holds may have lost later items.
            delta, finish = {"content": content}, "length" if self.mode == "truncated_compaction" else "stop"
        elif (self.turn == 0 and self.mode in {
            "tools", "uncertain", "goal", "credit_later", "invalid_arguments", "invalid_arguments_once", "gemini_nullable",
            "checkpoint_credit"
        }) or (self.mode == "invalid_arguments" and self.turn == 1):
            self.turn += 1
            name = "goal_checkpoint" if self.mode == "goal" else "topic_history"
            arguments = (
                {"summary": "The supplied acceptance criterion is met by the cited argument.", "status": "complete"}
                if name == "goal_checkpoint"
                else {}
            )
            if self.mode == "checkpoint_credit":
                name = "answer_checkpoint"
                arguments = {"summary": "The finite-dimensional case follows by rank-nullity.",
                             "remaining": "The infinite-dimensional case is unresolved.", "sources": ["Given linear map, equation (1)"]}
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-exact-source",
                        "type": "function",
                        "function": {
                            "name": name, "arguments": "{" + KEY if self.mode.startswith("invalid_arguments") else json.dumps(arguments),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            if self.mode == "uncertain":
                return httpx.Response(503, json={"error": {"message": KEY}})
            delta, finish = {"content": "The argument proves the stated result; no missing hypothesis is supplied."}, "stop"
            if self.mode == "invalid_unicode":
                delta = {"content": "Invalid scalar: \ud800"}
            elif self.mode == "empty":
                delta = {}
            elif self.mode == "reasoning_only" and len(self.requests) <= 2:
                delta = {"reasoning_details": [{"type": "reasoning.text", "text": "Fixture reasoning", "index": 0}]}
                finish = "length"
            elif self.mode == "summary":
                delta["reasoning_details"] = [{"type": "reasoning.summary", "text": "Checking the hypotheses.", "index": 0},
                    {"type": "reasoning.text", "text": "Opaque fixture reasoning", "index": 1}]
            elif self.mode == "unexpected_tool":
                delta = {"tool_calls": [{"index": 0, "id": "call-not-granted", "type": "function", "function": {
                    "name": "ledger_claim", "arguments": json.dumps({"statement": "Not authorized by a gamma step"})}}]}
                finish = "tool_calls"
            elif self.mode in {"long", "limit", "credit_continuation"}:
                delta = {"content": f"Segment {len(self.requests)}. "}
                finish = "length" if self.mode != "long" or len(self.requests) < 6 else "stop"

        def event(value):
            return "data: " + json.dumps(value) + "\n\n"

        if self.mode in {"gemini_nullable", "gemini_bad"}:
            delta["reasoning_details"] = [{"type": "reasoning.text", "text": "Fixture thought", "id": None,
                "format": "google-gemini-v1", "index": 0, "signature": None if self.mode == "gemini_nullable" else {"secret": KEY}}]
        wire = event({"id": generation, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        wire += event({"id": generation, "choices": [], "usage": {"cost": "0.000008", "prompt_tokens": 20, "completion_tokens": 10}})
        wire += "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)


def turn(installation, fixture, maximum=256):
    _, state, _, owner, topics = prepare(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    parameters = state.parameters([MODELS[0]], maximum, 50, None)
    state.enqueue(owner["user_id"], owner["organization_id"], topics[1]["id"], "research-test", parameters, [quote])
    claimed = state.claim()
    ResearchAgent(state, fixture.provider, claimed, quote).execute()
    return state, owner, topics, claimed


def test_budget_refusal_keeps_a_useful_checkpoint_without_another_paid_retry(installation):
    fixture = ResearchFixture("checkpoint_credit")
    state, owner, topics, row = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert len(fixture.requests) == 2 and result["status"] == "failed"
    assert result["answer_state"]["status"] == "partial"
    assert "finite-dimensional" in result["answer_state"]["checkpoint"]["summary"]
    with state.store.transaction() as db:
        message = db.execute("SELECT content FROM messages WHERE id=?", (result["message_id"],)).fetchone()[0]
    assert "infinite-dimensional" in message and "Not a completed answer" in message
    assert result["actual_microusd"] == 8


@pytest.mark.parametrize("stop", ["interrupted", "timeout", "review"])
def test_checkpoint_survives_interruption_timeout_or_review_handoff(installation, stop):
    from test_organization_research_workbench import source_run

    from reasoning_ledger.server.research_reviews import ResearchReviews

    state, owner, _, row, tools = source_run(installation)
    tools.invoke("answer_checkpoint", {"summary": "A precise provisional answer.",
                                      "remaining": "Check the stated hypothesis.", "sources": ["Definition 2"]})
    if stop == "review":
        claim = tools.invoke("ledger_claim", {"statement": "Every integer equals itself."})["result"]
        proof = tools.invoke("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."})["result"]
        ResearchReviews(state).request(tools, proof_id=proof["proof_id"])
    fixture = ResearchFixture("final")
    agent = ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"])),
                          stopped=lambda: stop == "interrupted", timeout=-1 if stop == "timeout" else 1800)
    agent.execute()
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert fixture.requests == []
    assert result["answer_state"]["status"] == ("waiting" if stop == "review" else "partial")
    assert result["answer_state"]["reason"] == {"review": "independent review", "interrupted": "worker_interrupted",
                                               "timeout": "execution_timeout"}[stop]
    with state.store.transaction() as db:
        message = db.execute("SELECT content FROM messages WHERE id=?", (result["message_id"],)).fetchone()[0]
    assert "precise provisional answer" in message and "Check the stated hypothesis" in message
    assert "not a completed answer" in message.lower()


@pytest.mark.parametrize("mode", ["research", "plain_comparison", "plain_response"])
def test_effective_invocation_tools_and_utc_date_match_the_provider_request(installation, mode):
    import datetime

    _, state, _, owner, topics = prepare(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=mode != "plain_response")
    parameters = state.parameters([MODELS[0]], 256, 50, None)
    kwargs = {"workflow_context": [{"role": "system", "content": "Answer precisely."}, {"role": "user", "content": "A question."}]} \
        if mode == "plain_comparison" else {}
    if kwargs:
        with state.store.transaction(write=True) as db:
            state._enqueue(db, owner["user_id"], owner["organization_id"], topics[2]["id"],
                           "capabilities-" + mode, parameters, [quote], **kwargs)
    else:
        state.enqueue(owner["user_id"], owner["organization_id"], topics[1]["id"], "capabilities-" + mode, parameters, [quote])
    row, fixture = state.claim(), ResearchFixture("final")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    capabilities = result["capabilities"]
    assert capabilities["mode"] == mode
    assert capabilities["current_date"] == datetime.datetime.now(datetime.UTC).date().isoformat()
    advertised = [tool["function"]["name"] for tool in fixture.requests[0].get("tools", [])]
    assert sorted(advertised) == sorted(capabilities["advertised_tools"])
    assert ("web_fetch" in capabilities["discoverable_tools"]) == (mode == "research")
    assert capabilities["current_date"] in fixture.requests[0]["messages"][1]["content"]


def test_cancelling_during_a_handoff_keeps_the_checkpoint_partial(installation):
    from test_organization_research_workbench import source_run

    state, owner, _, row, tools = source_run(installation)
    tools.invoke("answer_checkpoint", {"summary": "Retained useful progress.", "remaining": "An open gap.", "sources": []})
    with state.store.transaction(write=True) as db:
        state._event(db, owner["organization_id"], row["id"], {"type": "agent", "phase": "waiting", "reason": "independent review"})
    state.cancel(owner["user_id"], owner["organization_id"], row["id"])
    state.finish(row, "succeeded", no_charge=True)
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert result["status"] == "cancelled" and result["answer_state"]["status"] == "partial"
    with state.store.transaction() as db:
        message = db.execute("SELECT content FROM messages WHERE id=?", (result["message_id"],)).fetchone()[0]
    assert "Retained useful progress" in message and "Partial response: cancelled" in message and "*Waiting:" not in message


@pytest.mark.parametrize("native_status", ["failed", "completed"])
def test_final_admission_revocation_is_cancelled_even_without_another_native_callback(installation, monkeypatch, native_status):
    from reasoning_ledger.agents.contracts import AgentRunResult

    config, _, _, owner = installation

    def refused(executor, prepared, emit, *, cancelled):
        from reasoning_ledger.server.store import Store

        store = Store(config.data_dir)
        key = store.keys(owner["user_id"], owner["organization_id"])[0]
        store.disable_key(owner["user_id"], owner["organization_id"], key["id"])
        return AgentRunResult(prepared.run_id, native_status, output_text="Not authorized for publication", error="admission refused")

    monkeypatch.setattr("reasoning_ledger.server.research_agent.ReasoningNativeExecutor.execute", refused)
    fixture = ResearchFixture()
    state, owner, _, row = turn(installation, fixture)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "cancelled" and run["error_code"] == "key_disabled"
    assert not fixture.requests


def test_beta_tool_loop_records_each_call_and_returns_exact_sources(installation):
    fixture = ResearchFixture()
    state, owner, topics, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded" and result["actual_microusd"] == 16
    assert len(fixture.requests) == 2
    assert all(request["usage"] == {"include": True} for request in fixture.requests)
    tools = [message for message in fixture.requests[1]["messages"] if message["role"] == "tool"]
    assert len(tools) == 1 and json.loads(tools[0]["content"])["result"]["receipt_id"]
    assert "Compute a shared result" in tools[0]["content"]
    with state.store.transaction() as db:
        calls = db.execute("SELECT * FROM inference_calls WHERE run_id=? ORDER BY sequence", (run["id"],)).fetchall()
        assert len(calls) == 2 and calls[1]["parent_call_id"] == calls[0]["id"]
        assert [call["purpose"] for call in calls] == ["answer", "continuation"]
        assert all(json.loads(call["quote_json"])["supports_tools"] for call in calls)
        assert (
            db.execute("SELECT count(*) FROM research_tool_receipts WHERE run_id=? AND status='completed'", (run["id"],)).fetchone()[0] == 1
        )
    assert (
        ResearchContext(state.store).inspect(owner["user_id"], owner["organization_id"], topics[1]["id"])["snapshot"]["policy"]
        == "token-budgeted-cited-memory"
    )


def cached(message):
    return isinstance(message.get("content"), list) and any("cache_control" in part for part in message["content"])


def test_cache_breakpoints_mark_a_copy_at_the_system_prompt_and_conversation_ends():
    messages = [
        {"role": "system", "content": "Instructions."},
        {"role": "user", "content": [{"type": "text", "text": "Question."}, {"type": "image_url", "image_url": {"url": "data:"}}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function"}]},
        {"role": "tool", "tool_call_id": "call", "content": "Result."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "next", "type": "function"}]},
    ]
    original = json.loads(json.dumps(messages))
    marked, end = cache_breakpoints(messages)
    assert messages == original, "the agent's own messages are never changed"
    assert end == 3 and [index for index, message in enumerate(marked) if cached(message)] == [0, 3]
    assert marked[0]["content"] == [{"type": "text", "text": "Instructions.", "cache_control": {"type": "ephemeral"}}]
    assert marked[3]["tool_call_id"] == "call" and marked[3]["content"][0]["text"] == "Result."
    later, end = cache_breakpoints([*messages, {"role": "tool", "tool_call_id": "next", "content": "More."}], previous=1)
    assert end == 5 and [index for index, message in enumerate(later) if cached(message)] == [0, 1, 5]
    assert "cache_control" in later[1]["content"][0] and "cache_control" not in later[1]["content"][1]


def test_cache_write_price_bounds_an_anthropic_reservation():
    pricing = {"input_cache_write": "0.00000125", "input_cache_read": "0.0000001"}
    messages = [{"role": "user", "content": "x" * 30_000}]
    plain = Quote("test/one", "0.000001", "0.000002", "0", 200000, 8192, pricing).reservation(messages, 0)
    anthropic = Quote("anthropic/fixture", "0.000001", "0.000002", "0", 200000, 8192, pricing).reservation(messages, 0)
    assert anthropic == pytest.approx(plain * 1.25, rel=0.001)
    assert Quote("anthropic/fixture", "0.000001", "0.000002", "0", 200000, 8192).reservation(messages, 0) == plain


@pytest.mark.parametrize("model", ["anthropic/fixture", MODELS[0]])
def test_anthropic_research_rounds_send_cache_breakpoints_and_retain_unmarked_sessions(installation, model):
    _, state, client, owner, topics = prepare(installation)
    org = owner["organization_id"]
    client.put(f"/api/organizations/{org}/policy",
               json={"allowed_models": [*MODELS, "anthropic/fixture"], "monthly_budget_microusd": 1_000_000}).raise_for_status()
    quote = Quote(model, "0.000001", "0.000002", "0", 200000, 8192, {"input_cache_write": "0.00000125"}, supports_tools=True)
    state.enqueue(owner["user_id"], org, topics[1]["id"], "cache-" + model, state.parameters([model], 256, 50, None), [quote])
    fixture, row = ResearchFixture(), state.claim()
    ResearchAgent(state, fixture.provider, row, quote).execute()
    assert state.run(owner["user_id"], org, row["id"])["status"] == "succeeded" and len(fixture.requests) == 2
    first, second = ([index for index, message in enumerate(request["messages"]) if cached(message)] for request in fixture.requests)
    if model != "anthropic/fixture":
        assert first == second == [], "other models cache automatically or not at all"
        return
    assert first == [0, len(fixture.requests[0]["messages"]) - 1]
    # The second round re-reads the first round's conversation, then marks the tool result that ends it.
    assert second == [0, first[1], len(fixture.requests[1]["messages"]) - 1]
    assert fixture.requests[1]["messages"][-1]["role"] == "tool"
    with state.store.transaction() as db:
        session = json.loads(db.execute("SELECT state_json FROM research_sessions WHERE run_id=?", (row["id"],)).fetchone()[0])
    assert "cache_control" not in json.dumps(session)


class ManyToolRounds(ResearchFixture):
    """Free provider fixture with visible progress alongside tools."""
    def __init__(self, rounds=30, progress=None):
        super().__init__()
        self.rounds = rounds
        self.progress = progress

    def handle(self, request):
        if request.method == "GET":
            return super().handle(request)
        body = json.loads(request.content)
        self.requests.append(body)
        index = len(self.requests)
        if index <= self.rounds:
            assert body.get("tools"), "research tools disappeared before the authorized budget was exhausted"
            delta = {"content": self.progress or f"Checking step {index}. ", "tool_calls": [{"index": 0, "id": f"call-{index}",
                "type": "function", "function": {"name": "ledger_status", "arguments": "{}"}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"content": "The remaining obligation needs another research turn."}, "stop"
        events = [
            {"id": f"gen-tools-{index}", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
            {"choices": [], "usage": {"cost": "0.000008", "prompt_tokens": 20, "completion_tokens": 10}},
        ]
        wire = "".join("data: " + json.dumps(value) + "\n\n" for value in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)


def test_streamed_tool_progress_and_final_answer_are_retained_once_without_rewriting(installation):
    fixture = ManyToolRounds(rounds=2)
    state, owner, _, row = turn(installation, fixture, maximum=None)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "succeeded" and run["accounting_state"] == "settled"
    assert run["output_text"] == "Checking step 1. \n\nChecking step 2. \n\nThe remaining obligation needs another research turn."


def test_tool_rounds_without_trailing_space_remain_separate_and_have_first_token_timing(installation):
    fixture = ManyToolRounds(rounds=1, progress="I'll record the references now.")
    state, owner, _, row = turn(installation, fixture, maximum=None)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["output_text"] == "I'll record the references now.\n\nThe remaining obligation needs another research turn."
    calls = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]
    assert all(0 <= call["time_to_response_headers_ms"] <= call["time_to_first_token_ms"] for call in calls)
    assert run["timing"]["tool_execution_ms"] is not None
    assert len(calls) == 2
    assert all(0 <= call["time_to_first_token_ms"] <= call["latency_ms"] for call in calls)


def test_round_boundaries_do_not_change_provider_token_fragments_or_length_continuations():
    from reasoning_ledger.server.research_provider import ResearchCompletion

    client = object.__new__(ResearchCompletion)
    client._last_visible_finish = "tool_calls"
    assert client._display_round("Result.", "Next") == "Result.\n\nNext"
    assert client._display_round("Result.\n", "Next") == "Result.\n\nNext"
    assert client._display_round("Result.\n\n", "Next") == "Result.\n\nNext"
    assert client._display_round("Result.", "") == "Result."
    client._last_visible_finish = "length"
    assert client._display_round("inter", "national") == "international"


def test_research_turn_has_no_implicit_24_tool_ceiling(installation):
    fixture = ManyToolRounds()
    state, owner, _, row = turn(installation, fixture, maximum=None)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "succeeded" and len(fixture.requests) == 31
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_tool_receipts WHERE run_id=? AND status='completed'",
                          (row["id"],)).fetchone()[0] == 30


def test_goal_continues_after_many_tools_and_visible_progress_without_replaying(installation):
    _, state, _, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    goals, fixture = ResearchGoals(state), ManyToolRounds()
    goals.create(user, org, topic, client_id="long-goal-fixture", objective="Investigate a precise lemma",
                 acceptance=["An independently reviewed proof"], model=MODELS[0], budget_microusd=1000000,
                 max_turns=10, max_tool_calls=100, duration_seconds=3600)
    asyncio.run(goals.dispatch(fixture.provider))
    row = state.claim()
    ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"]))).execute()
    assert state.run(user, org, row["id"])["status"] == "succeeded"
    assert goals.current(user, org, topic)["tool_calls"] == 30
    asyncio.run(goals.dispatch(fixture.provider))
    next_run = state.claim()
    assert next_run and next_run["id"] != row["id"] and next_run["request_id"] != row["request_id"]
    assert goals.current(user, org, topic)["status"] == "active"
    assert "do not replay completed side effects" in next_run["context_json"]


def test_explicit_goal_tool_budget_still_stops_without_extra_execution(installation):
    _, state, _, owner, topics = prepare(installation)
    org, topic, user = owner["organization_id"], topics[1]["id"], owner["user_id"]
    goals, fixture = ResearchGoals(state), ManyToolRounds(rounds=2)
    goals.create(user, org, topic, client_id="explicit-tools-goal", objective="Investigate a lemma",
                 acceptance=["An independently reviewed proof"], model=MODELS[0], budget_microusd=1000000,
                 max_turns=10, max_tool_calls=2, duration_seconds=3600)
    asyncio.run(goals.dispatch(fixture.provider))
    row = state.claim()
    ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"]))).execute()
    assert state.run(user, org, row["id"])["status"] == "succeeded"
    assert "tools" not in fixture.requests[-1]
    asyncio.run(goals.dispatch(fixture.provider))
    assert state.claim() is None and goals.current(user, org, topic)["status"] == "budget_limited"
    assert goals.current(user, org, topic)["tool_calls"] == 2


def test_internal_execution_error_is_not_context_failure_or_secret_payload(installation, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("private runtime payload " + KEY)

    monkeypatch.setattr("reasoning_ledger.server.research_agent.ResearchTools", fail)
    state, owner, _, row = turn(installation, ResearchFixture(), maximum=None)
    result = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert result["error_code"] == "agent_execution_error"
    events = state.events(owner["user_id"], owner["organization_id"], row["id"], 0)
    assert "ValueError" in json.dumps(events) and KEY not in json.dumps(events)


def test_reasoning_only_rounds_continue_with_preserved_evidence_and_current_holds(installation):
    fixture = ResearchFixture("reasoning_only")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded" and result["actual_microusd"] == 24
    assert len(fixture.requests) == 3
    assert any(message.get("reasoning_details") for message in fixture.requests[1]["messages"])
    assert result["reserved_microusd"] == 24
    assert result["cost_progress"] == {"confirmed_microusd": 24, "next_call_estimate_microusd": 0, "internal_hold_microusd": 0}
    assert [item["round"] for item in result["activity"] if item["phase"] == "round"] == [1, 2, 3]


def test_progress_exposes_brief_provider_summary_not_raw_reasoning(installation):
    state, owner, _, run = turn(installation, ResearchFixture("summary"))
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded"
    assert [event["summary"] for event in result["activity"] if event["phase"] == "reasoning_summary"] == ["Checking the hypotheses."]
    assert "Opaque fixture reasoning" not in json.dumps(result["activity"])


def test_gamma_llm_does_not_gain_implicit_research_tools(installation):
    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[2]["id"]
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    with state.store.transaction(write=True) as db:
        state._enqueue(db, user, org, topic, "gamma-tool-denial-001", state.parameters([MODELS[0]], None, 50, None), [quote],
                       workflow_context=[{"role": "system", "content": "A workflow model step."},
                                         {"role": "user", "content": "Answer only."}])
    row = state.claim()
    fixture = ResearchFixture("unexpected_tool")
    ResearchAgent(state, fixture.provider, row, Quote(**json.loads(row["quote_json"]))).execute()
    assert "tools" not in fixture.requests[0]
    assert state.run(user, org, row["id"])["error_code"] == "unsupported_tool_call"
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM mathematical_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM research_tool_receipts").fetchone()[0] == 0


def test_nullable_reasoning_signature_survives_tool_continuation(installation):
    fixture = ResearchFixture("gemini_nullable")
    state, owner, _, run = turn(installation, fixture, maximum=None)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded" and result["actual_microusd"] == 16
    assert len(fixture.requests) == 2
    assistant = next(message for message in fixture.requests[1]["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_details"][0]["signature"] is None
    assert assistant["reasoning_details"][0]["id"] is None
    assert all("max_completion_tokens" not in request for request in fixture.requests)


def test_protocol_failure_identifies_reasoning_stage_without_leaking_payload(installation):
    fixture = ResearchFixture("gemini_bad")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["accounting_state"] == "uncertain"
    assert result["diagnostics"]["protocol_stage"] == "reasoning"
    assert KEY not in json.dumps(result["diagnostics"])


def test_queued_turn_executes_and_reports_its_captured_prompt(installation):
    _, state, _, owner, topics = prepare(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(
        owner["user_id"], owner["organization_id"], topics[1]["id"], "captured-prompt",
        state.parameters([MODELS[0]], 256, 50, None), [quote],
    )
    row = state.claim()
    messages = json.loads(row["context_json"])
    messages[0]["content"] = "Captured mathematical instructions for this queued turn."
    with state.store.transaction(write=True) as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
        snapshot.pop("system_prompt", None)
        snapshot.update(prompt_version="captured-fixture-prompt", messages=messages)
        db.execute("UPDATE research_contexts SET snapshot_json=? WHERE request_id=?", (json.dumps(snapshot), row["request_id"]))
    row["context_json"] = json.dumps(messages)
    fixture = ResearchFixture("plain")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    assert fixture.requests[0]["messages"][0]["content"] == messages[0]["content"]
    records = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]
    assert records[0]["prompt_version"] == "captured-fixture-prompt"
    inspected = ResearchContext(state.store).inspect(owner["user_id"], owner["organization_id"], topics[1]["id"])
    assert inspected["system_prompt"] == messages[0]["content"]


def test_unknown_second_call_does_not_hide_known_first_charge(installation):
    fixture = ResearchFixture("uncertain")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["accounting_state"] == "uncertain"
    assert result["actual_microusd"] is None
    assert result["diagnostics"]["http_status"] == 503 and KEY not in json.dumps(result)
    records = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"], limit=100)
    assert len(records["calls"]) == 2
    assert sorted(item["accounting_state"] for item in records["calls"]) == ["settled", "uncertain"]


@pytest.mark.parametrize("mode,calls,cost,accounting", [
    ("credit_first", 1, None, "released"), ("credit_later", 2, 8, "settled"),
    ("credit_continuation", 2, 8, "settled"),
])
def test_pre_generation_credit_refusal_releases_only_rejected_call(installation, mode, calls, cost, accounting):
    fixture = ResearchFixture(mode)
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["accounting_state"] == accounting
    assert result["actual_microusd"] == cost and len(fixture.requests) == calls
    assert result["diagnostics"]["reason"] == "weight_exceeds_budget"
    assert KEY not in json.dumps(result)
    records = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"])["calls"]
    assert sorted(item["accounting_state"] for item in records) == sorted(["settled"] * (calls - 1) + ["released"])
    if mode == "credit_continuation":
        assert result["message_id"] and "Segment 1." in result["output_text"]


@pytest.mark.parametrize(
    "mode,code,calls", [("empty", "agent_empty_response", 1), ("invalid_arguments", "agent_invalid_tool_arguments", 2)]
)
def test_native_failures_keep_known_usage_and_safe_specific_reason(installation, mode, code, calls):
    state, owner, _, run = turn(installation, ResearchFixture(mode))
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["error_code"] == code
    # Malformed tool arguments are asked for again once before the run fails.
    assert result["accounting_state"] == "settled" and result["actual_microusd"] == 8 * calls
    assert KEY not in json.dumps(result)
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM research_tool_receipts WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
        events = [json.loads(row[0]) for row in db.execute("SELECT event_json FROM llm_events WHERE run_id=?", (run["id"],))]
        assert any(event.get("phase") == "failed" and event["reason"] == code for event in events)


def test_malformed_tool_arguments_are_repaired_once(installation):
    fixture = ResearchFixture("invalid_arguments_once")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded" and result["message_id"] and len(fixture.requests) == 2
    assert "not a valid JSON object" in fixture.requests[1]["messages"][-1]["content"]
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM research_tool_receipts WHERE run_id=?", (run["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("mode,status,calls", [("long", "succeeded", 6)])
def test_long_answer_continues_in_bounded_priced_calls_and_preserves_partial(installation, mode, status, calls):
    fixture = ResearchFixture(mode)
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == status and result["accounting_state"] == "settled"
    assert result["actual_microusd"] == 8 * calls and result["completion_tokens"] == 10 * calls
    assert result["output_text"] == "".join(f"Segment {index}. " for index in range(1, calls + 1))
    assert result["message_id"] and len(fixture.requests) == calls
    assert all(request["max_completion_tokens"] <= 256 for request in fixture.requests)
    assert all("tools" not in request for request in fixture.requests[1:])
    if status == "failed":
        assert result["error_code"] == "output_token_limit"


def test_default_output_has_no_wire_token_cap_and_records_exact_call_events(installation):
    fixture = ResearchFixture("long")
    state, owner, _, run = turn(installation, fixture, maximum=None)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "succeeded" and len(fixture.requests) == 6
    assert all("max_completion_tokens" not in request and "max_tokens" not in request for request in fixture.requests)
    with state.store.transaction() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT event_json FROM llm_events WHERE run_id=?", (run["id"],))]
    requests = [event["request"] for event in events if event.get("phase") == "provider_request"]
    responses = [event["response"] for event in events if event.get("phase") == "provider_response"]
    assert requests == fixture.requests and len(responses) == 6
    assert KEY not in json.dumps(events)


@pytest.mark.parametrize("change_profile", [False, True])
def test_compaction_preserves_sources_prices_and_captured_instructions(installation, change_profile):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    settings = f"/api/organizations/{org}/topics/{topic}/research-settings"
    from reasoning_ledger.server.research_tools import DEFAULT_TOOLS
    if change_profile:
        client.put(settings, json={"tools": DEFAULT_TOOLS, "prompt_profile": "blueprint"}).raise_for_status()
    for number in range(70):
        state.store.post_message(
            owner["user_id"],
            org,
            topic,
            f"Research source {number}: V is a vector space over Q. " + "Keep the missing dimension hypothesis. " * 40,
            "source-" + str(number),
        )
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(owner["user_id"], org, topic, "compaction-test", state.parameters([MODELS[0]], 512, 50, None), [quote])
    fixture, run = ResearchFixture("memory"), state.claim()
    captured_prompt = json.loads(run["context_json"])[0]["content"]
    if change_profile:
        client.put(settings, json={"tools": DEFAULT_TOOLS, "prompt_profile": "lean"}).raise_for_status()
    ResearchAgent(state, fixture.provider, run, quote).execute()
    result = state.run(owner["user_id"], org, run["id"])
    assert result["status"] == "succeeded", result
    view = ResearchContext(state.store).inspect(owner["user_id"], org, topic)
    assert view["system_prompt"] == captured_prompt
    assert fixture.requests[-1]["messages"][0]["content"] == captured_prompt
    if change_profile:
        assert view["snapshot"]["prompt_profile"] == "blueprint"
        assert client.get(settings).json()["prompt_profile"] == "lean"
    assert view["working_memory"]["body"]["definitions"][0]["sources"]
    assert "dimension is not established" in json.dumps(view["working_memory"])
    with state.store.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM messages WHERE organization_id=? AND topic_id=? AND sender_kind='human'", (org, topic)
            ).fetchone()[0]
            == 71
        )
        assert db.execute("SELECT count(*) FROM inference_calls WHERE run_id=? AND purpose='compaction'", (run["id"],)).fetchone()[0] >= 1
        assert db.execute("SELECT count(*) FROM research_memories WHERE organization_id=? AND topic_id=?", (org, topic)).fetchone()[0] >= 1
    assert result["actual_microusd"] == len(fixture.requests) * 8
    assert all(request["max_completion_tokens"] <= 512 for request in fixture.requests)
    assert fixture.requests[-1]['max_completion_tokens'] == 512  # compaction preserves the answer's per-call limit


def long_topic(installation):
    _, state, _, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    for number in range(70):
        state.store.post_message(owner["user_id"], org, topic, "A precise original hypothesis. " * 60, "memory-source-" + str(number))
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    return state, owner, org, topic, quote


def test_bad_compaction_answers_from_recent_messages_and_is_not_retried_every_turn(installation):
    state, owner, org, topic, quote = long_topic(installation)
    state.enqueue(owner["user_id"], org, topic, "bad-memory", state.parameters([MODELS[0]], 256, 50, None), [quote])
    run = state.claim()
    fixture = ResearchFixture("bad_compaction")
    ResearchAgent(state, fixture.provider, run, quote).execute()
    result = state.run(owner["user_id"], org, run["id"])
    assert result["status"] == "succeeded", "a rejected summary does not fail the turn"
    compactions = [body for body in fixture.requests if "Compact mathematical research" in body["messages"][0]["content"]]
    assert len(compactions) == 1
    answer = fixture.requests[-1]["messages"]
    assert any(message["role"] == "system" and "could not be summarized" in message["content"] for message in answer)
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM research_memories WHERE topic_id=?", (topic,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM messages WHERE topic_id=? AND sender_kind='human'", (topic,)).fetchone()[0] == 71
        reply = db.execute("SELECT content FROM messages WHERE inference_run_id=?", (run["id"],)).fetchone()[0]
    assert reply.endswith("drew only on recent messages and history lookups.*")
    assert [event["reason"] for event in compaction_events(state, run["id"])] == ["invalid_summary"]

    state.store.post_message(owner["user_id"], org, topic, "Is this thing on?", "after-bad-memory")
    state.enqueue(owner["user_id"], org, topic, "after-bad-memory-run", state.parameters([MODELS[0]], 256, 50, None), [quote])
    second = state.claim()
    retry = ResearchFixture("bad_compaction")
    ResearchAgent(state, retry.provider, second, quote).execute()
    assert state.run(owner["user_id"], org, second["id"])["status"] == "succeeded"
    assert not any("Compact mathematical research" in body["messages"][0]["content"] for body in retry.requests), \
        "the same failed batch is not summarized again within the retry window"
    assert any(event["phase"] == "compaction_skipped" for event in state.run(owner["user_id"], org, second["id"])["activity"])


def compaction_events(state, run_id):
    with state.store.transaction() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT event_json FROM llm_events WHERE run_id=?", (run_id,))]
    return [event for event in events if event.get("phase") == "compaction_failed"]


@pytest.mark.parametrize("efforts,reasoning", [(("none", "low", "high", "max"), {"effort": "none"}), ((), None)])
def test_compaction_asks_for_the_lowest_advertised_effort_with_room_to_answer(installation, efforts, reasoning):
    from reasoning_ledger.server.research_agent import COMPACTION_MAX_TOKENS

    state, owner, org, topic, _ = long_topic(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 65536, supports_tools=True, reasoning_efforts=efforts)
    state.enqueue(owner["user_id"], org, topic, "reasoning-memory", state.parameters([MODELS[0]], None, 50, None), [quote])
    fixture, run = ResearchFixture("memory"), state.claim()
    ResearchAgent(state, fixture.provider, run, quote).execute()
    assert state.run(owner["user_id"], org, run["id"])["status"] == "succeeded"
    compactions = [body for body in fixture.requests if "Compact mathematical research" in body["messages"][0]["content"]]
    assert compactions and all(body["max_completion_tokens"] == COMPACTION_MAX_TOKENS for body in compactions)
    assert all(body.get("reasoning") == reasoning for body in compactions)
    assert all("reasoning" not in body for body in fixture.requests if body not in compactions), "the answer keeps its own effort"


def test_length_truncated_compaction_is_reported_as_an_output_limit(installation):
    state, owner, org, topic, quote = long_topic(installation)
    state.enqueue(owner["user_id"], org, topic, "truncated-memory", state.parameters([MODELS[0]], 256, 50, None), [quote])
    run = state.claim()
    ResearchAgent(state, ResearchFixture("truncated_compaction").provider, run, quote).execute()
    assert state.run(owner["user_id"], org, run["id"])["status"] == "succeeded", "a cut-off summary does not fail the turn"
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM research_memories WHERE topic_id=?", (topic,)).fetchone()[0] == 0, \
            "a summary cut off by its output limit is not saved, even when what arrived parses"
    [failed] = compaction_events(state, run["id"])
    assert failed["reason"] == "output_token_limit" and "hit its output limit" in failed["summary"]


def test_near_valid_compaction_keeps_only_cited_items(installation):
    state, owner, org, topic, quote = long_topic(installation)
    state.enqueue(owner["user_id"], org, topic, "fenced-memory", state.parameters([MODELS[0]], 256, 50, None), [quote])
    run = state.claim()
    ResearchAgent(state, ResearchFixture("fenced_compaction").provider, run, quote).execute()
    assert state.run(owner["user_id"], org, run["id"])["status"] == "succeeded"
    with state.store.transaction() as db:
        memories = [json.loads(row[0]) for row in db.execute("SELECT body_json FROM research_memories WHERE topic_id=?", (topic,))]
        reply = db.execute("SELECT content FROM messages WHERE inference_run_id=?", (run["id"],)).fetchone()[0]
    assert memories and all(memory["claims"] == [] and memory["next_steps"] == [] for memory in memories)
    assert all(memory["definitions"][0]["text"].startswith("V is a vector space") for memory in memories)
    assert "couldn't be summarized" not in reply


def test_goal_dispatch_and_explicit_completion_use_the_same_agent_loop(installation):
    _, state, _, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    goals = ResearchGoals(state)
    goals.create(
        owner["user_id"],
        org,
        topic,
        client_id="goal-request-id-123456",
        objective="Explain the supplied result",
        acceptance=["Give a cited argument with missing hypotheses explicit"],
        model=MODELS[0],
        budget_microusd=1_000_000,
        max_turns=3,
        max_tool_calls=5,
        duration_seconds=3600,
    )
    fixture = ResearchFixture("goal")
    asyncio.run(goals.dispatch(fixture.provider))
    run = state.claim()
    assert run is not None
    ResearchAgent(state, fixture.provider, run, Quote(**json.loads(run["quote_json"]))).execute()
    goal = goals.current(owner["user_id"], org, topic)
    assert goal["status"] == "complete" and goal["turns"] == 1
    assert state.run(owner["user_id"], org, run["id"])["status"] == "succeeded"
    asyncio.run(goals.dispatch(fixture.provider))
    assert state.claim() is None


def test_goal_api_and_topic_tool_settings_are_authenticated_and_replayable(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    path = f"/api/organizations/{org}/topics/{topic}"
    assert client.get(path + "/goal").status_code == 200
    assert client.get(path + "/research-context").json()["prompt_version"]
    settings = client.put(path + "/research-settings", json={"tools": ["history", "knowledge"]})
    assert settings.status_code == 200
    assert client.get(path + "/research-settings").json()["tools"] == ["history", "knowledge"]
    body = {
        "client_id": "api-goal-request-123456",
        "objective": "Prove the supplied lemma",
        "acceptance": ["Identify every missing hypothesis"],
        "model": MODELS[0],
        "budget_microusd": 500000,
        "max_turns": 5,
        "max_tool_calls": 10,
        "duration_seconds": 3600,
    }
    first = client.post(path + "/goal", json=body)
    assert first.status_code == 202, first.text
    repeated = client.post(path + "/goal", json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == first.json()["id"]
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE topic_id=? AND content LIKE '/goal%'", (topic,)).fetchone()[0] == 1
    assert client.patch(path + "/goal/" + first.json()["id"], json={"status": "paused"}).status_code == 200
    assert client.get(path + "/goal").json()["status"] == "paused"


def test_tool_capable_provider_rejects_invalid_unicode_without_duplicate_charge(installation):
    fixture = ResearchFixture("invalid_unicode")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["error_code"] == "provider_protocol"
    assert result["actual_microusd"] == 8 and len(fixture.requests) == 1
    records = UsageStore(state.store).calls(owner["user_id"], owner["organization_id"], limit=100)["calls"]
    assert len(records) == 1 and records[0]["accounting_state"] == "settled" and records[0]["status"] == "failed"


def test_changed_queued_tool_grant_fails_run_before_provider_submission(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(owner["user_id"], org, topic, "queued-grant-fixture", state.parameters([MODELS[0]], 256, 50, None), [quote])
    assert client.put(f"/api/organizations/{org}/topics/{topic}/research-settings", json={"tools": []}).status_code == 200
    run, fixture = state.claim(), ResearchFixture()
    ResearchAgent(state, fixture.provider, run, quote).execute()
    result = state.run(owner["user_id"], org, run["id"])
    assert result["status"] == "failed" and result["accounting_state"] == "released"
    assert fixture.requests == []


def test_plain_beta_with_no_tool_groups_does_not_advertise_goal_controls(installation):
    from reasoning_ledger.server.research_tools import ResearchTools

    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    assert client.put(f"/api/organizations/{org}/topics/{topic}/research-settings", json={"tools": []}).status_code == 200
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(owner["user_id"], org, topic, "plain-beta-grants", state.parameters([MODELS[0]], 256, 50, None), [quote])
    assert ResearchTools(state, state.claim()).registry.list() == []


def test_compaction_preserves_a_small_per_call_output_limit(installation):
    _, state, _, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    for number in range(70):
        state.store.post_message(owner["user_id"], org, topic, "A precise original hypothesis. " * 60, "bounded-memory-" + str(number))
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(owner["user_id"], org, topic, "bounded-memory", state.parameters([MODELS[0]], 32, 50, None), [quote])
    fixture, run = ResearchFixture("memory"), state.claim()
    ResearchAgent(state, fixture.provider, run, quote).execute()
    result = state.run(owner["user_id"], org, run["id"])
    assert result["status"] == "succeeded" and result["actual_microusd"] == len(fixture.requests) * 8
    assert len(fixture.requests) >= 2
    assert all(request["max_completion_tokens"] <= 32 for request in fixture.requests)
    with state.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE topic_id=? AND sender_kind='human'", (topic,)).fetchone()[0] == 71


def test_accepted_beta_without_research_snapshot_is_not_mislabeled_as_workflow(installation):
    from test_public_inference import Fixture

    _, state, _, owner, topics = prepare(installation)
    quote = Quote(MODELS[0], "0.000001", "0.000002", "0", 200000, 8192)
    state.enqueue(
        owner["user_id"],
        owner["organization_id"],
        topics[1]["id"],
        "legacy-beta-queue",
        state.parameters([MODELS[0]], 256, 50, None),
        [quote],
    )
    row = state.claim()
    with state.store.transaction(write=True) as db:
        db.execute("DELETE FROM research_contexts WHERE request_id=?", (row["request_id"],))
    asyncio.run(InferenceWorker(state, Fixture().provider)._execute(row))
    with state.store.transaction() as db:
        call = db.execute("SELECT purpose FROM inference_calls WHERE run_id=?", (row["id"],)).fetchone()
        assert call[0] == "answer"
