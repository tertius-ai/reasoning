from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from reasoning_ledger.agents import PreparedTool, RunBudget
from reasoning_ledger.agents.research_memory import COMPACTION_PROMPT, MEMORY_FIELDS, memory_from_reply
from reasoning_ledger.harnesses import (
    ExecutionTargetCapabilities,
    ExecutionTargetKind,
    HarnessEventKind,
    HarnessSessionSpec,
    HarnessTurnRequest,
    NetworkAccess,
    ReasoningHarnessError,
    ReasoningNativeHarnessAdapter,
    SessionMode,
    TargetIsolation,
    WorkspaceAccess,
    validate_harness_trace,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def complete(self, payload, *, timeout):
        self.payloads.append((json.loads(json.dumps(payload)), timeout))
        return self.responses.pop(0)


class BlockingClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, payload, *, timeout):
        self.started.set()
        assert self.release.wait(timeout=5)
        return {
            "choices": [{"message": {"role": "assistant", "content": "too late"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
        }


class MemoryClient(FakeClient):
    def complete(self, payload, *, timeout):
        if payload["messages"][0]["content"] == COMPACTION_PROMPT:
            self.payloads.append((json.loads(json.dumps(payload)), timeout))
            sources = json.loads(payload["messages"][1]["content"])["source_messages"]
            memory = {field: [] for field in MEMORY_FIELDS}
            memory["definitions"] = [{"text": "The original hypotheses and question remain relevant.", "sources": [sources[0]["id"]]}]
            return {
                "choices": [{"message": {"role": "assistant", "content": json.dumps(memory)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.001},
            }
        return super().complete(payload, timeout=timeout)


def _target() -> ExecutionTargetCapabilities:
    return ExecutionTargetCapabilities(
        target_id="stateless-test",
        display_name="Stateless test target",
        kind=ExecutionTargetKind.STATELESS,
        isolation=TargetIsolation.PROCESS,
        workspace_access=(WorkspaceAccess.NONE,),
        network_access=(NetworkAccess.DENIED,),
    )


def _spec(*, mode=SessionMode.FRESH, source_session_id=None, provider="test-provider"):
    return HarnessSessionSpec(
        harness_id="reasoning-native",
        target=_target(),
        model="test/model",
        model_provider=provider,
        mode=mode,
        source_session_id=source_session_id,
    )


def _adapter(path: Path, resolver, **kwargs) -> ReasoningNativeHarnessAdapter:
    return ReasoningNativeHarnessAdapter(
        session_id="session-test",
        state_path=path,
        client_resolver=resolver,
        system_prompt="Be concise.",
        **kwargs,
    )


def test_reasoning_adapter_persists_a_conformant_checkpoint(tmp_path):
    client = FakeClient(
        [
            {
                "choices": [{"message": {"role": "assistant", "content": "Answer"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "cost": 0.002},
            }
        ]
    )
    resolved = []
    adapter = _adapter(
        tmp_path / "reasoning-session.json",
        lambda provider, model: resolved.append((provider, model)) or client,
    )
    session = adapter.create_session(_spec())
    events = []

    result = adapter.run_turn(
        session,
        HarnessTurnRequest(turn_id="turn-1", messages=({"text": "Question"},), effort="high"),
        events.append,
    )

    assert result.status == "completed"
    assert result.output_text == "Answer"
    assert result.usage == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    assert result.cost == {"amount_usd": 0.002, "currency": "USD", "basis": "provider_reported"}
    assert result.checkpoint["checkpoint_id"].startswith("chk_")
    assert resolved == [("test-provider", "test/model")]
    assert events[-2].kind is HarnessEventKind.CHECKPOINT_CREATED
    assert events[-1].kind is HarnessEventKind.TURN_COMPLETED
    assert validate_harness_trace(adapter.capabilities, tuple(events), result).valid is True

    state = json.loads((tmp_path / "reasoning-session.json").read_text())
    assert state["history"] == [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "Answer"},
    ]
    assert state["checkpoint"] == result.checkpoint
    assert state["sequence"] == events[-1].sequence
    assert state["harness"]["adapter_version"] == adapter.capabilities.adapter_version
    assert {item["name"] for item in state["turns"][0]["prompt_hashes"]} == {
        "resolved_system",
        "resolved_user",
    }


def test_reasoning_adapter_resumes_with_prior_history_and_pinned_route(tmp_path):
    path = tmp_path / "reasoning-session.json"
    first_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "First answer"}}]}])
    first = _adapter(path, lambda _provider, _model: first_client)
    first_session = first.create_session(_spec())
    first.run_turn(
        first_session,
        HarnessTurnRequest(turn_id="turn-1", messages=({"content": "First question"},)),
    )
    first.close_session(first_session)

    second_client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Second answer"}}]}])
    second = _adapter(path, lambda _provider, _model: second_client)
    second_session = second.create_session(_spec(mode=SessionMode.RESUME, source_session_id="session-test"))
    second.run_turn(
        second_session,
        HarnessTurnRequest(turn_id="turn-2", messages=({"content": "Second question"},)),
    )

    assert second_client.payloads[0][0]["messages"] == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
    ]


