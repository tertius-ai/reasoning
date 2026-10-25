import io
import json
import ssl
import urllib.error
from dataclasses import replace

import pytest

from reasoning_ledger.agents import (
    AgentRunSpec,
    CompletionArtifactRequirement,
    OpenRouterClient,
    OpenRouterError,
    PreparedAgentRun,
    PreparedSkill,
    PromptBinding,
    ReasoningNativeExecutor,
    RunBudget,
    ToolBinding,
)
from reasoning_ledger.agents.contracts import PreparedTool
from reasoning_ledger.agents.openrouter_errors import (
    openrouter_http_error_message,
    openrouter_transport_error_message,
    sanitize_provider_error_message,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def complete(self, payload, *, timeout):
        self.payloads.append((json.loads(json.dumps(payload)), timeout))
        return self.responses.pop(0)


class CancellingClient:
    def __init__(self, cancelled):
        self.cancelled = cancelled

    def complete(self, payload, *, timeout):
        self.cancelled.append(True)
        return {
            "choices": [{"message": {"role": "assistant", "content": "late answer"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }


def test_openrouter_http_errors_keep_guidance_without_routing_metadata(monkeypatch):
    body = {
        "error": {
            "message": "The requested model was retired; use provider/replacement.",
            "metadata": {
                "raw": json.dumps(
                    {
                        "error": {"message": "Use provider/replacement instead."},
                        "user_id": "user-secret",
                        "region": "internal-region",
                    }
                ),
                "provider_name": "internal-route",
            },
        },
        "user_id": "user-secret",
    }

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            404,
            "Not Found",
            {},
            io.BytesIO(json.dumps(body).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    client = OpenRouterClient(api_key="secret")
    with pytest.raises(OpenRouterError) as caught:
        client.complete({"model": "provider/retired"}, timeout=1)

    message = str(caught.value)
    assert "provider/replacement" in message
    assert "user-secret" not in message
    assert "internal-region" not in message
    assert "internal-route" not in message

    cli_message = sanitize_provider_error_message(
        'Codex provider error: {"error":{"message":"Use provider/replacement"},"user_id":"user-secret","region":"internal-region"}'
    )
    assert cli_message == "Codex provider error: Use provider/replacement"
    plain_message = openrouter_http_error_message(
        403,
        "Denied for user_id=user-secret in region=internal-region",
    )
    assert plain_message == ("OpenRouter returned HTTP 403: Denied for user_id=[redacted] in region=[redacted]")


def test_openrouter_certificate_errors_are_actionable_and_not_misreported_as_key_expiry(monkeypatch):
    certificate_error = ssl.SSLCertVerificationError(1, "certificate verify failed")

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(certificate_error)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    client = OpenRouterClient(api_key="secret")
    with pytest.raises(OpenRouterError) as caught:
        client.complete({"model": "provider/model"}, timeout=1)

    message = str(caught.value)
    assert "secure connection" in message
    assert "not an expired API key" in message
    assert "urlopen error" not in message
    assert "_ssl.c" not in message
    assert "model catalog" in openrouter_transport_error_message(certificate_error, catalog=True)


def _request(*, output_schema=None, tools=(), effort=None):
    spec = AgentRunSpec(
        executor_id="reasoning-native",
        model="openai/gpt-oss-20b",
        system_prompt=PromptBinding("system", "s1"),
        user_prompt=PromptBinding("work", "u1"),
        tools=tuple(ToolBinding(tool.name) for tool in tools),
        output_schema=output_schema,
        effort=effort,
    )
    return PreparedAgentRun(
        run_id="run_native",
        spec=spec,
        system_prompt="You are the student.",
        user_prompt="Work on lemma 3.",
        prompt_hashes={"system": "abc", "user": "def"},
        context_messages=({"role": "assistant", "content": "Prior result"},),
        tools=tools,
    )


def test_optional_tool_budget_allows_more_than_24_calls_without_infinite_wire_values():
    tool = PreparedTool("ledger_status", "Read status", {"type": "object"})
    responses = [{"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call-{i}", "type": "function", "function": {"name": tool.name, "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}]} for i in range(30)]
    responses.append({"choices": [{"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}]})
    client, calls = FakeClient(responses), []
    request = _request(tools=(tool,))
    request = replace(request, spec=replace(request.spec, budget=RunBudget(max_tool_calls=None)))
    result = ReasoningNativeExecutor(client, tool_invoker=lambda name, args: calls.append(name) or {}).execute(request, lambda event: None)
    assert result.status == "completed" and len(calls) == 30 and len(client.payloads) == 31
    assert "Infinity" not in json.dumps(client.payloads, allow_nan=False)


def test_answer_continuation_stops_before_exhausted_cumulative_budget():
    client = FakeClient([{
        "choices": [{"message": {"role": "assistant", "content": "Partial answer."}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }])
    request = _request()
    request = replace(request, spec=replace(request.spec, budget=RunBudget(max_output_tokens=2)))
    result = ReasoningNativeExecutor(client, max_output_continuations=3).execute(request, lambda event: None)
    assert result.status == "failed" and result.metadata["output_budget_exhausted"]
    assert result.output_text == "Partial answer." and result.usage["completion_tokens"] == 2
    assert len(client.payloads) == 1


@pytest.mark.parametrize("maximum", [-1, 4, True, 1.5])
def test_answer_continuation_limit_is_bounded(maximum):
    with pytest.raises(ValueError, match="output continuations"):
        ReasoningNativeExecutor(FakeClient([]), max_output_continuations=maximum)


def test_native_executor_runs_tool_loop_and_records_usage():
    tool = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
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
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": '{"claim_id":"clm_3"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "The claim holds."}}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 5},
            },
        ]
    )
    called = []
    events = []
    records = []
    executor = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: called.append((name, arguments)) or {"statement": "X"},
        call_recorder=records.append,
    )

    result = executor.execute(_request(tools=(tool,)), events.append)

    assert result.status == "completed"
    assert result.output_text == "The claim holds."
    assert result.usage == {"prompt_tokens": 25, "completion_tokens": 8}
    assert [record["sequence"] for record in records] == [1, 2]
    assert called == [("lookup_claim", {"claim_id": "clm_3"})]
    assert [event.kind.value for event in events] == [
        "run.started",
        "tool.requested",
        "tool.completed",
        "message.delta",
        "message.completed",
        "usage",
        "run.completed",
    ]
    first_payload = client.payloads[0][0]
    assert first_payload["messages"] == [
        {"role": "system", "content": "You are the student."},
        {"role": "assistant", "content": "Prior result"},
        {"role": "user", "content": "Work on lemma 3."},
    ]
    assert first_payload["tools"][0]["function"]["name"] == "lookup_claim"
    second_messages = client.payloads[1][0]["messages"]
    assert second_messages[-2:] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_claim",
                        "arguments": '{"claim_id":"clm_3"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "lookup_claim",
            "content": json.dumps({"ok": True, "result": {"statement": "X"}}),
        },
    ]


def test_native_executor_cancels_cooperatively_after_a_billable_request():
    cancelled = []
    events = []
    executor = ReasoningNativeExecutor(CancellingClient(cancelled))

    result = executor.execute(_request(), events.append, cancelled=lambda: bool(cancelled))

    assert result.status == "cancelled"
    assert result.error == "run interrupted"
    assert result.usage == {"prompt_tokens": 7, "completion_tokens": 2}
    assert result.metadata["cancelled"] is True
    assert [event.kind.value for event in events] == [
        "run.started",
        "usage",
        "run.cancelled",
    ]


def test_native_executor_reserves_cumulative_output_for_completion_contract():
    tool = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
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
                                    "id": "lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 60},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "DONE"}}],
                "usage": {"completion_tokens": 15},
            },
        ]
    )
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_output_tokens=100),
            completion_markers=("DONE",),
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda _name, _arguments: {"claim": "A"},
    ).execute(request)

    assert result.status == "completed"
    assert result.usage["completion_tokens"] == 75
    assert result.metadata["output_budget_completion_activated"] is True
    assert client.payloads[0][0]["max_tokens"] == 100
    continuation = client.payloads[1][0]
    assert continuation["max_tokens"] == 40
    assert "cumulative 100 output-token budget" in continuation["messages"][-1]["content"]
    assert "terminal markers DONE" in continuation["messages"][-1]["content"]


def test_native_executor_fails_when_provider_exceeds_cumulative_output_budget():
    client = FakeClient(
        [
            {
                "choices": [{"message": {"role": "assistant", "content": "Too long."}}],
                "usage": {"completion_tokens": 101},
            }
        ]
    )
    request = _request()
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_output_tokens=100),
        ),
    )

    result = ReasoningNativeExecutor(client).execute(request)

    assert result.status == "failed"
    assert result.error == "OpenRouter exceeded the Reasoning cumulative output-token budget of 100"
    assert result.metadata["output_budget_exhausted"] is True
    assert client.payloads[0][0]["max_tokens"] == 100


