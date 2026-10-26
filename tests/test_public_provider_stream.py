"""Untrusted provider parsing and durable beta execution through a wire fixture.

The real HTTP serializer, parser, SQLite state and executor are used. The
provider transport is a fixture, not paid inference or public API acceptance.
"""

import asyncio
import json
import time

import httpx
import pytest

from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.inference_worker import InferenceWorker
from reasoning_ledger.server.openrouter import PROTOCOL_STAGES, OpenRouter, ProviderError, Quote, safe_diagnostics
from reasoning_ledger.server.store import Store
from reasoning_ledger.server.workflow_runs import WorkflowRuns
from reasoning_ledger.server.workflow_store import WorkflowStore
from reasoning_ledger.server.workflow_worker import WorkflowWorker

KEY = "sk-or-private-fixture-key"
MODEL = "test/model"
QUOTE = Quote(MODEL, "0.000001", "0.000002", "0", 200000, 8192)
TEXT = " α = 研究 🧮 e\u0301 "


def test_protocol_diagnostics_have_closed_stage_names_and_no_untrusted_payload():
    for stage in PROTOCOL_STAGES:
        value = safe_diagnostics({"category": "protocol", "protocol_stage": stage, "message": KEY, "action": KEY, "payload": KEY})
        assert value["protocol_stage"] == stage and KEY not in json.dumps(value)
        assert "payload" not in value and "http_status" not in value
    for stage in (KEY, None, [], {}):
        assert safe_diagnostics({"category": "protocol", "protocol_stage": stage, "message": KEY}) is None


def event(body):
    return b"data: " + json.dumps(body, ensure_ascii=True).encode() + b"\r\n\r\n"


def content(text):
    return {"id": "gen-fixture", "choices": [{"index": 0, "delta": {"content": text}}]}


def usage():
    # Current Chat Completions accounting frames repeat the terminal finish
    # with a content-free delta; neither that frame nor [DONE] adds output.
    return {
        "id": "gen-fixture",
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"cost": "0.0000079", "prompt_tokens": 20, "completion_tokens": 5},
    }


class WireBody(httpx.AsyncByteStream):
    def __init__(self, body, split):
        self.body, self.split, self.closed = body, split, False

    async def __aiter__(self):
        for start in range(0, len(self.body), self.split):
            yield self.body[start : start + self.split]

    async def aclose(self):
        self.closed = True


def fixture_provider(body, split=8192, *, content_type="text/event-stream"):
    responses, requests = [], []

    def respond(request):
        requests.append(request)
        stream = WireBody(body, split)
        responses.append(stream)
        return httpx.Response(200, headers={"Content-Type": content_type}, stream=stream)

    return OpenRouter(transport=httpx.MockTransport(respond)), responses, requests


@pytest.mark.parametrize("text_size,repetitions", [(100000, 1), (10000, 250)])
def test_valid_large_events_and_reasoning_streams_do_not_hit_small_wire_limits(text_size, repetitions):
    text = "a" * text_size
    provider, responses, _ = fixture_provider(event(content(text)) * repetitions + event(usage()) + b"data: [DONE]\n\n")

    async def read():
        chunks = [chunk async for chunk in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], None)]
        assert sum(len(chunk.text) for chunk in chunks) == text_size * repetitions
        assert next(chunk.usage for chunk in chunks if chunk.usage).prompt_tokens == 20

    asyncio.run(read())
    assert responses[0].closed


def test_size_diagnostics_are_allowlisted_and_exclude_private_data():
    from reasoning_ledger.server.openrouter import protocol_diagnostics

    result = safe_diagnostics(protocol_diagnostics("stream_framing", "event_size", 123456))
    assert result["protocol_rule"] == "event_size" and result["received_bytes"] == 123456
    result = safe_diagnostics({"category": "protocol", "protocol_stage": "stream_framing", "protocol_rule": KEY,
                               "received_bytes": KEY, "message": KEY})
    assert KEY not in json.dumps(result)


