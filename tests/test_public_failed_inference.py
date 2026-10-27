"""Failed streams retain received work and inspectable accounting without paid calls."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_public_provider_stream import KEY, MODEL, QUOTE, content, enqueue, event, fixture_provider, usage
from test_public_provider_stream import state as state
from test_public_server import installation as installation

from reasoning_ledger.server.inference_worker import InferenceWorker
from reasoning_ledger.server.openrouter import ProviderError, Usage, rejection_error
from reasoning_ledger.server.research_provider import ResearchCompletion
from reasoning_ledger.server.usage_store import UsageStore


@pytest.mark.parametrize("failure", ["stream_error", "disconnect", "incomplete", "cancelled"])
def test_research_failure_flushes_received_tail_and_retains_partial_exchange(state, monkeypatch, failure):
    ledger, owner, _ = state
    enqueue(state, "received-tail")
    row = ledger.claim()
    body = event(content("Opening. ")) + event(content("The received result."))
    body += event({"id": "gen-fixture", "choices": [{"delta": {"reasoning": "Retained private reasoning."}}]})
    if failure == "stream_error":
        body += event({"error": {"code": 502, "message": KEY}})

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
            if failure == "disconnect":
                raise httpx.ReadError(KEY)
            if failure == "cancelled":
                raise asyncio.CancelledError()

    provider, _, _ = fixture_provider(body)
    if failure in {"disconnect", "cancelled"}:
        provider.transport = httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenBody()))
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = True
    # Freeze only this module's clock: the second delta falls inside the save interval.
    monkeypatch.setattr("reasoning_ledger.server.research_provider.time", SimpleNamespace(monotonic=lambda: 10.0))
    with pytest.raises((ProviderError, httpx.ReadError, asyncio.CancelledError)):
        asyncio.run(completion._read_agent_stream(KEY, {"model": MODEL}, 30))
    result = ledger.run(owner["user_id"], owner["organization_id"], row["id"])
    assert result["output_text"] == "Opening. The received result."
    with ledger.store.transaction() as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT event_json FROM llm_events WHERE run_id=?", (row["id"],))]
    partial = next(item for item in events if item.get("phase") == "provider_partial_response")
    assert partial["response"]["choices"][0]["message"] == {
        "role": "assistant", "content": result["output_text"], "reasoning": "Retained private reasoning.",
    }
    assert KEY not in json.dumps(partial)
    assert "Retained private reasoning." not in result["output_text"]


@pytest.mark.parametrize("research", [False, True])
def test_error_frame_keeps_its_generation_and_usage_without_becoming_success(state, research):
    ledger, owner, _ = state
    run = enqueue(state, "error-frame-usage")
    row = ledger.claim()
    frame = {**usage(), "error": {"code": 502, "message": KEY}}
    frame["usage"]["cost"] = 0.0000079  # Numeric JSON accounting also passes through Decimal parsing.
    provider, _, requests = fixture_provider(event(frame))
    if research:
        completion = ResearchCompletion(ledger, provider, row, QUOTE)
        with pytest.raises(ProviderError):
            asyncio.run(completion.submit({"model": MODEL, "messages": [{"role": "user", "content": "Prompt"}]}, timeout=30))
        ledger.finish(row, "failed", usage=completion.aggregate(), error=completion.failure_code)
    else:
        # Exercise the non-research worker used by retained legacy beta requests.
        with ledger.store.transaction(write=True) as db:
            db.execute("DELETE FROM research_contexts WHERE request_id=?", (row["request_id"],))
        asyncio.run(InferenceWorker(ledger, provider)._execute(row))
    result = ledger.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["error_code"] == "provider_stream_error"
    assert result["accounting_state"] == "settled" and result["actual_microusd"] == 8
    calls = UsageStore(ledger.store).calls(owner["user_id"], owner["organization_id"])["calls"]
    assert len(calls) == len(requests) == 1
    assert calls[0]["provider_generation_id"] == "gen-fixture"
    assert calls[0]["diagnostics"]["http_status"] == 502
    assert calls[0]["diagnostics"]["phase"] == "stream"


def test_scoped_diagnostics_show_paid_calls_before_credit_refusal_without_private_content(state):
    from reasoning_ledger.server.bot_diagnostics import details

    ledger, owner, topic = state
    ledger.store.set_policy(owner["user_id"], owner["organization_id"], [MODEL], 100_000_000)
    enqueue(state, "paid-then-rejected")
    row = ledger.claim()
    calls = UsageStore(ledger.store)
    paid = calls.begin_call(row, QUOTE, purpose="answer", context=[{"role": "user", "content": KEY}])
    calls.finish_call(paid, status="succeeded", usage=Usage(17_000_000, 50000, 1000, reasoning_tokens=950),
                      provider_generation_id="gen-paid")
    refused = calls.begin_call(row, QUOTE, purpose="continuation", parent_call_id=paid)
    calls.finish_call(refused, status="failed", no_charge=True, error_code="provider_rejected",
                      diagnostics=rejection_error(402, {"limit_source": "openrouter_key_limit", "raw": KEY}).diagnostics)
    ledger.progress(row, "An opening sentence.", "gen-paid")
    ledger.finish(row, "failed", usage=Usage(17_000_000, 50000, 1000), error="provider_rejected")
    # Another run in the same topic must not appear in a run-targeted result.
    other = enqueue(state, "unrelated-work")
    with ledger.store.transaction() as db:
        value = details(db, owner["organization_id"], topic, run_id=row["id"])
        message = db.execute("SELECT content FROM messages WHERE inference_run_id=?", (row["id"],)).fetchone()[0]
    result = value["records"]["inference"]
    assert len(result) == 1 and result[0]["id"] == row["id"]
    assert result[0]["actual_microusd"] == 17_000_000
    assert result[0]["provider_call_count"] == 2 and not result[0]["provider_calls_truncated"]
    receipts = result[0]["provider_calls"]
    assert [(call["id"], call["accounting_state"]) for call in receipts] == [(paid, "settled"), (refused, "released")]
    assert receipts[0]["reasoning_tokens"] == 950 and receipts[0]["provider_generation_id"] == "gen-paid"
    assert receipts[1]["diagnostics"]["category"] == "credit"
    assert receipts[1]["diagnostics"]["limit_source"] == "openrouter_key_limit"
    assert receipts[1]["diagnostics"]["phase"] == "http"
    assert "spending limit" in message and KEY not in message
    assert KEY not in json.dumps(value) and other["id"] not in json.dumps(value)


def test_diagnostic_call_window_is_bounded_and_marks_omitted_history(state):
    from reasoning_ledger.server.bot_diagnostics import PROVIDER_CALL_LIMIT, details

    ledger, owner, topic = state
    enqueue(state, "many-calls")
    row = ledger.claim()
    calls, identities = UsageStore(ledger.store), []
    for index in range(PROVIDER_CALL_LIMIT + 2):
        call = calls.begin_call(row, QUOTE, purpose="answer" if not index else "continuation")
        calls.finish_call(call, status="succeeded", usage=Usage(8, 20, 10), provider_generation_id=f"gen-{index}")
        identities.append(call)
    with ledger.store.transaction() as db:
        run = details(db, owner["organization_id"], topic)["records"]["inference"][0]
    assert run["provider_call_count"] == PROVIDER_CALL_LIMIT + 2 and run["provider_calls_truncated"]
    assert [call["id"] for call in run["provider_calls"]] == identities[-PROVIDER_CALL_LIMIT:]


def test_failed_research_answer_and_full_trace_keep_received_output(installation, monkeypatch):
    from test_public_research_agent import ResearchFixture, turn

    from reasoning_ledger.server.research_export import export_topic
    from reasoning_ledger.server.topic_diagnostics import summarize_topic

    fixture = ResearchFixture()
    original = fixture.handle
    body = event(content("Opening. ")) + event(content("Received mathematical result."))
    body += event({"id": "gen-fixture", "choices": [{"delta": {"reasoning": "Private retained reasoning."}}]})
    body += event({"error": {"code": 502, "message": KEY}})

    def respond(request):
        if request.method == "GET":
            return original(request)
        fixture.requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    fixture.provider.transport = httpx.MockTransport(respond)
    # Native timing stays live; only the stream flush interval is held fixed.
    import time

    monkeypatch.setattr("reasoning_ledger.server.research_provider.time",
                        SimpleNamespace(monotonic=lambda: 10.0, time=time.time))
    ledger, owner, topics, row = turn(installation, fixture)
    result = ledger.run(owner["user_id"], owner["organization_id"], row["id"])
    assert result["status"] == "failed" and result["accounting_state"] == "uncertain"
    assert result["output_text"] == "Opening. Received mathematical result."
    assert result["message_id"] and len(fixture.requests) == 1
    exported = export_topic(ledger, owner["user_id"], owner["organization_id"], topics[1]["id"])
    raw = json.dumps(exported)
    assert "Private retained reasoning." in raw and KEY not in raw
    summary = summarize_topic(exported)
    exchange = summary["runs"][0]["provider_exchanges"][0]["response"]
    assert exchange["partial"] is True
    assert exchange["choices"][0]["reasoning_characters"] == len("Private retained reasoning.")
    assert "Private retained reasoning." not in json.dumps(summary)


def test_failed_stream_retention_obeys_attempt_fence(state):
    ledger, _, _ = state
    enqueue(state, "fenced-partial")
    row = ledger.claim()

    class FencedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(content("Already saved. "))
            with ledger.store.transaction(write=True) as db:
                db.execute("UPDATE llm_runs SET attempt_id='replacement' WHERE id=?", (row["id"],))
            yield event(content("Must not be saved by the old attempt."))
            raise httpx.ReadError(KEY)

    provider, _, _ = fixture_provider(b"")
    provider.transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=FencedBody()))
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = True
    with pytest.raises((ProviderError, httpx.ReadError)):
        asyncio.run(completion._read_agent_stream(KEY, {"model": MODEL}, 30))
    with ledger.store.transaction() as db:
        output = db.execute("SELECT output_text FROM llm_runs WHERE id=?", (row["id"],)).fetchone()[0]
        assert "Must not" not in output
        assert not db.execute("SELECT 1 FROM llm_events WHERE run_id=? AND event_json LIKE '%provider_partial_response%'",
                              (row["id"],)).fetchone()


def test_retention_failure_does_not_replace_the_provider_failure(state, monkeypatch, caplog):
    ledger, _, _ = state
    enqueue(state, "retention-error")
    row = ledger.claim()
    provider, _, _ = fixture_provider(event({"error": {"code": 429, "message": KEY}}))
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = True

    async def unavailable(*args):
        raise ValueError(KEY)

    monkeypatch.setattr(completion, "_save_failed_stream", unavailable)
    with pytest.raises(ProviderError) as failure:
        asyncio.run(completion._read_agent_stream(KEY, {"model": MODEL}, 30))
    assert failure.value.code == "provider_stream_error" and failure.value.diagnostics["http_status"] == 429
    assert "could not be retained" in caplog.text and KEY not in caplog.text