def test_native_executor_finishes_without_tools_after_budget_exhaustion():
    tool = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
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
                                    "id": "allowed",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                },
                                {
                                    "id": "denied",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "Bounded summary."}}],
                "usage": {},
            },
        ]
    )
    called = []
    events = []
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(request.spec, budget=RunBudget(max_tool_calls=1)),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: called.append(name) or {"claim": "A"},
    ).execute(request, events.append)

    assert result.status == "completed"
    assert result.output_text == "Bounded summary."
    assert result.metadata["tool_budget_exhausted"] is True
    assert result.metadata["tool_calls_denied"] == 1
    assert called == ["lookup_claim"]
    final_payload = client.payloads[1][0]
    assert "tools" not in final_payload
    assert "tool_choice" not in final_payload
    assert "tool-call budget is now exhausted" in final_payload["messages"][-1]["content"]
    denied = [event for event in events if event.kind.value == "tool.completed"][-1]
    assert denied.data["ok"] is False


def test_native_executor_repairs_future_intent_before_completing():
    tool = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Writing the report now.",
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "REPORT_COMPLETE",
                        }
                    }
                ],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(request.spec, completion_markers=("REPORT_COMPLETE",)),
    )
    called = []

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: called.append(name) or {"sha256": "a" * 64},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text == "REPORT_COMPLETE"
    assert result.metadata["completion_repair_attempted"] is True
    assert called == []
    repair = client.payloads[1][0]["messages"][-1]["content"]
    assert "Do not call another tool or add analysis" in repair
    assert "REPORT_COMPLETE" in repair
    assert "tools" not in client.payloads[1][0]
    assert "tool_choice" not in client.payloads[1][0]