def test_research_transport_accepts_large_native_reasoning_events(state):
    from reasoning_ledger.server.research_provider import ResearchCompletion

    ledger, _, _ = state
    enqueue(state, "large-native-reasoning")
    row = ledger.claim()
    reasoning = {"id": "gen-fixture", "choices": [{"index": 0, "delta": {"reasoning": "x" * 100000}}]}
    provider, responses, _ = fixture_provider(event(reasoning) * 25 + event(content("Answer")) + event(usage()) + b"data: [DONE]\n\n")
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = False
    result = asyncio.run(completion._read_agent_stream(KEY, {"model": MODEL}, 30))
    assert result["choices"][0]["message"]["content"] == "Answer"
    assert result["usage"]["prompt_tokens"] == 20 and result["id"] == "gen-fixture"
    assert responses[0].closed


def test_research_parser_accepts_more_than_twenty_four_tool_calls(state):
    from reasoning_ledger.server.research_provider import ResearchCompletion

    ledger, _, _ = state
    enqueue(state, "many-tools")
    row = ledger.claim()
    calls = [{"index": index, "id": f"call-{index}", "type": "function",
              "function": {"name": "fixture", "arguments": "{}"}} for index in range(40)]
    frame = {"id": "gen-fixture", "choices": [{"index": 0, "delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}
    provider, responses, _ = fixture_provider(event(frame) + event(usage()) + b"data: [DONE]\n\n")
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = False
    payload = {"model": MODEL, "tools": [{"type": "function", "function": {"name": "fixture", "parameters": {"type": "object"}}}]}
    response = asyncio.run(completion._read_agent_stream(KEY, payload, 30))
    assert len(response["choices"][0]["message"]["tool_calls"]) == 40 and responses[0].closed


def test_incremental_line_framing_bounds_unicode_and_handles_tiny_fragments():
    from reasoning_ledger.server.openrouter import MAX_EVENT_BYTES, StreamLines

    lines = StreamLines()
    assert list(lines.feed("data: ", 6)) == []
    for index in range(100000):
        assert list(lines.feed("x", index + 7)) == []
    assert list(lines.feed("\r\n\n", 100010)) == ["data: " + "x" * 100000, ""]
    assert lines.pending_bytes == 0
    with pytest.raises(ProviderError) as error:
        list(lines.feed("α" * (MAX_EVENT_BYTES // 2 + 1), MAX_EVENT_BYTES + 2))
    assert error.value.diagnostics["protocol_rule"] == "event_size"


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff", "\ud800\ud800", "\udfff\udfff"])
@pytest.mark.parametrize("split", [1, 7, 8192])
def test_provider_refuses_unpaired_surrogate_content_before_yield(surrogate, split):
    provider, responses, _ = fixture_provider(event(content("private-text-" + surrogate)) + b"data: [DONE]\n\n", split)

    async def read():
        yielded = []
        with pytest.raises(ProviderError) as error:
            async for chunk in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64):
                yielded.append(chunk)
        assert error.value.code == "provider_protocol" and not error.value.no_charge
        assert str(error.value) == "provider_protocol"
        assert yielded == []

    asyncio.run(read())
    assert len(responses) == 1 and responses[0].closed


# Below the 64 KiB event bound, but beyond the C/Python parser nesting limits
# on the supported Python versions. Do not change interpreter recursion limits.
DEEP_JSON = b"[" * 10000 + b"0" + b"]" * 10000


def test_deep_provider_event_is_protocol_failure_not_uncaught_recursion():
    provider, responses, _ = fixture_provider(b"data: " + DEEP_JSON + b"\n\n")

    async def read():
        with pytest.raises(ProviderError) as error:
            async for _ in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64):
                pytest.fail("malformed event must not yield a chunk")
        assert error.value.code == "provider_protocol" and not error.value.no_charge

    asyncio.run(read())
    assert responses[0].closed


def test_deep_catalog_is_a_safe_pre_submission_failure():
    provider, responses, requests = fixture_provider(b'{"data":' + DEEP_JSON + b"}", content_type="application/json")

    async def read():
        with pytest.raises(ProviderError) as error:
            await provider.quotes([MODEL])
        assert error.value.code == "catalog_unavailable" and error.value.no_charge

    asyncio.run(read())
    assert requests[0].method == "GET" and responses[0].closed
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("split", [1, 2, 7, 8192])
@pytest.mark.parametrize("escaped", [False, True])
def test_valid_unicode_and_repeated_terminal_accounting_frames_are_preserved(split, escaped):
    first = b"data: " + json.dumps(content(TEXT), ensure_ascii=escaped).encode("utf8") + b"\n\n"
    terminal = event({"id": "gen-fixture", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]})
    provider, responses, _ = fixture_provider(b": heartbeat\n\n" + first + terminal + event(usage()) + b"data: [DONE]\n\n", split)

    async def read():
        chunks = [chunk async for chunk in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64)]
        assert "".join(chunk.text for chunk in chunks) == TEXT
        assert [chunk.finish_reason for chunk in chunks if chunk.finish_reason] == ["stop", "stop"]
        known = [chunk.usage for chunk in chunks if chunk.usage]
        assert len(known) == 1 and known[0].cost_microusd == 8
        assert known[0].prompt_tokens == 20 and known[0].completion_tokens == 5

    asyncio.run(read())
    assert responses[0].closed


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "state")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "a private fixture password", "Research")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Research")
    channel = store.create_entity(user, org, "channels", "Models", project["id"])
    topic = store.create_entity(user, org, "topics", "Models", channel["id"], "beta")
    store.post_message(user, org, topic["id"], "Compute a shared result", "human-prompt")
    store.add_key(user, org, "openrouter", "Organization", KEY)
    store.set_policy(user, org, [MODEL], 1_000_000)
    return InferenceStore(store), owner, topic["id"]