def test_reasoning_adapter_rejects_route_changes_and_tampered_checkpoints(tmp_path):
    path = tmp_path / "reasoning-session.json"
    client = FakeClient([{"choices": [{"message": {"role": "assistant", "content": "Answer"}}]}])
    adapter = _adapter(path, lambda _provider, _model: client)
    session = adapter.create_session(_spec())
    adapter.run_turn(
        session,
        HarnessTurnRequest(turn_id="turn-1", messages=({"content": "Question"},)),
    )
    adapter.close_session(session)

    changed_route = _adapter(path, lambda _provider, _model: client)
    with pytest.raises(ReasoningHarnessError, match="pinned model and provider"):
        changed_route.create_session(
            _spec(
                mode=SessionMode.RESUME,
                source_session_id="session-test",
                provider="different-provider",
            )
        )

    state = json.loads(path.read_text())
    state["history"][0]["content"] = "tampered"
    path.write_text(json.dumps(state))
    tampered = _adapter(path, lambda _provider, _model: client)
    with pytest.raises(ReasoningHarnessError, match="digest does not match"):
        tampered.create_session(_spec(mode=SessionMode.RESUME, source_session_id="session-test"))


def test_reasoning_adapter_redacts_tool_arguments_and_results_from_events(tmp_path):
    tool = PreparedTool(
        name="lookup",
        description="Look something up",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"query":"private"}'},
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Done"}}]},
        ]
    )
    calls = []
    adapter = _adapter(
        tmp_path / "reasoning-session.json",
        lambda _provider, _model: client,
        tool_invoker=lambda name, arguments: calls.append((name, arguments)) or {"secret": "result"},
    )
    session = adapter.create_session(_spec())
    events = []

    result = adapter.run_turn(
        session,
        HarnessTurnRequest(turn_id="turn-tool", messages=({"text": "Use the tool"},), tools=(tool,)),
        events.append,
    )

    requested = next(event for event in events if event.kind is HarnessEventKind.TOOL_REQUESTED)
    completed = next(event for event in events if event.kind is HarnessEventKind.TOOL_COMPLETED)
    assert requested.data == {"tool_call_id": "call-1", "name": "lookup"}
    assert completed.data == {
        "tool_call_id": "call-1",
        "name": "lookup",
        "ok": True,
        "has_error": False,
    }
    assert calls == [("lookup", {"query": "private"})]
    assert validate_harness_trace(adapter.capabilities, tuple(events), result).valid is True


def test_reasoning_adapter_enforces_the_turn_tool_budget_before_dispatch(tmp_path):
    tool = PreparedTool(
        name="lookup",
        description="Look something up",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-denied",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        ]
    )
    calls = []
    adapter = _adapter(
        tmp_path / "reasoning-session.json",
        lambda _provider, _model: client,
        tool_invoker=lambda name, arguments: calls.append((name, arguments)),
    )
    session = adapter.create_session(_spec())

    result = adapter.run_turn(
        session,
        HarnessTurnRequest(
            turn_id="turn-budget",
            messages=({"text": "Try the tool"},),
            tools=(tool,),
            budget=RunBudget(max_tool_calls=0),
        ),
    )

    assert result.status == "failed"
    assert "tool-budget final turn" in result.error
    assert result.metadata["adapter_session_ref"] == "reasoning:session-test"
    assert calls == []