def test_native_executor_repairs_each_changed_completion_state():
    path = "research/route/iteration-1/report.md"
    lookup = PreparedTool(
        name="arxiv_lookup",
        description="Resolve a source",
        input_schema={"type": "object", "properties": {}},
    )
    read = PreparedTool(
        name="arxiv_read",
        description="Read a source",
        input_schema={"type": "object", "properties": {}},
    )
    write = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I should resolve the source.",
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "arxiv_lookup",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I should read it next.",
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "read_1",
                                    "type": "function",
                                    "function": {
                                        "name": "arxiv_read",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I should write the report.",
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Evidence.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(lookup, read, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_tool_calls=5),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
            completion_tool_calls={"arxiv_lookup": 1, "arxiv_read": 1},
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read":
            if "content" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["content"], "sha256": "d" * 64}
        if name == "artifact_write":
            persisted["content"] = arguments["content"]
            return {"path": path, "sha256": "d" * 64}
        return {"ok": True}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.metadata["completion_contract_auto_completed"] is True
    assert result.metadata["completion_repair_attempted"] is True
    assert result.metadata["completion_repair_attempts"] == 3
    assert result.metadata["successful_tool_calls"] == {
        "arxiv_lookup": 1,
        "arxiv_read": 1,
        "artifact_write": 1,
    }
    repaired_payloads = [client.payloads[index][0] for index in (1, 3, 5)]
    assert [payload["tools"][0]["function"]["name"] for payload in repaired_payloads] == [
        "arxiv_lookup",
        "arxiv_read",
        "artifact_write",
    ]
    assert [payload["tool_choice"]["function"]["name"] for payload in repaired_payloads] == [
        "arxiv_lookup",
        "arxiv_read",
        "artifact_write",
    ]
    assert "later required tools will be exposed" in repaired_payloads[0]["messages"][-1]["content"]


def test_native_executor_auto_completes_when_tool_batch_satisfies_contract():
    path = "research/route/iteration-1/report.md"
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )
    write = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
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
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                },
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Evidence.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 100},
            }
        ]
    )
    request = _request(tools=(lookup, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_output_tokens=100),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read":
            if "content" not in persisted:
                raise FileNotFoundError(path)
            return {
                "path": path,
                "text": persisted["content"],
                "sha256": "c" * 64,
            }
        if name == "artifact_write":
            persisted["content"] = arguments["content"]
            return {"path": path, "sha256": "c" * 64}
        return {"claim": "A"}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.output_text.endswith("REPORT_COMPLETE")
    assert result.usage["completion_tokens"] == 100
    assert result.metadata["completion_contract_auto_completed"] is True
    assert "output_budget_exhausted" not in result.metadata
    assert len(client.payloads) == 1


def test_native_executor_synthesizes_after_tool_only_contract():
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
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
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 20},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Audited the returned claim with its limits.",
                        }
                    }
                ],
                "usage": {"completion_tokens": 30},
            },
        ]
    )
    request = _request(tools=(lookup,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=1, max_output_tokens=1000),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"claim": "A", "scope": "finite"},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text == "Audited the returned claim with its limits."
    assert result.metadata["completion_tool_finalization_activated"] is True
    assert "completion_contract_auto_completed" not in result.metadata
    assert result.metadata["successful_tool_calls"] == {"lookup_claim": 1}
    assert len(client.payloads) == 2
    final_payload = client.payloads[1][0]
    assert "tools" not in final_payload
    assert "tool_choice" not in final_payload
    assert final_payload["max_tokens"] == 980
    assert "does not replace your semantic analysis" in final_payload["messages"][-1]["content"]


def test_native_executor_preserves_tool_only_contract_when_finalization_hits_limit():
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 20},
            },
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "max_tokens",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "The semantic audit used its remaining budget.",
                        },
                    }
                ],
                "usage": {"completion_tokens": 80},
            },
        ]
    )
    request = _request(tools=(lookup,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=1, max_output_tokens=100),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"claim": "A"},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text == "Reasoning verified every completion requirement."
    assert result.usage["completion_tokens"] == 100
    assert result.metadata["completion_tool_finalization_activated"] is True
    assert result.metadata["completion_tool_finalization_output_limited"] is True
    assert result.metadata["completion_contract_auto_completed"] is True
    assert result.metadata["recovery_attempted"] is False
    assert "output_budget_exhausted" not in result.metadata
    assert result.metadata["successful_tool_calls"] == {"lookup_claim": 1}
    assert len(client.payloads) == 2


def test_native_executor_preserves_tool_only_contract_when_finalization_inference_fails():
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )

    class ToolThenRateLimited:
        def __init__(self):
            self.calls = 0
            self.payloads = []

        def complete(self, payload, *, timeout):
            self.calls += 1
            self.payloads.append((json.loads(json.dumps(payload)), timeout))
            if self.calls == 1:
                return {
                    "id": "gen_tool",
                    "model": "stealth/ox-alpha",
                    "provider": "Stealth",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "lookup_1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup_claim",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"completion_tokens": 20},
                }
            raise OpenRouterError(
                "upstream shared pool is rate limited",
                status_code=429,
            )

    client = ToolThenRateLimited()
    request = _request(tools=(lookup,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=1, max_output_tokens=1000),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )
    records = []
    events = []

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"claim": "A", "scope": "finite"},
        call_recorder=records.append,
        rate_limit_retry_delays=(),
    ).execute(request, events.append)

    assert result.status == "completed"
    assert result.output_text == "Reasoning verified every completion requirement."
    assert result.metadata["completion_tool_finalization_activated"] is True
    assert result.metadata["completion_tool_finalization_failed"] is True
    assert result.metadata["completion_contract_auto_completed"] is True
    assert result.metadata["successful_tool_calls"] == {"lookup_claim": 1}
    assert [record["status"] for record in records] == ["completed", "failed"]
    assert len(client.payloads) == 2
    assert "tools" not in client.payloads[1][0]
    assert "tool_choice" not in client.payloads[1][0]
    assert [event.kind.value for event in events][-3:] == [
        "message.completed",
        "usage",
        "run.completed",
    ]