def enqueue(state, client):
    ledger, owner, topic = state
    return ledger.enqueue(
        owner["user_id"], owner["organization_id"], topic, client, ledger.parameters([MODEL], 64, 50, None), [QUOTE]
    )[0]


@pytest.mark.parametrize("metadata", [
    {}, {"limit_source": "openrouter_key_limit"},
    {"limit_source": "openrouter_credits", "reason": "weight_exceeds_budget"},
    {"limit_source": "openrouter_in_flight_budget", "reason": "in_flight_budget_exhausted"},
    {"limit_source": [KEY], "reason": {"secret": KEY}},
])
def test_http_credit_refusal_is_free_and_retains_only_allowlisted_guidance(state, metadata):
    ledger, owner, _ = state
    run = enqueue(state, "credit-refusal")
    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(
        402, json={"error": {"message": KEY, "metadata": {**metadata, "raw": KEY, "remedy_hint": KEY}}},
    )))
    asyncio.run(InferenceWorker(ledger, provider)._execute(ledger.claim()))
    result = ledger.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["accounting_state"] == "released"
    assert result["diagnostics"]["http_status"] == 402 and KEY not in json.dumps(result)
    if isinstance(metadata.get("limit_source"), str):
        assert result["diagnostics"]["limit_source"] == metadata["limit_source"]
    else:
        assert "limit_source" not in result["diagnostics"]
    assert ledger.balance(owner["user_id"], owner["organization_id"])["uncertain_microusd"] == 0
    assert ledger.claim() is None


@pytest.mark.parametrize("known_usage", [False, True])
def test_mid_stream_credit_error_does_not_prove_a_free_submission(state, known_usage):
    ledger, owner, _ = state
    run = enqueue(state, "mid-stream-credit-error")
    body = event(content(TEXT)) + (event(usage()) if known_usage else b"")
    body += event({"error": {"code": 402, "message": KEY, "metadata": {"limit_source": "openrouter_key_limit", "raw": KEY}}})
    provider, _, _ = fixture_provider(body)
    asyncio.run(InferenceWorker(ledger, provider)._execute(ledger.claim()))
    result = ledger.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["accounting_state"] == ("settled" if known_usage else "uncertain")
    assert result["actual_microusd"] == (8 if known_usage else None)
    assert result["error_code"] == "provider_stream_error"
    assert result["diagnostics"]["limit_source"] == "openrouter_key_limit"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("body", [b"not JSON", b"x" * 70000, DEEP_JSON])