def test_reasoning_adapter_compacts_history_with_an_auditable_digest(tmp_path):
    client = MemoryClient(
        [
            {"choices": [{"message": {"role": "assistant", "content": "long answer"}}]},
            {"choices": [{"message": {"role": "assistant", "content": "next answer"}}]},
        ]
    )
    path = tmp_path / "reasoning-session.json"
    adapter = _adapter(
        path,
        lambda _provider, _model: client,
        max_context_characters=10,
        retained_messages=1,
    )
    session = adapter.create_session(_spec())
    first_events = []
    adapter.run_turn(
        session,
        HarnessTurnRequest(turn_id="turn-1", messages=({"text": "long question"},)),
        first_events.append,
    )

    compacted = next(event for event in first_events if event.kind is HarnessEventKind.PROGRESS)
    assert compacted.data["category"] == "context_compaction"
    state = json.loads(path.read_text())
    assert state["history"][0]["content"].startswith("Cited mathematical working memory")
    assert state["compactions"][0]["digest"] == compacted.data["digest"]
    assert state["compactions"][0]["status"] == "completed"
    source_id = state["working_memory"]["definitions"][0]["sources"][0]
    assert adapter.read_source(session, source_id)["content"] == "long question"

    adapter.run_turn(
        session,
        HarnessTurnRequest(turn_id="turn-2", messages=({"text": "next question"},)),
    )
    assert client.payloads[2][0]["messages"][1]["content"].startswith("Cited mathematical working memory")
    assert any(tool["function"]["name"] == "research_source_read" for tool in client.payloads[2][0]["tools"])


@pytest.mark.parametrize("limit", ["output", "cost", "quote"])
def test_compaction_respects_remaining_turn_budgets_and_preserves_sources(tmp_path, limit):
    client = MemoryClient(
        [{"choices": [{"message": {"content": "long answer"}}], "usage": {"prompt_tokens": 8, "completion_tokens": 3, "cost": 0.99}}]
    )
    records = []
    adapter = _adapter(
        tmp_path / "session.json",
        lambda _provider, _model: client,
        max_context_characters=10,
        retained_messages=1,
        call_recorder=records.append,
        compaction_cost_bound=(lambda session, payload: 0.02) if limit == "cost" else None,
    )
    session = adapter.create_session(_spec())
    budget = RunBudget(max_output_tokens=3) if limit == "output" else RunBudget(max_cost_usd=1)
    result = adapter.run_turn(session, HarnessTurnRequest(turn_id="budget", messages=({"text": "long question"},), budget=budget))
    assert result.status == "completed" and len(client.payloads) == 1
    state = json.loads(adapter.state_path.read_text())
    assert state["history"] == [{"role": "user", "content": "long question"}, {"role": "assistant", "content": "long answer"}]
    assert state["compactions"][0]["status"] == "deferred"
    assert state["source_archive"][0]["content"] == "long question"
    assert not any(record.get("purpose") == "compaction" for record in records)