def test_native_executor_flags_visible_finalization_that_hits_limit():
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 20},
            },
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "max_tokens",
                        "message": {
                            "role": "assistant",
                            "content": "Partial semantic audit.",
                        },
                    }
                ],
                "usage": {"completion_tokens": 80},
            },
        ]
    )
    request = _request(tools=(lookup,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=1, max_output_tokens=100),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"claim": "A"},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text == "Partial semantic audit."
    assert result.metadata["completion_tool_finalization_activated"] is True
    assert result.metadata["completion_tool_finalization_output_limited"] is True
    assert "completion_contract_auto_completed" not in result.metadata
    assert result.metadata["recovery_attempted"] is False
    assert result.metadata["successful_tool_calls"] == {"lookup_claim": 1}
    assert len(client.payloads) == 2


def test_native_executor_auto_completes_tool_only_contract_at_output_limit():
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
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
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 100},
            }
        ]
    )
    request = _request(tools=(lookup,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=1, max_output_tokens=100),
            completion_tool_calls={"lookup_claim": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"claim": "A"},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text == "Reasoning verified every completion requirement."
    assert result.metadata["completion_contract_auto_completed"] is True
    assert "completion_tool_finalization_activated" not in result.metadata
    assert len(client.payloads) == 1


def test_native_executor_repairs_marker_detached_from_required_artifact():
    path = "research/route/iteration-1/report.md"
    write = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        },
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "REPORT_COMPLETE",
                            "tool_calls": [
                                {
                                    "id": "write_placeholder",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps({"path": path, "content": "placeholder"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 10},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "REPORT_COMPLETE",
                        }
                    }
                ],
                "usage": {"completion_tokens": 10},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_report",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Evidence.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 10},
            },
        ]
    )
    request = _request(tools=(write,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_tool_calls=3),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read":
            if "content" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["content"], "sha256": "e" * 64}
        persisted["content"] = arguments["content"]
        return {"path": path, "sha256": "e" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert persisted["content"].endswith("REPORT_COMPLETE")
    assert result.metadata["completion_repair_attempted"] is True
    repair = client.payloads[2][0]
    assert repair["tool_choice"]["function"]["name"] == "artifact_write"
    assert "terminal markers REPORT_COMPLETE" in repair["messages"][-1]["content"]


def test_native_executor_restores_tools_to_repair_an_output_limit_artifact():
    path = "research/route/iteration-1/report.md"
    tool = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"completion_tokens": 8000},
            },
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_recovered",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Recovered report.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 3000},
            },
        ]
    )
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_output_tokens=16000),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
        ),
    )
    persisted = {}
    invocations = []

    def invoke(name, arguments):
        invocations.append((name, arguments))
        if name == "artifact_read":
            if "content" not in persisted:
                raise FileNotFoundError(path)
            return {
                "path": path,
                "text": persisted["content"],
                "sha256": "b" * 64,
            }
        persisted["content"] = arguments["content"]
        return {"path": path, "sha256": "b" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.output_text.startswith("Recovered and verified")
    assert result.metadata["recovery_attempted"] is True
    assert result.metadata["artifact_recovery_completed"] is True
    assert result.metadata["output_artifact_recovery_reserved"] is True
    assert "completion_repair_attempted" not in result.metadata
    assert [name for name, _arguments in invocations] == [
        "artifact_read",
        "artifact_read",
        "artifact_write",
        "artifact_read",
    ]
    recovery = client.payloads[1][0]
    assert client.payloads[0][0]["max_tokens"] == 8000
    assert recovery["tools"][0]["function"]["name"] == "artifact_write"
    assert recovery["tool_choice"] == {
        "type": "function",
        "function": {"name": "artifact_write"},
    }
    assert recovery["reasoning"] == {"effort": "low"}
    assert recovery["max_tokens"] == 4096
    assert path in recovery["messages"][-1]["content"]


def test_native_executor_reserves_recovery_for_required_artifact_write_call():
    tool = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"completion_tokens": 8000},
            },
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_recovered",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": "research/report.md",
                                                "content": "Recovered report.",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 3000},
            },
        ]
    )
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(
                request.spec.budget,
                max_output_tokens=16000,
                max_tool_calls=1,
            ),
            completion_tool_calls={"artifact_write": 1},
        ),
    )
    invocations = []

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: invocations.append((name, arguments)) or {"ok": True},
    ).execute(request)

    assert result.status == "completed"
    assert result.output_text.startswith("Recovered and verified")
    assert result.metadata["recovery_attempted"] is True
    assert result.metadata["artifact_recovery_completed"] is True
    assert result.metadata["output_artifact_recovery_reserved"] is True
    assert result.metadata["successful_tool_calls"] == {"artifact_write": 1}
    assert [name for name, _arguments in invocations] == ["artifact_write"]
    assert client.payloads[0][0]["max_tokens"] == 8000
    recovery = client.payloads[1][0]
    assert recovery["tools"][0]["function"]["name"] == "artifact_write"
    assert recovery["tool_choice"] == {
        "type": "function",
        "function": {"name": "artifact_write"},
    }
    assert recovery["reasoning"] == {"effort": "low"}
    assert recovery["max_tokens"] == 4096
    assert "artifact_write" in recovery["messages"][-1]["content"]