def test_credit_refusal_keeps_free_accounting_with_unusable_diagnostic_body(body):
    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(402, content=body)))

    async def read():
        with pytest.raises(ProviderError) as error:
            async for _ in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64):
                pytest.fail("rejected request must not return output")
        assert error.value.no_charge and error.value.diagnostics["http_status"] == 402

    asyncio.run(read())


def test_credit_refusal_keeps_free_accounting_when_optional_body_read_fails():
    class BrokenErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":'
            raise httpx.ReadError("private provider detail " + KEY)

    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(402, stream=BrokenErrorBody())))

    async def read():
        with pytest.raises(ProviderError) as error:
            async for _ in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64):
                pytest.fail("rejected request must not return output")
        assert error.value.no_charge and KEY not in json.dumps(error.value.diagnostics)

    asyncio.run(read())


@pytest.mark.parametrize(
    "bad_event", [event(content("private-text-\ud800")), b"data: " + DEEP_JSON + b"\n\n"], ids=["surrogate", "deep-json"]
)
@pytest.mark.parametrize("known_usage", [False, True])
def test_real_executor_fails_bad_provider_data_without_losing_valid_progress_or_accounting(state, bad_event, known_usage):
    ledger, owner, topic = state
    run = enqueue(state, "malformed-provider-run")
    prefix = event(content(TEXT)) + (event(usage()) if known_usage else b"")
    provider, responses, requests = fixture_provider(prefix + bad_event + b"data: [DONE]\n\n")
    worker = InferenceWorker(ledger, provider)
    asyncio.run(worker._execute(ledger.claim()))
    result = ledger.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["error_code"] == "provider_protocol"
    assert result["accounting_state"] == ("settled" if known_usage else "uncertain")
    assert result["actual_microusd"] == (8 if known_usage else None)
    assert result["output_text"] == TEXT
    messages = ledger.store.messages(owner["user_id"], owner["organization_id"], topic)
    assistants = [message for message in messages if message["author_kind"] == "assistant"]
    assert len(assistants) == 1 and assistants[0]["content"].startswith(TEXT)
    assert "Partial response: failed" in assistants[0]["content"]
    assert "private-text" not in json.dumps(result) and KEY not in json.dumps(result)
    balance = ledger.balance(owner["user_id"], owner["organization_id"])
    assert balance["spent_microusd"] == (8 if known_usage else 0)
    assert balance["reserved_microusd"] == 0
    assert balance["uncertain_microusd"] == (0 if known_usage else result["reserved_microusd"])
    assert result["reserved_microusd"] >= run["reserved_microusd"]
    assert len(requests) == 1 and responses[0].closed
    assert ledger.claim() is None


@pytest.mark.parametrize(
    "bad_body", [event(content("private-text-\ud800")), b"data: " + DEEP_JSON + b"\n\n"], ids=["surrogate", "deep-json"]
)
def test_actual_executor_thread_survives_bad_response_and_serves_following_queue(state, bad_body):
    ledger, owner, _ = state
    enqueue(state, "first-bad-wire-response")
    enqueue(state, "second-good-wire-response")
    responses, requests = [], []

    def respond(request):
        requests.append(request)
        body = bad_body if len(requests) == 1 else event(content(TEXT)) + event(usage())
        stream = WireBody(body + b"data: [DONE]\n\n", 7)
        responses.append(stream)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    worker = InferenceWorker(ledger, OpenRouter(transport=httpx.MockTransport(respond)), concurrency=1)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with ledger.store.transaction() as db:
                statuses = [row[0] for row in db.execute("SELECT status FROM llm_runs")]
            if all(status in {"failed", "succeeded"} for status in statuses) or not worker.running:
                break
            time.sleep(0.02)
        assert sorted(statuses) == ["failed", "succeeded"]
        assert worker.running
        assert len(requests) == 2 and all(response.closed for response in responses)
        with ledger.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM llm_runs WHERE error_code='provider_protocol'").fetchone()[0] == 1
        assert ledger.balance(owner["user_id"], owner["organization_id"])["spent_microusd"] == 8
    finally:
        worker.stop()
    assert not worker.running