def test_compaction_records_cost_and_preserves_invalid_summary_sources(tmp_path):
    invalid = {field: [] for field in MEMORY_FIELDS}
    invalid["claims"] = [{"text": "A fabricated proof", "sources": ["invented"]}]
    client = FakeClient(
        [
            {"choices": [{"message": {"content": "answer"}}], "usage": {"completion_tokens": 2, "cost": 0.002}},
            {
                "choices": [{"message": {"content": json.dumps(invalid)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.001},
            },
        ]
    )
    records = []
    adapter = _adapter(
        tmp_path / "session.json",
        lambda _provider, _model: client,
        max_context_characters=10,
        retained_messages=1,
        call_recorder=records.append,
    )
    session = adapter.create_session(_spec())
    result = adapter.run_turn(session, HarnessTurnRequest(turn_id="invalid-memory", messages=({"text": "Keep all hypotheses"},)))
    assert result.cost["amount_usd"] == pytest.approx(0.003)
    assert records[-1]["purpose"] == "compaction" and records[-1]["usage"]["cost"] == 0.001
    memory = adapter.inspect_memory(session)
    assert memory["working_memory"] is None and memory["compactions"][0]["status"] == "deferred"
    assert adapter.read_source(session, memory["source_ids"][0])["content"] == "Keep all hypotheses"
    with pytest.raises(ReasoningHarnessError, match="does not belong"):
        adapter.read_source(session, "source-from-another-session")


def test_cited_memory_and_original_source_archive_survive_resume(tmp_path):
    client = MemoryClient([{"choices": [{"message": {"content": "answer"}}]}])
    path = tmp_path / "session.json"
    first = _adapter(path, lambda _provider, _model: client, max_context_characters=10, retained_messages=1)
    session = first.create_session(_spec())
    first.run_turn(session, HarnessTurnRequest(turn_id="source-turn", messages=({"text": "Assume R is commutative."},)))
    original = first.inspect_memory(session)["working_memory"]
    source = original["definitions"][0]["sources"][0]
    first.close_session(session)
    second = _adapter(path, lambda _provider, _model: client)
    resumed = second.create_session(_spec(mode=SessionMode.RESUME, source_session_id="session-test"))
    assert second.read_source(resumed, source)["content"] == "Assume R is commutative."
    assert second.inspect_memory(resumed)["working_memory"] == original


def test_deferred_compaction_is_prepared_before_next_answer_with_remaining_output_budget(tmp_path):
    client = MemoryClient(
        [
            {"choices": [{"message": {"content": "first answer"}}], "usage": {"completion_tokens": 3, "cost": 0.001}},
            {"choices": [{"message": {"content": "next answer"}}], "usage": {"completion_tokens": 3, "cost": 0.002}},
        ]
    )
    adapter = _adapter(tmp_path / "session.json", lambda _provider, _model: client, max_context_characters=10, retained_messages=1)
    session = adapter.create_session(_spec())
    first = adapter.run_turn(
        session, HarnessTurnRequest(turn_id="first", messages=({"text": "first question"},), budget=RunBudget(max_output_tokens=3))
    )
    assert first.status == "completed" and len(client.payloads) == 1
    second = adapter.run_turn(
        session, HarnessTurnRequest(turn_id="second", messages=({"text": "next question"},), budget=RunBudget(max_output_tokens=500))
    )
    assert second.status == "completed" and second.usage["output_tokens"] == 11
    assert client.payloads[1][0]["messages"][0]["content"] == COMPACTION_PROMPT
    assert client.payloads[2][0]["max_tokens"] == 496
    assert second.cost["amount_usd"] == pytest.approx(0.004)


def test_unpriced_deferred_context_refuses_next_answer_instead_of_sending_full_history(tmp_path):
    client = MemoryClient([{"choices": [{"message": {"content": "answer"}}], "usage": {"completion_tokens": 3, "cost": 0.001}}])
    adapter = _adapter(tmp_path / "session.json", lambda _provider, _model: client, max_context_characters=10, retained_messages=1)
    session = adapter.create_session(_spec())
    request = HarnessTurnRequest(turn_id="first", messages=({"text": "first question"},), budget=RunBudget(max_cost_usd=1))
    assert adapter.run_turn(session, request).status == "completed"
    next_request = HarnessTurnRequest(turn_id="second", messages=({"text": "next question"},), budget=RunBudget(max_cost_usd=1))
    rejected = adapter.run_turn(session, next_request)
    assert rejected.status == "failed" and "price bound" in rejected.error and len(client.payloads) == 1


def test_reasoning_adapter_interrupts_an_active_provider_boundary(tmp_path):
    client = BlockingClient()
    adapter = _adapter(tmp_path / "reasoning-session.json", lambda _provider, _model: client)
    session = adapter.create_session(_spec())
    events = []
    outcome = {}

    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "result",
            adapter.run_turn(
                session,
                HarnessTurnRequest(turn_id="turn-blocked", messages=({"text": "Wait"},)),
                events.append,
            ),
        )
    )
    worker.start()
    assert client.started.wait(timeout=5)
    assert adapter.interrupt(session, "turn-blocked") is True
    client.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    result = outcome["result"]
    assert result.status == "interrupted"
    assert result.usage == {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}
    assert events[-2].kind is HarnessEventKind.CHECKPOINT_CREATED
    assert events[-1].kind is HarnessEventKind.TURN_INTERRUPTED
    assert validate_harness_trace(adapter.capabilities, tuple(events), result).valid is True


def test_compaction_replies_close_to_the_contract_keep_only_cited_items():
    source, sources = "a" * 32, {"a" * 32}
    item = {"text": "V is finite-dimensional.", "sources": [source, "invented"]}
    reply = "Memory follows.\n```json\n" + json.dumps({"definitions": [item], "claims": [{"text": "x" * 4001, "sources": [source]}],
                                                       "extra": [item]}) + "\n```"
    memory = memory_from_reply(reply, sources)
    assert set(memory) == set(MEMORY_FIELDS)
    assert memory["definitions"] == [{"text": "V is finite-dimensional.", "sources": [source]}]
    assert memory["claims"] == [], "an over-long item is dropped, never shortened"
    for refused in ("no JSON here", "[1, 2]", json.dumps({"goals": [{"text": "uncited", "sources": ["invented"]}]}),
                    json.dumps({field: [] for field in MEMORY_FIELDS})):
        with pytest.raises(ValueError):
            memory_from_reply(refused, sources)