def test_native_executor_reserves_output_for_artifact_recovery_argument_repair():
    read_many = PreparedTool(
        name="artifact_read_many",
        description="Read artifacts",
        input_schema={"type": "object", "properties": {}},
    )
    write = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    path = "research/fused-crossing.md"
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "read_sources",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_read_many",
                                        "arguments": '{"paths":["source.md"]}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 219},
            },
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"completion_tokens": 9589},
            },
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "native_finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_truncated",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": ('{"path":"research/fused-crossing.md","content":"unterminated'),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 4096},
            },
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_repaired",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Bounded fused design.",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 200},
            },
        ]
    )
    request = _request(tools=(read_many, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=2, max_output_tokens=18_000),
            completion_tool_calls={
                "artifact_read_many": 1,
                "artifact_write": 1,
            },
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read_many":
            return {"items": [{"path": "source.md", "text": "source"}]}
        persisted.update(arguments)
        return {"path": arguments["path"], "sha256": "f" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.output_text.startswith("Recovered and verified")
    assert result.usage["completion_tokens"] == 14_104
    assert result.metadata["recovery_attempted"] is True
    assert result.metadata["tool_argument_repair_attempted"] is True
    assert result.metadata["artifact_recovery_completed"] is True
    assert "output_budget_exhausted" not in result.metadata
    assert result.metadata["successful_tool_calls"] == {
        "artifact_read_many": 1,
        "artifact_write": 1,
    }
    assert persisted == {"path": path, "content": "Bounded fused design."}
    assert [payload[0]["max_tokens"] for payload in client.payloads] == [
        9808,
        9589,
        4096,
        4096,
    ]
    assert "valid JSON object" in client.payloads[3][0]["messages"][-1]["content"]


def test_native_executor_reserves_calls_for_required_artifact():
    path = "research/route/iteration-1/report.md"
    lookup = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )
    write = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
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
                                    "id": "lookup_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_claim",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "text": "Report.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "REPORT_COMPLETE"}}],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(lookup, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=3),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
        ),
    )
    persisted = {}
    invocations = []

    def invoke(name, arguments):
        if name == "artifact_read":
            if "text" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["text"], "sha256": "d" * 64}
        invocations.append((name, arguments))
        if name == "artifact_write":
            persisted["text"] = arguments["text"]
        return {"path": path, "sha256": "d" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.metadata["completion_tool_reserve_activated"] is True
    assert result.metadata["completion_tool_calls_denied"] == 0
    assert [name for name, _arguments in invocations] == [
        "lookup_claim",
        "artifact_write",
    ]
    reserved = client.payloads[1][0]
    assert [tool["function"]["name"] for tool in reserved["tools"]] == ["artifact_write"]
    assert reserved["tool_choice"] == {
        "type": "function",
        "function": {"name": "artifact_write"},
    }
    assert path in reserved["messages"][-1]["content"]


def test_native_executor_denies_same_response_calls_that_enter_artifact_reserve():
    path = "research/route/iteration-1/report.md"
    lookup = PreparedTool("lookup_claim", "Read a claim", {"type": "object", "properties": {}})
    write = PreparedTool("artifact_write", "Write an artifact", {"type": "object", "properties": {}})
    lookup_calls = [
        {
            "id": f"lookup_{index}",
            "type": "function",
            "function": {"name": "lookup_claim", "arguments": "{}"},
        }
        for index in range(3)
    ]
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": lookup_calls,
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "text": "Report.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "REPORT_COMPLETE"}}],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(lookup, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=4),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
        ),
    )
    persisted = {}
    invoked = []

    def invoke(name, arguments):
        if name == "artifact_read":
            if "text" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["text"], "sha256": "e" * 64}
        invoked.append(name)
        if name == "artifact_write":
            persisted["text"] = arguments["text"]
        return {"path": path, "sha256": "e" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert invoked == ["lookup_claim", "lookup_claim", "artifact_write"]
    assert result.metadata["completion_tool_reserve_activated"] is True
    assert result.metadata["completion_tool_calls_denied"] == 1
    denied = json.loads(client.payloads[1][0]["messages"][-2]["content"])
    assert denied["ok"] is False
    assert "reserved" in denied["error"]


def test_native_executor_reserves_required_successful_tool_calls_before_artifact():
    path = "research/route/iteration-1/source-audit.md"
    read = PreparedTool(
        "arxiv_read",
        "Read a source",
        {"type": "object", "properties": {}},
    )
    write = PreparedTool(
        "artifact_write",
        "Write an artifact",
        {"type": "object", "properties": {}},
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
                                    "id": "read_1",
                                    "type": "function",
                                    "function": {
                                        "name": "arxiv_read",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "read_2",
                                    "type": "function",
                                    "function": {
                                        "name": "arxiv_read",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "text": "Source audit.\n\nSOURCE_AUDIT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "SOURCE_AUDIT_COMPLETE",
                        }
                    }
                ],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(read, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=4),
            completion_markers=("SOURCE_AUDIT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
            completion_tool_calls={"arxiv_read": 2},
        ),
    )
    persisted = {}
    invoked = []

    def invoke(name, arguments):
        if name == "artifact_read":
            if "text" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["text"], "sha256": "f" * 64}
        invoked.append(name)
        if name == "artifact_write":
            persisted["text"] = arguments["text"]
        return {"ok": True}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.metadata["successful_tool_calls"] == {
        "arxiv_read": 2,
        "artifact_write": 1,
    }
    assert invoked == ["arxiv_read", "arxiv_read", "artifact_write"]
    assert [payload["tool_choice"]["function"]["name"] for payload, _timeout in client.payloads[:3]] == [
        "arxiv_read",
        "arxiv_read",
        "artifact_write",
    ]