def test_actual_workflow_thread_handles_bad_catalog_without_paid_child_or_worker_death(state):
    ledger, owner, beta = state
    user, org = owner["user_id"], owner["organization_id"]
    with ledger.store.transaction() as db:
        channel = db.execute("SELECT channel_id FROM topics WHERE id=?", (beta,)).fetchone()[0]
    topic = ledger.store.create_entity(user, org, "topics", "Workflow", channel, "gamma")["id"]
    definitions = WorkflowStore(ledger.store)
    runs = WorkflowRuns(definitions, ComputeStore(ledger.store, {}), ledger)
    saved = definitions.create(user, org, topic, "catalog-definition-fixture", {
        "format": 1,
        "name": "Catalog failure",
        "nodes": [{
            "id": "reason", "kind": "llm", "depends_on": [], "inputs": {},
            "parameters": {"model": MODEL, "prompt": "Explain a result", "max_completion_tokens": 64},
        }],
    })
    run = runs.start(user, org, saved["workflow_id"], saved["version"], "catalog-run-fixture")
    provider, responses, requests = fixture_provider(b'{"data":' + DEEP_JSON + b"}", content_type="application/json")
    worker = WorkflowWorker(runs, provider)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = runs.get(user, org, run["id"])
            if result["status"] == "failed" or not worker.running:
                break
            time.sleep(0.02)
        assert result["status"] == "failed" and worker.running
        assert result["nodes"][0]["error_code"] == "quote_unavailable"
        assert len(requests) == 1 and requests[0].method == "GET" and responses[0].closed
        with ledger.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0] == 0
        balance = ledger.balance(user, org)
        assert balance["spent_microusd"] == balance["reserved_microusd"] == balance["uncertain_microusd"] == 0
        assert KEY not in json.dumps(result)
    finally:
        worker.stop()
    assert not worker.running


def refusal(status, body):
    provider = OpenRouter(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))

    async def read():
        with pytest.raises(ProviderError) as error:
            async for _ in provider.stream(KEY, QUOTE, [{"role": "user", "content": "Prompt"}], 64):
                pytest.fail("rejected request must not return output")
        return error.value

    return asyncio.run(read())


@pytest.mark.parametrize(("message", "unmatched"), [
    ("No endpoints found matching your data policy (Free model publication). " + KEY, "data_policy"),
    ("No endpoints found that support tool use. " + KEY, "parameters"),
    ("No endpoints found that can handle the requested parameters.", "parameters"),
    ("No allowed providers are available for the selected model.", "providers"),
    ("Model recursive/flint is not a valid model ID", "model"),
    ("Something else entirely " + KEY, None),
])
def test_route_not_found_is_free_and_explained_without_provider_text(message, unmatched):
    error = refusal(404, {"error": {"code": 404, "message": message}})
    assert error.no_charge and error.diagnostics["http_status"] == 404
    assert error.diagnostics.get("unmatched") == unmatched
    assert KEY not in json.dumps(error.diagnostics) and message not in json.dumps(error.diagnostics)
    if unmatched == "data_policy":
        assert "privacy settings" in error.diagnostics["action"]


@pytest.mark.parametrize("status", [408, 429, 502, 503])
def test_refusals_that_may_follow_provider_work_stay_uncertain(status):
    assert not refusal(status, {"error": {"message": "upstream"}}).no_charge


def test_only_an_all_zero_price_route_counts_as_free():
    assert Quote(MODEL, "0", "0", "0", 200000, 8192).free
    assert Quote(MODEL, "0", "0", "0", 200000, 8192, pricing={"prompt": "0", "web_search": "0"}).free
    assert not QUOTE.free
    assert not Quote(MODEL, "0", "0", "0", 200000, 8192, pricing={"web_search": "0.004"}).free
    assert not Quote(MODEL, "0", "0", "0", 200000, 8192, pricing={"prompt": "-1"}).free, "an unreadable price is not free"

