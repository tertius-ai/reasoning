"""OpenRouter reasoning metadata through the real streaming parser and executor.

The fixture file holds sanitized documented and provider-variant shapes. The
provider transport is a wire fixture, not paid inference.
"""

import asyncio
import json
from pathlib import Path

import pytest
from test_public_provider_stream import KEY, MODEL, QUOTE, enqueue, event, fixture_provider, usage
from test_public_provider_stream import state as state
from test_public_research_agent import ResearchFixture, turn
from test_public_server import installation as installation

from reasoning_ledger.server.openrouter import PROTOCOL_FIELDS, ProviderError, protocol_diagnostics, safe_diagnostics
from reasoning_ledger.server.research_provider import ResearchCompletion

CASES = json.loads((Path(__file__).parent / "fixtures" / "openrouter_reasoning_details.json").read_text(encoding="utf-8"))


def read_stream(state, deltas):
    ledger, _, _ = state
    enqueue(state, "reasoning-shape")
    row = ledger.claim()
    frames = [{"id": "gen-fixture", "choices": [{"index": 0, "delta": delta}]} for delta in deltas]
    frames.append({"id": "gen-fixture", "choices": [{"index": 0, "delta": {"content": "Answer"}}]})
    body = b"".join(event(frame) for frame in frames) + event(usage()) + b"data: [DONE]\n\n"
    provider, responses, _ = fixture_provider(body, split=17)
    completion = ResearchCompletion(ledger, provider, row, QUOTE)
    completion.stream_visible = False
    try:
        return asyncio.run(completion._read_agent_stream(KEY, {"model": MODEL}, 30))
    finally:
        assert responses[0].closed


@pytest.mark.parametrize("name", sorted(CASES["accepted"]))
def test_documented_and_variant_reasoning_shapes_are_accepted_and_preserved(state, name):
    case = CASES["accepted"][name]
    message = read_stream(state, case["deltas"])["choices"][0]["message"]
    assert message["content"] == "Answer"
    assert message.get("reasoning_details", []) == case["expected"]
    assert message.get("reasoning") == case.get("reasoning")


@pytest.mark.parametrize("name", sorted(CASES["rejected"]))
def test_malformed_reasoning_names_the_field_without_echoing_provider_values(state, name):
    case = CASES["rejected"][name]
    with pytest.raises(ProviderError) as error:
        read_stream(state, [case["delta"]])
    assert error.value.code == "provider_protocol" and not error.value.no_charge
    diagnostics = safe_diagnostics(error.value.diagnostics)
    assert diagnostics["protocol_stage"] == "reasoning"
    assert diagnostics["protocol_field"] == case["field"]
    assert diagnostics["protocol_value_type"] == case["value_type"]
    assert case["field"] in diagnostics["message"] and PROTOCOL_FIELDS[case["field"]] in diagnostics["message"]
    assert "SENTINEL" not in json.dumps(diagnostics)


def test_protocol_field_diagnostics_are_allowlisted():
    assert safe_diagnostics(protocol_diagnostics("reasoning", "response_structure", 10, KEY, KEY)) == {
        "category": "protocol",
        "protocol_stage": "reasoning",
        "protocol_rule": "response_structure",
        "received_bytes": 10,
        "message": "The response could not be parsed: reasoning metadata.",
        "action": protocol_diagnostics("reasoning")["action"],
    }
    stored = {"category": "protocol", "protocol_stage": "reasoning", "protocol_field": "reasoning_details[].text",
              "protocol_value_type": KEY, "message": KEY}
    value = safe_diagnostics(stored)
    assert value["protocol_field"] == "reasoning_details[].text" and "protocol_value_type" not in value
    assert KEY not in json.dumps(value)
    assert "No automatic retry" in value["action"] and "charge may have occurred" in value["action"]


def test_malformed_reasoning_fails_once_with_uncertain_charge_and_no_replay(installation):
    fixture = ResearchFixture("gemini_bad")
    state, owner, _, run = turn(installation, fixture)
    result = state.run(owner["user_id"], owner["organization_id"], run["id"])
    assert result["status"] == "failed" and result["error_code"] == "provider_protocol"
    assert result["accounting_state"] == "uncertain" and result["actual_microusd"] is None
    assert result["diagnostics"]["protocol_field"] == "reasoning_details[].signature"
    assert result["diagnostics"]["protocol_value_type"] == "object"
    assert len(fixture.requests) == 1
    assert KEY not in json.dumps(result)


def test_null_reasoning_text_with_signature_is_replayed_after_a_tool_round(installation):
    class SignatureFixture(ResearchFixture):
        def handle(self, request):
            response = super().handle(request)
            if request.method == "GET" or len(self.requests) != 1:
                return response
            wire = response.content.decode().split("\n\n")
            first = json.loads(wire[0].removeprefix("data: "))
            first["choices"][0]["delta"]["reasoning_details"] = [
                {"type": "reasoning.text", "text": "Plan the lookup.", "format": "anthropic-claude-v1", "index": 0}]
            signature = {"choices": [{"index": 0, "delta": {"reasoning_details": [
                {"type": "reasoning.text", "text": None, "signature": "c2ln", "format": "anthropic-claude-v1", "index": 0}]}}]}
            wire[0] = "data: " + json.dumps(first)
            wire.insert(1, "data: " + json.dumps(signature))
            return response.__class__(200, content="\n\n".join(wire), headers={"Content-Type": "text/event-stream"})

    fixture = SignatureFixture("tools")
    state, owner, _, row = turn(installation, fixture)
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "succeeded"
    assistant = next(message for message in fixture.requests[1]["messages"] if message.get("tool_calls"))
    assert assistant["reasoning_details"] == [
        {"type": "reasoning.text", "text": "Plan the lookup.", "format": "anthropic-claude-v1", "index": 0, "signature": "c2ln"}]