def test_native_executor_rotates_distinct_reserved_tool_names():
    lookup = PreparedTool(
        "artifact_read",
        "Read an artifact",
        {"type": "object", "properties": {}},
    )
    describe = PreparedTool(
        "noether_describe",
        "Describe a computation",
        {"type": "object", "properties": {}},
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
                                    "id": "read_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_read",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "describe_1",
                                    "type": "function",
                                    "function": {
                                        "name": "noether_describe",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Both required calls succeeded.",
                        }
                    }
                ],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(lookup, describe))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=2),
            completion_tool_calls={"artifact_read": 1, "noether_describe": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"ok": True},
    ).execute(request)

    assert result.status == "completed"
    assert result.metadata["successful_tool_calls"] == {
        "artifact_read": 1,
        "noether_describe": 1,
    }
    assert [payload["tool_choice"]["function"]["name"] for payload, _timeout in client.payloads[:2]] == [
        "artifact_read",
        "noether_describe",
    ]
    assert "exact required tool exposed in this request is noether_describe" in client.payloads[1][0]["messages"][-1]["content"]


def test_native_executor_fails_closed_after_repeated_missing_required_call():
    lookup = PreparedTool(
        "artifact_read",
        "Read an artifact",
        {"type": "object", "properties": {}},
    )
    describe = PreparedTool(
        "noether_describe",
        "Describe a computation",
        {"type": "object", "properties": {}},
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
                                    "id": "read_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_read",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The second tool is unavailable.",
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The second tool is still unavailable.",
                        }
                    }
                ],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(lookup, describe))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=2),
            completion_tool_calls={"artifact_read": 1, "noether_describe": 1},
        ),
    )

    result = ReasoningNativeExecutor(
        client,
        tool_invoker=lambda name, arguments: {"ok": True},
    ).execute(request)

    assert result.status == "failed"
    assert result.error is not None
    assert "unsatisfied completion contract after bounded repair" in result.error
    assert "noether_describe" in result.error
    assert result.metadata["successful_tool_calls"] == {"artifact_read": 1}
    assert result.metadata["completion_repair_attempted"] is True
    assert result.metadata["completion_repair_attempts"] == 1
    assert [payload["tools"][0]["function"]["name"] for payload, _timeout in client.payloads] == [
        "artifact_read",
        "noether_describe",
        "noether_describe",
    ]


def test_native_executor_schedules_required_artifact_write_after_evidence_call():
    path = "research/route/iteration-1/evaluated.md"
    compute = PreparedTool(
        "noether_compute",
        "Compute exact evidence",
        {"type": "object", "properties": {}},
    )
    write = PreparedTool(
        "artifact_write",
        "Write an artifact",
        {"type": "object", "properties": {}},
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
                                    "id": "compute_1",
                                    "type": "function",
                                    "function": {
                                        "name": "noether_compute",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_1",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "text": "924 mod 5 is 4.\n\nEVALUATION_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "EVALUATION_COMPLETE"}}],
                "usage": {},
            },
        ]
    )
    request = _request(tools=(compute, write))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=RunBudget(max_tool_calls=3),
            completion_markers=("EVALUATION_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
            completion_tool_calls={"artifact_write": 1, "noether_compute": 1},
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read":
            if "text" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["text"], "sha256": "a" * 64}
        if name == "artifact_write":
            persisted["text"] = arguments["text"]
        return {"ok": True}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.metadata["successful_tool_calls"] == {
        "noether_compute": 1,
        "artifact_write": 1,
    }
    assert [payload["tool_choice"]["function"]["name"] for payload, _timeout in client.payloads[:2]] == [
        "noether_compute",
        "artifact_write",
    ]


def test_native_executor_validates_structured_output():
    schema = {
        "type": "object",
        "properties": {"decision": {"type": "string"}},
        "required": ["decision"],
    }
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"decision":"continue"}',
                        }
                    }
                ],
                "usage": {"total_tokens": 8},
            }
        ]
    )
    result = ReasoningNativeExecutor(client).execute(_request(output_schema=schema))
    assert result.status == "completed"
    assert result.structured_output == {"decision": "continue"}
    assert client.payloads[0][0]["response_format"]["json_schema"]["strict"] is True


def test_native_executor_activates_only_the_bound_skill_revision(tmp_path):
    package = tmp_path / "sequence-scout"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: sequence-scout\n---\n\nTest offsets.\n")
    skill = PreparedSkill(
        "sequence-scout",
        "sklrev_selected",
        "sequence-scout",
        "Test offsets.",
        description="Identify integer sequences.",
        package_directory=str(package),
    )
    tool = PreparedTool(
        "skill_read",
        "Activate a bound skill",
        {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "revision_id": {"type": "string"},
            },
        },
    )
    request = _request(tools=(tool,))
    request = replace(request, skills=(skill,))
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
                                    "id": "skill_1",
                                    "type": "function",
                                    "function": {
                                        "name": "skill_read",
                                        "arguments": '{"skill_id":"sequence-scout","revision_id":"sklrev_selected"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            },
            {
                "choices": [{"message": {"role": "assistant", "content": "Activated."}}],
                "usage": {},
            },
        ]
    )

    result = ReasoningNativeExecutor(client).execute(request)

    assert result.status == "completed"
    tool_message = client.payloads[1][0]["messages"][-1]
    assert "Test offsets." in json.loads(tool_message["content"])["result"]["content"]


