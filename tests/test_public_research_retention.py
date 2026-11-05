"""Received file source survives failures after the provider stream completes."""

import asyncio
import json

import pytest
from test_public_provider_stream import (
    QUOTE,
    artifact_payload,
    artifact_stream,
    content,
    enqueue,
    event,
    fixture_provider,
    synthetic_latex,
    usage,
)
from test_public_provider_stream import (
    state as state,
)

from reasoning_ledger.server.openrouter import ProviderError
from reasoning_ledger.server.research_artifact_recovery import ResearchArtifactRecovery
from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.research_provider import ResearchCompletion
from reasoning_ledger.server.research_tools import ResearchTools


@pytest.mark.parametrize("failure", ["missing_usage", "invalid_final_protocol", "interrupted", "cancelled_after_stream"])
def test_file_source_is_recoverable_after_stream_and_post_stream_failures(state, failure):
    ledger, owner, topic = state
    enqueue(state, "post-stream-file-retention")
    row = ledger.claim()
    source = synthetic_latex()
    arguments = json.dumps({"name": "artifact_write", "arguments": {"path": "retained.tex", "text": source}},
                           ensure_ascii=False)
    note = "The requested source is being delivered."
    wire = event(content(note))
    if failure == "invalid_final_protocol":
        # Reasoning is retained as evidence but final UTF-8 validation refuses
        # an unpaired surrogate before any tool can run.
        wire += event({"id": "gen-fixture", "choices": [{"index": 0, "delta": {"reasoning": "\ud800"}}]})
    wire += artifact_stream(arguments, done=failure != "interrupted")
    if failure in {"invalid_final_protocol", "cancelled_after_stream"}:
        receipt = usage()
        receipt["choices"] = []
        wire = wire.replace(b"data: [DONE]", event(receipt) + b"data: [DONE]")
    provider, streams, requests = fixture_provider(wire)
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    if failure == "cancelled_after_stream":
        read_stream = completion._read_agent_stream

        async def cancel_after_complete_stream(*arguments):
            response = await read_stream(*arguments)
            with ledger.store.transaction(write=True) as db:
                db.execute("UPDATE llm_runs SET cancel_requested=1 WHERE id=?", (row["id"],))
            completion.next_cancel_check = 0
            return response

        completion._read_agent_stream = cancel_after_complete_stream
    payload = {**artifact_payload(), "messages": [{"role": "user", "content": "Save a research source file."}]}
    with pytest.raises(ProviderError) as raised:
        asyncio.run(completion.submit_with_credit_retry(payload, timeout=30))
    expected = {"missing_usage": "usage_unavailable", "invalid_final_protocol": "provider_protocol",
                "interrupted": "provider_incomplete", "cancelled_after_stream": "execution_cancelled"}[failure]
    assert raised.value.code == expected and len(requests) == 1 and streams[0].closed
    ledger.finish(row, "failed", error=expected, usage=completion.aggregate())
    with ledger.store.transaction() as db:
        events = [json.loads(record[0]) for record in db.execute("SELECT event_json FROM llm_events WHERE run_id=?", (row["id"],))]
        retained = [record["response"] for record in events if record.get("phase") == "provider_partial_response"]
        assert len(retained) == 1, "a failure retains exactly one evidence event"
        assert retained[0]["partial"] is True
        calls = retained[0]["choices"][0]["message"]["tool_calls"]
        assert json.loads(calls[0]["function"]["arguments"])["arguments"]["text"] == source
        assert db.execute("SELECT output_text FROM llm_runs WHERE id=?", (row["id"],)).fetchone()[0] == note
        assert db.execute("SELECT COUNT(*) FROM research_tool_receipts").fetchone()[0] == 0
        if failure == "invalid_final_protocol":
            assert "reasoning" in retained[0]["omitted_fields"]
            assert "reasoning" not in retained[0]["choices"][0]["message"]
    user, org = owner["user_id"], owner["organization_id"]
    assert ResearchArtifacts(ledger.store).list(user, org, topic) == []
    recovery = ResearchArtifactRecovery(ledger.store)
    candidate = recovery.list(user, org, topic)[0]
    result = recovery.recover(user, org, topic, candidate["event_sequence"], candidate["tool_index"], candidate["sha256"])
    assert ResearchArtifacts(ledger.store).content(user, org, topic, result["path"], result["revision"])[0] == source.encode()
    run = ledger.run(user, org, row["id"])
    assert run["accounting_state"] == ("settled" if failure in {"invalid_final_protocol", "cancelled_after_stream"} else "uncertain")
    assert len(requests) == 1, "explicit source recovery never replays a paid provider call"


@pytest.mark.parametrize("resumed", [False, True])
def test_retrying_an_old_assembly_does_not_replace_a_newer_reply_attachment(state, resumed):
    ledger, owner, topic = state
    enqueue(state, "first-file-turn")
    row = ledger.claim()
    tools = ResearchTools(ledger, row)
    section = tools.invoke("artifact_write", {"path": "updated-section.tex", "text": "Initial source."})["result"]
    assembly = {"path": "updated.tex", "parts": [{"path": "updated-section.tex", "revision": section["revision"]}]}
    first = tools.invoke("artifact_assemble", assembly)["result"]
    if resumed:
        ledger.finish(row, "succeeded", no_charge=True)
        enqueue(state, "resumed-file-turn")
        row = ledger.claim()
        tools = ResearchTools(ledger, row)
    updated = tools.invoke("artifact_write", {"path": "updated.tex", "text": "A newer saved source.",
                                              "expected_sha256": first["sha256"]})["result"]
    with pytest.raises(ValueError, match="artifact changed"):
        tools.invoke("artifact_assemble", assembly)
    assert first["revision"] < updated["revision"]
    ledger.finish(row, "succeeded", no_charge=True)
    messages = ledger.store.messages(owner["user_id"], owner["organization_id"], topic)
    reply = next(message for message in messages if message.get("inference_run_id") == row["id"])
    assert [(file["path"], file["revision"]) for file in reply["files"]] == [("updated.tex", updated["revision"])]
    content, _ = ResearchArtifacts(ledger.store).content(owner["user_id"], owner["organization_id"], topic,
                                                       reply["files"][0]["path"], reply["files"][0]["revision"])
    assert content == b"A newer saved source."