def test_native_executor_returns_failures_as_results():
    client = FakeClient([{"choices": []}])
    events = []
    result = ReasoningNativeExecutor(client).execute(_request(), events.append)
    assert result.status == "failed"
    assert "did not contain a choice" in result.error
    assert events[-1].kind.value == "run.failed"


def test_native_executor_records_each_billable_call_without_prompt_content():
    records = []
    client = FakeClient(
        [
            {
                "id": "gen_1",
                "model": "example/routed-model",
                "provider": "Example Provider",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Done."},
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "cost": 0.00042,
                },
            }
        ]
    )

    result = ReasoningNativeExecutor(client, call_recorder=records.append).execute(_request())

    assert result.status == "completed"
    assert len(records) == 1
    assert records[0]["run_id"] == "run_native"
    assert records[0]["sequence"] == 1
    assert records[0]["requested_model"] == "openai/gpt-oss-20b"
    assert records[0]["resolved_model"] == "example/routed-model"
    assert records[0]["usage"]["cost"] == 0.00042
    assert "messages" not in records[0]
    assert "payload" not in records[0]


def test_native_executor_records_failed_openrouter_requests():
    class FailingClient:
        def complete(self, payload, *, timeout):
            raise OpenRouterError("provider unavailable")

    records = []
    result = ReasoningNativeExecutor(FailingClient(), call_recorder=records.append).execute(_request())

    assert result.status == "failed"
    assert records[0]["status"] == "failed"
    assert records[0]["error"] == "provider unavailable"
    assert records[0]["requested_model"] == "openai/gpt-oss-20b"


def test_native_executor_retries_explicit_rate_limits_and_records_each_attempt():
    class RateLimitedOnce:
        def __init__(self):
            self.calls = 0

        def complete(self, payload, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise OpenRouterError(
                    "upstream shared pool is rate limited",
                    status_code=429,
                    retry_after_seconds=0,
                )
            return {
                "id": "gen_after_retry",
                "choices": [{"message": {"role": "assistant", "content": "Recovered."}}],
                "usage": {"total_tokens": 7},
            }

    client = RateLimitedOnce()
    records = []
    result = ReasoningNativeExecutor(
        client,
        call_recorder=records.append,
        rate_limit_retry_delays=(0,),
    ).execute(_request())

    assert result.status == "completed"
    assert result.output_text == "Recovered."
    assert client.calls == 2
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["status"] for record in records] == ["failed", "completed"]
    assert records[0]["retryable"] is True
    assert records[0]["retry_after_seconds"] == 0


def test_native_executor_does_not_retry_ambiguous_failures():
    class FailingClient:
        def __init__(self):
            self.calls = 0

        def complete(self, payload, *, timeout):
            self.calls += 1
            raise OpenRouterError("connection closed after request")

    client = FailingClient()
    result = ReasoningNativeExecutor(
        client,
        rate_limit_retry_delays=(0, 0, 0),
    ).execute(_request())

    assert result.status == "failed"
    assert client.calls == 1


def test_native_executor_recovers_once_from_reasoning_only_output_limit():
    tool = PreparedTool(
        name="lookup_claim",
        description="Read a claim",
        input_schema={"type": "object", "properties": {}},
    )
    reasoning_details = [
        {
            "type": "reasoning.summary",
            "summary": "Inspected the ledger but did not reach the answer.",
            "id": "reasoning-1",
            "format": "openai-responses-v1",
            "index": 0,
        }
    ]
    client = FakeClient(
        [
            {
                "id": "gen_exhausted",
                "model": "stealth/ox-alpha",
                "provider": "Example Provider",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "max_tokens",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "A long internal analysis",
                            "reasoning_details": reasoning_details,
                        },
                    }
                ],
                "usage": {"completion_tokens": 65536},
            },
            {
                "id": "gen_recovered",
                "model": "stealth/ox-alpha",
                "provider": "Example Provider",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "The concise answer.",
                        },
                    }
                ],
                "usage": {"completion_tokens": 32},
            },
        ]
    )
    events = []

    result = ReasoningNativeExecutor(client).execute(
        _request(tools=(tool,), effort="xhigh"),
        events.append,
    )

    assert result.status == "completed"
    assert result.output_text == "The concise answer."
    assert result.usage == {"completion_tokens": 65568}
    assert result.metadata == {
        "completions": [
            {
                "response_id": "gen_exhausted",
                "model": "stealth/ox-alpha",
                "provider": "Example Provider",
                "finish_reason": "length",
                "native_finish_reason": "max_tokens",
            },
            {
                "response_id": "gen_recovered",
                "model": "stealth/ox-alpha",
                "provider": "Example Provider",
                "finish_reason": "stop",
                "native_finish_reason": "stop",
            },
        ],
        "recovery_attempted": True,
    }
    assert client.payloads[0][0]["reasoning"] == {"effort": "xhigh"}
    recovery = client.payloads[1][0]
    assert recovery["reasoning"] == {"effort": "low"}
    assert recovery["max_tokens"] == 4096
    assert "tools" not in recovery
    assert "tool_choice" not in recovery
    assert recovery["messages"][-2]["reasoning_details"] == reasoning_details
    assert "concise final answer" in recovery["messages"][-1]["content"]
    assert events[-3].data["finish_reason"] == "stop"
    assert events[-1].data["recovery_attempted"] is True


@pytest.mark.parametrize(
    "bad_arguments",
    [
        '{"path":"research/route/iteration-1/report.md","content":"unterminated',
        json.dumps("report content emitted without an argument object"),
    ],
    ids=("truncated-json", "non-object-json"),
)
def test_native_executor_repairs_invalid_tool_arguments_once(bad_arguments):
    path = "research/route/iteration-1/report.md"
    tool = PreparedTool(
        name="artifact_write",
        description="Write an artifact",
        input_schema={"type": "object", "properties": {}},
    )
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "max_tokens",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_truncated",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": bad_arguments,
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 8192},
            },
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write_repaired",
                                    "type": "function",
                                    "function": {
                                        "name": "artifact_write",
                                        "arguments": json.dumps(
                                            {
                                                "path": path,
                                                "content": "Concise report.\n\nREPORT_COMPLETE",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"completion_tokens": 200},
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "REPORT_COMPLETE",
                        },
                    }
                ],
                "usage": {"completion_tokens": 2},
            },
        ]
    )
    request = _request(tools=(tool,))
    request = replace(
        request,
        spec=replace(
            request.spec,
            budget=replace(request.spec.budget, max_output_tokens=12288),
            completion_markers=("REPORT_COMPLETE",),
            completion_artifacts=(CompletionArtifactRequirement(path),),
        ),
    )
    persisted = {}

    def invoke(name, arguments):
        if name == "artifact_read":
            if "content" not in persisted:
                raise FileNotFoundError(path)
            return {"path": path, "text": persisted["content"], "sha256": "c" * 64}
        persisted["content"] = arguments["content"]
        return {"path": path, "sha256": "c" * 64}

    result = ReasoningNativeExecutor(client, tool_invoker=invoke).execute(request)

    assert result.status == "completed"
    assert result.metadata["tool_argument_repair_attempted"] is True
    assert result.metadata["recovery_attempted"] is False
    assert persisted["content"].endswith("REPORT_COMPLETE")
    repair = client.payloads[1][0]
    assert repair["reasoning"] == {"effort": "low"}
    assert repair["max_tokens"] == 4096
    assert repair["tools"][0]["function"]["name"] == "artifact_write"
    assert "valid JSON object" in repair["messages"][-1]["content"]
    assert "at or below 10000 Unicode characters" in repair["messages"][-1]["content"]
    assert "Do not repeat the truncated draft verbatim" in repair["messages"][-1]["content"]
    assert all(
        message.get("tool_calls", [{}])[0].get("id") != "write_truncated" for message in repair["messages"] if message.get("tool_calls")
    )


def _tool_call_response(arguments, finish_reason="tool_calls"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "lookup", "type": "function", "function": {"name": "source_search", "arguments": arguments}}
                    ],
                },
            }
        ],
        "usage": {"completion_tokens": 40},
    }


def test_native_executor_asks_once_for_valid_json_when_arguments_are_malformed_without_truncation():
    tool = PreparedTool(name="source_search", description="Search sources", input_schema={"type": "object", "properties": {}})
    client = FakeClient(
        [
            _tool_call_response('{"query": "standard conjectures surveys",}'),
            _tool_call_response(json.dumps({"query": "standard conjectures surveys"})),
            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Kleiman 1994."}}],
             "usage": {"completion_tokens": 5}},
        ]
    )
    calls = []
    result = ReasoningNativeExecutor(client, tool_invoker=lambda name, arguments: calls.append(arguments) or {"items": []}).execute(
        _request(tools=(tool,))
    )

    assert result.status == "completed" and result.output_text == "Kleiman 1994."
    assert result.metadata["tool_argument_repair_attempted"] is True
    assert calls == [{"query": "standard conjectures surveys"}], "the malformed call never ran"
    repair = client.payloads[1][0]
    assert "not a valid JSON object" in repair["messages"][-1]["content"]
    assert "output limit" not in repair["messages"][-1]["content"]
    assert repair.get("max_tokens") == client.payloads[0][0].get("max_tokens"), "an untruncated call keeps its budget"
    assert repair.get("reasoning") == client.payloads[0][0].get("reasoning")


def test_native_executor_fails_when_the_repaired_tool_call_is_malformed_again():
    tool = PreparedTool(name="source_search", description="Search sources", input_schema={"type": "object", "properties": {}})
    client = FakeClient([_tool_call_response('{"query": '), _tool_call_response("[1, 2]")])
    calls = []
    result = ReasoningNativeExecutor(client, tool_invoker=lambda name, arguments: calls.append(arguments)).execute(
        _request(tools=(tool,))
    )

    assert result.status == "failed" and result.error.startswith("model returned invalid arguments for source_search")
    assert len(client.payloads) == 2 and calls == []


def test_native_executor_fails_when_recovery_is_also_empty():
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"completion_tokens": 100},
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "   "},
                    }
                ],
                "usage": {"completion_tokens": 1},
            },
        ]
    )
    events = []

    result = ReasoningNativeExecutor(client).execute(_request(), events.append)

    assert result.status == "failed"
    assert "bounded recovery attempt also returned no content" in result.error
    assert result.usage == {"completion_tokens": 101}
    assert result.metadata["recovery_attempted"] is True
    assert all(event.kind.value != "message.completed" for event in events)
    assert events[-1].kind.value == "run.failed"
    assert events[-1].data["completions"][-1]["finish_reason"] == "stop"


def test_native_executor_rejects_empty_terminal_success():
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
                "usage": {},
            }
        ]
    )

    result = ReasoningNativeExecutor(client).execute(_request())

    assert result.status == "failed"
    assert "no visible assistant content" in result.error
    assert result.metadata["recovery_attempted"] is False
