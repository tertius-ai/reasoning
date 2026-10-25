"""Reasoning-owned agent loop using OpenRouter's OpenAI-compatible API."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .contracts import (
    AgentEvent,
    AgentEventKind,
    AgentRunResult,
    EventSink,
    ExecutorCapabilities,
    PreparedAgentRun,
    PromptFidelity,
    ToolInvoker,
)
from .openrouter_errors import openrouter_http_error_message, openrouter_ssl_context, openrouter_transport_error_message

_TOOL_BUDGET_FINAL_PROMPT = (
    "The tool-call budget is now exhausted. Do not request another tool. "
    "Finish from the evidence already returned, state every unresolved claim "
    "explicitly, and provide a useful bounded result. If a required artifact "
    "was not persisted, include its complete intended contents and say that it "
    "was not written."
)

_COMPLETION_REPAIR_PROMPT = (
    "Your last message stopped before satisfying the run completion contract. "
    "Missing requirements: {requirements}. Reasoning intentionally exposes only "
    "the next required tool name in this request; later required tools will be "
    "exposed after that call succeeds. Do not narrate or promise a future action. "
    "Call the exposed tool now, then continue through the remaining requirements."
)

_COMPLETION_REPAIR_MAX_TOKENS = 4096

_COMPLETION_MARKER_REPAIR_PROMPT = (
    "Every required artifact and successful tool call is already verified. Do "
    "not call another tool or add analysis. Return only the still-missing "
    "terminal marker: {requirements}."
)

_COMPLETION_TOOL_FINAL_PROMPT = (
    "Every required successful tool call is now verified. Do not call another "
    "tool. Read the returned tool evidence and give the requested final answer "
    "now, preserving every stated limitation and unresolved claim. The "
    "completion contract proves that the calls succeeded; it does not replace "
    "your semantic analysis of their results."
)

_COMPLETION_TOOL_FINAL_MAX_TOKENS = 4096

_COMPLETION_TOOL_RESERVE_PROMPT = (
    "Reasoning has reserved the remaining tool-call capacity for the workflow's "
    "required artifacts. Stop research calls and use artifact_write now. Write "
    "one of these still-missing or stale exact paths: {paths}. Follow the granted "
    "tool schema exactly, include every required terminal marker in the artifact, "
    "and do not return prose before the write."
)

_COMPLETION_REQUIRED_TOOL_RESERVE_PROMPT = (
    "Reasoning has reserved the remaining tool-call capacity for the workflow's "
    "completion contract. The outstanding successful tool calls are: {requirements}. "
    "The exact required tool exposed in this request is {tool_name}. "
    "Only that next required tool name is intentionally exposed; "
    "other required names will be exposed on later continuations after it succeeds. "
    "Call the exposed tool now. A failed or rejected call does not satisfy the "
    "contract. Finish the required source or computation calls before writing the "
    "final artifact."
)

_TOOL_ARGUMENT_REPAIR_PROMPT = (
    "Your previous {tool_name} tool call reached the output limit and its JSON "
    "arguments were incomplete or did not decode to the required JSON object. "
    "The call was rejected and no tool ran. Retry the required action now with one "
    "concise, complete tool call whose arguments are a valid JSON object. Do not "
    "repeat the truncated draft verbatim: summarize it aggressively, keep any "
    "artifact content at or below 10000 Unicode characters, preserve the essential "
    "evidence, conclusion, and terminal marker, and omit nonessential exposition."
)
_TOOL_ARGUMENT_FORMAT_PROMPT = (
    "Your previous {tool_name} tool call was rejected because its arguments were not "
    "a valid JSON object, and no tool ran. Retry the required action now with one "
    "tool call whose arguments are a single valid JSON object matching the tool's "
    "parameters: no text outside the object, no trailing commas, and every string quoted."
)

_OUTPUT_BUDGET_COMPLETION_PROMPT = (
    "This run has used {used} of its cumulative {limit} output-token budget. "
    "Stop discretionary analysis. Complete only the still-required calls, persist "
    "the required artifact and terminal marker, and finish within the remaining "
    "{remaining} tokens. Outstanding requirements: {requirements}."
)
_OUTPUT_BUDGET_COMPLETION_FRACTION = 0.6


class _RunCancelled(RuntimeError):
    """Internal control-flow signal for cooperative executor cancellation."""


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise _RunCancelled("run interrupted")


def _wait_for_retry(delay: float, cancelled: Callable[[], bool] | None) -> None:
    """Wait for a provider retry while remaining responsive to interruption."""

    remaining = max(0.0, delay)
    while remaining > 0:
        _raise_if_cancelled(cancelled)
        interval = min(0.05, remaining)
        started = time.monotonic()
        time.sleep(interval)
        remaining -= time.monotonic() - started
    _raise_if_cancelled(cancelled)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _pending_completion_artifacts(
    request: PreparedAgentRun,
    tool_invoker: ToolInvoker | None,
) -> list[str]:
    pending: list[str] = []
    artifact_contents: list[str] = []
    for requirement in request.spec.completion_artifacts:
        try:
            artifact = tool_invoker("artifact_read", {"path": requirement.path}) if tool_invoker is not None else None
        except Exception:
            artifact = None
        if (
            not isinstance(artifact, Mapping)
            or not isinstance(artifact.get("sha256"), str)
            or requirement.previous_sha256 == artifact["sha256"]
        ):
            pending.append(requirement.path)
            continue
        artifact_text = artifact.get("text")
        if isinstance(artifact_text, str):
            artifact_contents.append(artifact_text)
    if request.spec.completion_artifacts and any(marker not in "\n".join(artifact_contents) for marker in request.spec.completion_markers):
        first_path = request.spec.completion_artifacts[0].path
        if first_path not in pending:
            pending.append(first_path)
    return pending


def _pending_completion_tool_calls(
    request: PreparedAgentRun,
    successful_tool_calls: Mapping[str, int],
) -> dict[str, int]:
    requirements = sorted(
        request.spec.completion_tool_calls.items(),
        key=lambda item: item[0] == "artifact_write",
    )
    return {
        name: minimum - int(successful_tool_calls.get(name, 0))
        for name, minimum in requirements
        if int(successful_tool_calls.get(name, 0)) < minimum
    }


def _completion_failures(
    request: PreparedAgentRun,
    text: str,
    tool_invoker: ToolInvoker | None,
    successful_tool_calls: Mapping[str, int],
) -> list[str]:
    artifact_contents: list[str] = []
    missing_artifacts: list[str] = []
    unchanged_artifacts: list[str] = []
    for requirement in request.spec.completion_artifacts:
        try:
            artifact = tool_invoker("artifact_read", {"path": requirement.path}) if tool_invoker is not None else None
        except Exception:
            artifact = None
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("sha256"), str):
            missing_artifacts.append(requirement.path)
            continue
        if requirement.previous_sha256 == artifact["sha256"]:
            unchanged_artifacts.append(requirement.path)
        artifact_text = artifact.get("text")
        if isinstance(artifact_text, str):
            artifact_contents.append(artifact_text)

    marker_contents = "\n".join(artifact_contents) if request.spec.completion_artifacts else text
    missing_markers = [marker for marker in request.spec.completion_markers if marker not in marker_contents]
    failures: list[str] = []
    if missing_markers:
        failures.append("terminal markers " + ", ".join(missing_markers))
    if missing_artifacts:
        failures.append("artifacts " + ", ".join(missing_artifacts))
    if unchanged_artifacts:
        failures.append("fresh artifacts " + ", ".join(unchanged_artifacts))
    pending_tool_calls = _pending_completion_tool_calls(request, successful_tool_calls)
    if pending_tool_calls:
        failures.append("successful tool calls " + ", ".join(f"{name} x{remaining}" for name, remaining in pending_tool_calls.items()))
    return failures


class CompletionClient(Protocol):
    def complete(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]: ...


class OpenRouterError(RuntimeError):
    """An inference request could not be completed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_rate_limit(self) -> bool:
        return self.status_code == 429


class OpenRouterClient:
    """Small dependency-free client for the OpenRouter chat completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        app_url: str | None = None,
        app_name: str = "Reasoning",
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
        self.app_url = app_url or os.environ.get("REASONING_APP_URL")
        self.app_name = app_name

    def complete(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set; add it to the backend environment")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": self.app_name,
            "X-OpenRouter-Metadata": "enabled",
        }
        if self.app_url:
            headers["HTTP-Referer"] = self.app_url
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=openrouter_ssl_context()) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retry_after = None
            if exc.code == 429:
                try:
                    retry_after = max(0.0, float(exc.headers.get("Retry-After", "")))
                except (TypeError, ValueError):
                    retry_after = None
            raise OpenRouterError(
                openrouter_http_error_message(exc.code, detail),
                status_code=exc.code,
                retry_after_seconds=retry_after,
            ) from exc
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter returned a response Reasoning could not read. Try the request again.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenRouterError(openrouter_transport_error_message(exc)) from exc


class _Events:
    def __init__(self, run_id: str, sink: EventSink | None):
        self.run_id = run_id
        self.sink = sink
        self.sequence = 0

    def emit(self, kind: AgentEventKind, data: dict[str, Any] | None = None) -> None:
        self.sequence += 1
        if self.sink:
            self.sink(
                AgentEvent(
                    run_id=self.run_id,
                    sequence=self.sequence,
                    kind=kind,
                    at=_now(),
                    data=data or {},
                )
            )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") in {"text", "output_text"})


def _merge_usage(total: dict[str, Any], usage: Mapping[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        elif key not in total:
            total[key] = value


def _output_tokens_used(usage: Mapping[str, Any]) -> int:
    for key in ("completion_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return 0


_OUTPUT_LIMIT_REASONS = {"length", "max_tokens", "max_output_tokens"}
_RECOVERY_MAX_TOKENS = 4096
_ARTIFACT_RECOVERY_TOKEN_ATTEMPTS = 2
_RECOVERY_PROMPT = (
    "Your previous response reached its output limit before producing visible "
    "assistant content. Return only a concise final answer to the original request "
    "now. Do not call tools or repeat the analysis, and satisfy any required output "
    "format."
)

_ARTIFACT_RECOVERY_PROMPT = (
    "Your previous response reached its output limit after analysis but before "
    "persisting the required result. Missing completion requirements: {requirements}. "
    "Do not repeat the analysis or return prose first. Use the forced artifact_write "
    "call now to write the complete bounded result to its exact required path, include "
    "every required terminal marker in that artifact, and omit nonessential exposition."
)


def _finish_metadata(response: Mapping[str, Any], choice: Mapping[str, Any]) -> dict[str, Any]:
    """Keep small, durable routing and termination diagnostics for one completion."""

    values = {
        "response_id": response.get("id"),
        "model": response.get("model"),
        "provider": response.get("provider"),
        "finish_reason": choice.get("finish_reason") or response.get("finish_reason"),
        "native_finish_reason": choice.get("native_finish_reason") or response.get("native_finish_reason"),
    }
    return {key: value for key, value in values.items() if value is not None}


def _hit_output_limit(metadata: Mapping[str, Any]) -> bool:
    return any(
        isinstance(metadata.get(key), str) and metadata[key].strip().lower() in _OUTPUT_LIMIT_REASONS
        for key in ("finish_reason", "native_finish_reason")
    )


def _empty_output_error(metadata: Mapping[str, Any], *, recovery_attempted: bool) -> str:
    finish = metadata.get("finish_reason", "unknown")
    native = metadata.get("native_finish_reason", "unknown")
    if recovery_attempted:
        return (
            "OpenRouter exhausted its output limit before producing visible assistant "
            "content, and the bounded recovery attempt also returned no content "
            f"(finish_reason={finish!r}, native_finish_reason={native!r})"
        )
    return f"OpenRouter returned no visible assistant content (finish_reason={finish!r}, native_finish_reason={native!r})"


class ReasoningNativeExecutor:
    """Observable model/tool loop with no third-party harness prompt."""

    def __init__(
        self,
        client: CompletionClient | None = None,
        *,
        tool_invoker: ToolInvoker | None = None,
        call_recorder: Callable[[Mapping[str, Any]], Any] | None = None,
        rate_limit_retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        max_output_continuations: int | None = 0,
        continue_reasoning_only: bool = False,
    ):
        self.client = client or OpenRouterClient()
        self.tool_invoker = tool_invoker
        self.call_recorder = call_recorder
        self.rate_limit_retry_delays = rate_limit_retry_delays
        if max_output_continuations is not None and (type(max_output_continuations) is not int or not 0 <= max_output_continuations <= 3):
            raise ValueError("output continuations must be an integer between zero and three")
        self.max_output_continuations = max_output_continuations
        self.continue_reasoning_only = continue_reasoning_only

    def _record_call(self, record: Mapping[str, Any], errors: list[str]) -> None:
        if self.call_recorder is None:
            return
        try:
            self.call_recorder(record)
        except Exception as exc:  # accounting must not retry a billable model call
            errors.append(str(exc))

    @staticmethod
    def _read_skill(request: PreparedAgentRun, arguments: dict[str, Any]) -> dict[str, Any]:
        skill_id = arguments.get("skill_id")
        revision_id = arguments.get("revision_id")
        relative = arguments.get("path", "SKILL.md")
        if not all(isinstance(value, str) and value for value in (skill_id, revision_id, relative)):
            raise ValueError("skill_read needs non-empty skill_id, revision_id, and path")
        skill = next(
            (item for item in request.skills if item.skill_id == skill_id and item.revision_id == revision_id),
            None,
        )
        if skill is None or skill.package_directory is None:
            raise ValueError("skill revision is not granted to this run")
        package = Path(skill.package_directory).resolve()
        target = (package / relative).resolve()
        if target == package or not target.is_relative_to(package):
            raise ValueError("skill file path escapes the package")
        if not target.is_file():
            raise ValueError(f"unknown skill package file: {relative}")
        if target.stat().st_size > 512 * 1024:
            raise ValueError("skill package file exceeds the 512 KiB read limit")
        return {
            "skill_id": skill_id,
            "revision_id": revision_id,
            "path": str(target.relative_to(package)),
            "content": target.read_text(encoding="utf-8"),
        }

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            executor_id="reasoning-native",
            display_name="Reasoning Native (OpenRouter)",
            prompt_fidelity=PromptFidelity.WIRE_EXACT,
            supports_tools=True,
            supports_skills=True,
            supports_structured_output=True,
            model_options=(
                "openrouter/auto",
                "openai/gpt-oss-20b",
                "openai/gpt-oss-120b",
                "qwen/qwen3-coder",
            ),
        )

    def execute(
        self,
        request: PreparedAgentRun,
        emit: EventSink | None = None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        events = _Events(request.run_id, emit)
        events.emit(
            AgentEventKind.RUN_STARTED,
            {"executor": self.capabilities.executor_id, "model": request.spec.model},
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system_prompt},
            *[dict(message) for message in request.context_messages],
            {"role": "user", "content": request.user_prompt},
        ]
        payload: dict[str, Any] = {
            "model": request.spec.model,
            "messages": messages,
        }
        if request.spec.budget.max_output_tokens is not None:
            payload["max_tokens"] = request.spec.budget.max_output_tokens
        if request.spec.effort is not None:
            payload["reasoning"] = {"effort": request.spec.effort}
        tool_definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
        tool_limit = float("inf") if request.spec.budget.max_tool_calls is None else request.spec.budget.max_tool_calls
        if tool_definitions and tool_limit > 0:
            payload["tools"] = tool_definitions
            payload["tool_choice"] = "auto"
        if request.spec.output_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "reasoning_agent_output",
                    "strict": True,
                    "schema": request.spec.output_schema,
                },
            }

        usage: dict[str, Any] = {}
        tool_count = 0
        successful_tool_calls: dict[str, int] = {}
        final_text = ""
        continued_text = ""
        output_continuations = 0
        structured: Any = None
        completion_metadata: list[dict[str, Any]] = []
        accounting_errors: list[str] = []
        recovery_attempted = False
        artifact_recovery_completed = False
        output_limit_artifact_recovery = False
        completion_repair_attempted = False
        completion_repair_attempts = 0
        completion_repair_failure_keys: set[tuple[str, ...]] = set()
        completion_contract_auto_completed = False
        completion_tool_finalization_activated = False
        completion_tool_finalization_failed = False
        completion_tool_finalization_output_limited = False
        tool_argument_repair_attempted = False
        completion_tool_reserve_active = False
        completion_tool_reserve_name: str | None = None
        completion_tool_reserve_activated = False
        completion_tool_reserve_denied = 0
        output_budget_completion_activated = False
        output_artifact_recovery_reserved = False
        output_budget_exhausted = False
        tool_budget_exhausted = bool(request.tools) and tool_limit == 0
        tool_budget_final_turn = tool_budget_exhausted
        tool_budget_denied = 0
        if tool_budget_exhausted:
            messages.append({"role": "user", "content": _TOOL_BUDGET_FINAL_PROMPT})
        request_sequence = 0
        consecutive_rate_limits = 0
        deadline = time.monotonic() + request.spec.budget.timeout_seconds
        artifact_write_definition = next(
            (definition for definition in tool_definitions if definition["function"]["name"] == "artifact_write"),
            None,
        )
        maximum_completion_reserve = min(
            tool_limit,
            sum(request.spec.completion_tool_calls.values()) + len(request.spec.completion_artifacts) + 1,
        )
        try:
            _raise_if_cancelled(cancelled)
            while True:
                remaining_tool_calls = tool_limit - tool_count
                pending_completion_tools = (
                    _pending_completion_tool_calls(request, successful_tool_calls)
                    if 0 < remaining_tool_calls <= maximum_completion_reserve
                    else {}
                )
                pending_completion_artifacts = (
                    _pending_completion_artifacts(request, self.tool_invoker)
                    if 0 < remaining_tool_calls <= maximum_completion_reserve
                    else []
                )
                reserved_tool_calls = min(
                    tool_limit,
                    sum(pending_completion_tools.values()) + len(pending_completion_artifacts) + 1,
                )
                should_reserve_completion_tools = bool(
                    (pending_completion_tools or pending_completion_artifacts)
                    and 0 < remaining_tool_calls <= reserved_tool_calls
                    and not tool_budget_final_turn
                )
                if should_reserve_completion_tools:
                    required_tool_name = next(iter(pending_completion_tools), None)
                    required_definition = next(
                        (definition for definition in tool_definitions if definition["function"]["name"] == required_tool_name),
                        None,
                    )
                    selected_definition = required_definition or artifact_write_definition
                    if selected_definition is None:
                        raise OpenRouterError("completion contract requires a tool that is not available")
                    selected_tool_name = selected_definition["function"]["name"]
                    if not completion_tool_reserve_active or completion_tool_reserve_name != selected_tool_name:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    _COMPLETION_REQUIRED_TOOL_RESERVE_PROMPT.format(
                                        requirements=", ".join(
                                            f"{name} x{remaining}" for name, remaining in pending_completion_tools.items()
                                        ),
                                        tool_name=selected_tool_name,
                                    )
                                    if pending_completion_tools
                                    else _COMPLETION_TOOL_RESERVE_PROMPT.format(paths=", ".join(pending_completion_artifacts))
                                ),
                            }
                        )
                    completion_tool_reserve_active = True
                    completion_tool_reserve_activated = True
                    completion_tool_reserve_name = selected_tool_name
                    payload = dict(payload)
                    payload["messages"] = messages
                    payload["tools"] = [selected_definition]
                    payload["tool_choice"] = {
                        "type": "function",
                        "function": {"name": selected_definition["function"]["name"]},
                    }
                elif completion_tool_reserve_active:
                    completion_tool_reserve_active = False
                    completion_tool_reserve_name = None
                    payload = dict(payload)
                    payload["messages"] = messages
                    if remaining_tool_calls > 0 and tool_definitions:
                        payload["tools"] = tool_definitions
                        payload["tool_choice"] = "auto"

                output_limit = request.spec.budget.max_output_tokens
                if output_limit is not None:
                    output_used = _output_tokens_used(usage)
                    output_remaining = output_limit - output_used
                    if output_remaining <= 0:
                        output_budget_exhausted = True
                        raise OpenRouterError(f"OpenRouter exhausted the Reasoning cumulative output-token budget of {output_limit}")
                    if (
                        not output_budget_completion_activated
                        and not (
                            recovery_attempted
                            or completion_repair_attempted
                            or tool_argument_repair_attempted
                            or output_limit_artifact_recovery
                        )
                        and (request.spec.completion_markers or request.spec.completion_artifacts or request.spec.completion_tool_calls)
                        and output_used >= output_limit * _OUTPUT_BUDGET_COMPLETION_FRACTION
                    ):
                        completion_failures = _completion_failures(
                            request,
                            "",
                            self.tool_invoker,
                            successful_tool_calls,
                        )
                        if completion_failures:
                            output_budget_completion_activated = True
                            messages.append(
                                {
                                    "role": "user",
                                    "content": _OUTPUT_BUDGET_COMPLETION_PROMPT.format(
                                        used=output_used,
                                        limit=output_limit,
                                        remaining=output_remaining,
                                        requirements="; ".join(completion_failures),
                                    ),
                                }
                            )
                            payload = dict(payload)
                            payload["messages"] = messages
                    request_output_limit = output_remaining
                    pending_artifact_write_call = (
                        _pending_completion_tool_calls(
                            request,
                            successful_tool_calls,
                        ).get("artifact_write", 0)
                        > 0
                    )
                    if not (
                        recovery_attempted
                        or completion_repair_attempted
                        or tool_argument_repair_attempted
                        or output_limit_artifact_recovery
                    ) and (
                        (
                            request.spec.completion_artifacts
                            and _pending_completion_artifacts(
                                request,
                                self.tool_invoker,
                            )
                        )
                        or pending_artifact_write_call
                    ):
                        recovery_reserve = min(
                            _ARTIFACT_RECOVERY_TOKEN_ATTEMPTS * _RECOVERY_MAX_TOKENS,
                            max(1, output_limit // 2),
                        )
                        if output_remaining > recovery_reserve:
                            output_artifact_recovery_reserved = True
                            request_output_limit -= recovery_reserve
                    payload["max_tokens"] = min(
                        int(payload.get("max_tokens", request_output_limit)),
                        request_output_limit,
                    )

                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise OpenRouterError("agent exceeded its total time budget")
                _raise_if_cancelled(cancelled)
                call_started_at = _now()
                call_started = time.monotonic()
                request_sequence += 1
                call_sequence = request_sequence
                try:
                    response = self.client.complete(
                        payload,
                        timeout=timeout,
                    )
                except Exception as exc:
                    retry_delay = None
                    if (
                        isinstance(exc, OpenRouterError)
                        and exc.is_rate_limit
                        and consecutive_rate_limits < len(self.rate_limit_retry_delays)
                    ):
                        configured_delay = self.rate_limit_retry_delays[consecutive_rate_limits]
                        retry_delay = max(
                            configured_delay,
                            exc.retry_after_seconds or 0.0,
                        )
                        if time.monotonic() + retry_delay >= deadline:
                            retry_delay = None
                    self._record_call(
                        {
                            "run_id": request.run_id,
                            "sequence": call_sequence,
                            "status": "failed",
                            "started_at": call_started_at,
                            "completed_at": _now(),
                            "duration_ms": (time.monotonic() - call_started) * 1000,
                            "requested_model": payload.get("model"),
                            "error": str(exc),
                            "retryable": retry_delay is not None,
                            "retry_after_seconds": retry_delay,
                        },
                        accounting_errors,
                    )
                    if retry_delay is not None:
                        consecutive_rate_limits += 1
                        _wait_for_retry(retry_delay, cancelled)
                        continue
                    tool_only_contract_satisfied = bool(
                        isinstance(exc, OpenRouterError)
                        and completion_tool_finalization_activated
                        and request.spec.completion_tool_calls
                        and not request.spec.completion_markers
                        and not request.spec.completion_artifacts
                        and request.spec.output_schema is None
                        and not _pending_completion_tool_calls(
                            request,
                            successful_tool_calls,
                        )
                    )
                    if tool_only_contract_satisfied:
                        completion_tool_finalization_failed = True
                        completion_contract_auto_completed = True
                        final_text = "Reasoning verified every completion requirement."
                        events.emit(
                            AgentEventKind.MESSAGE_DELTA,
                            {"text": final_text},
                        )
                        break
                    raise
                consecutive_rate_limits = 0
                choices = response.get("choices") or []
                choice_for_record = choices[0] if choices and isinstance(choices[0], dict) else {}
                self._record_call(
                    {
                        "run_id": request.run_id,
                        "sequence": call_sequence,
                        "status": "completed",
                        "started_at": call_started_at,
                        "completed_at": _now(),
                        "duration_ms": (time.monotonic() - call_started) * 1000,
                        "requested_model": payload.get("model"),
                        "response_id": response.get("id"),
                        "resolved_model": response.get("model"),
                        "upstream_provider": response.get("provider"),
                        "finish_reason": choice_for_record.get("finish_reason") or response.get("finish_reason"),
                        "native_finish_reason": choice_for_record.get("native_finish_reason") or response.get("native_finish_reason"),
                        "usage": response.get("usage") or {},
                    },
                    accounting_errors,
                )
                _merge_usage(usage, response.get("usage") or {})
                _raise_if_cancelled(cancelled)
                if request.spec.budget.max_output_tokens is not None and _output_tokens_used(usage) > request.spec.budget.max_output_tokens:
                    output_budget_exhausted = True
                if not choices or not isinstance(choices[0], dict):
                    raise OpenRouterError("OpenRouter response did not contain a choice")
                choice = choices[0]
                termination = _finish_metadata(response, choice)
                completion_metadata.append(termination)
                message = choice.get("message") or {}
                if not isinstance(message, dict):
                    raise OpenRouterError("OpenRouter choice did not contain a message")

                reasoning = message.get("reasoning") or message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    events.emit(AgentEventKind.REASONING_DELTA, {"text": reasoning})
                text = _content_text(message.get("content"))
                tool_calls = message.get("tool_calls") or []
                assistant_message = {
                    key: value
                    for key, value in message.items()
                    if key
                    in {
                        "role",
                        "content",
                        "tool_calls",
                        "reasoning",
                        "reasoning_content",
                        "reasoning_details",
                    }
                }
                assistant_message.setdefault("role", "assistant")
                messages.append(assistant_message)

                if not tool_calls:
                    resumable = bool(text.strip() or self.continue_reasoning_only and (
                        message.get("reasoning") or message.get("reasoning_content") or message.get("reasoning_details")))
                    if resumable and _hit_output_limit(termination) and (
                        self.max_output_continuations is None or output_continuations < self.max_output_continuations
                    ):
                        continued_text += text
                        final_text = continued_text
                        output_continuations += 1
                        events.emit(AgentEventKind.MESSAGE_DELTA, {"text": final_text})
                        messages.append({
                            "role": "user",
                            "content": ("Continue the interrupted answer exactly where it stopped, without repeating it. "
                                if text.strip() else "Continue from the preserved reasoning and produce a visible answer. ")
                            + "Finish the answer rather than starting a new investigation. Do not request tools.",
                        })
                        payload = {key: value for key, value in payload.items() if key not in {"tools", "tool_choice"}}
                        payload["messages"] = messages
                        continue
                    if not text.strip():
                        tool_only_contract_satisfied = bool(
                            completion_tool_finalization_activated
                            and request.spec.completion_tool_calls
                            and not request.spec.completion_markers
                            and not request.spec.completion_artifacts
                            and not _pending_completion_tool_calls(
                                request,
                                successful_tool_calls,
                            )
                        )
                        if _hit_output_limit(termination) and tool_only_contract_satisfied:
                            completion_tool_finalization_output_limited = True
                            completion_contract_auto_completed = True
                            final_text = "Reasoning verified every completion requirement."
                            events.emit(
                                AgentEventKind.MESSAGE_DELTA,
                                {"text": final_text},
                            )
                            break
                        if _hit_output_limit(termination) and not recovery_attempted:
                            recovery_attempted = True
                            recovery_payload = dict(payload)
                            completion_failures = _completion_failures(
                                request,
                                "",
                                self.tool_invoker,
                                successful_tool_calls,
                            )
                            pending_required_tools = _pending_completion_tool_calls(request, successful_tool_calls)
                            artifact_write = next(
                                (definition for definition in tool_definitions if definition["function"]["name"] == "artifact_write"),
                                None,
                            )
                            if (
                                request.spec.completion_artifacts
                                and not pending_required_tools
                                and request.spec.output_schema is None
                                and completion_failures
                                and artifact_write is not None
                                and tool_count < tool_limit
                            ):
                                output_limit_artifact_recovery = True
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": _ARTIFACT_RECOVERY_PROMPT.format(requirements="; ".join(completion_failures)),
                                    }
                                )
                                recovery_payload["tools"] = [artifact_write]
                                recovery_payload["tool_choice"] = {
                                    "type": "function",
                                    "function": {"name": "artifact_write"},
                                }
                            elif pending_required_tools and request.tools and tool_count < tool_limit:
                                artifact_write_only_recovery = set(pending_required_tools) == {"artifact_write"}
                                if artifact_write_only_recovery:
                                    output_limit_artifact_recovery = True
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": (
                                            _ARTIFACT_RECOVERY_PROMPT if artifact_write_only_recovery else _COMPLETION_REPAIR_PROMPT
                                        ).format(requirements="; ".join(completion_failures)),
                                    }
                                )
                                required_name = next(iter(pending_required_tools))
                                required_definition = next(
                                    definition for definition in tool_definitions if definition["function"]["name"] == required_name
                                )
                                recovery_payload["tools"] = [required_definition]
                                recovery_payload["tool_choice"] = {
                                    "type": "function",
                                    "function": {"name": required_name},
                                }
                            else:
                                messages.append({"role": "user", "content": _RECOVERY_PROMPT})
                                recovery_payload.pop("tools", None)
                                recovery_payload.pop("tool_choice", None)
                            recovery_payload["max_tokens"] = min(
                                int(payload.get("max_tokens", _RECOVERY_MAX_TOKENS)),
                                _RECOVERY_MAX_TOKENS,
                            )
                            recovery_payload["reasoning"] = {"effort": "low"}
                            payload = recovery_payload
                            continue
                        raise OpenRouterError(
                            _empty_output_error(
                                termination,
                                recovery_attempted=recovery_attempted,
                            )
                        )
                    completion_failures = _completion_failures(
                        request,
                        continued_text + text,
                        self.tool_invoker,
                        successful_tool_calls,
                    )
                    if completion_tool_finalization_activated and _hit_output_limit(termination):
                        completion_tool_finalization_output_limited = True
                    completion_repair_key = tuple(completion_failures)
                    if completion_failures and not tool_budget_final_turn and completion_repair_key not in completion_repair_failure_keys:
                        completion_repair_attempted = True
                        completion_repair_attempts += 1
                        completion_repair_failure_keys.add(completion_repair_key)
                        pending_required_tools = _pending_completion_tool_calls(request, successful_tool_calls)
                        pending_required_artifacts = _pending_completion_artifacts(request, self.tool_invoker)
                        marker_only_repair = bool(
                            request.spec.output_schema is None and not pending_required_tools and not pending_required_artifacts
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (_COMPLETION_MARKER_REPAIR_PROMPT if marker_only_repair else _COMPLETION_REPAIR_PROMPT).format(
                                    requirements="; ".join(completion_failures)
                                ),
                            }
                        )
                        payload = dict(payload)
                        payload["messages"] = messages
                        if marker_only_repair:
                            payload.pop("tools", None)
                            payload.pop("tool_choice", None)
                        elif pending_required_tools:
                            required_name = next(iter(pending_required_tools))
                            required_definition = next(
                                (definition for definition in tool_definitions if definition["function"]["name"] == required_name),
                                None,
                            )
                            if required_definition is None:
                                raise OpenRouterError(f"completion contract requires unavailable tool {required_name}")
                            payload["tools"] = [required_definition]
                            payload["tool_choice"] = {
                                "type": "function",
                                "function": {"name": required_name},
                            }
                        elif pending_required_artifacts:
                            if artifact_write_definition is None:
                                raise OpenRouterError("completion contract requires artifact_write, but the tool is unavailable")
                            payload["tools"] = [artifact_write_definition]
                            payload["tool_choice"] = {
                                "type": "function",
                                "function": {"name": "artifact_write"},
                            }
                        else:
                            payload["tools"] = tool_definitions
                            payload["tool_choice"] = "auto"
                        payload["max_tokens"] = min(
                            int(request.spec.budget.max_output_tokens or _COMPLETION_REPAIR_MAX_TOKENS),
                            _COMPLETION_REPAIR_MAX_TOKENS,
                        )
                        payload["reasoning"] = {"effort": "low"}
                        continue
                    if completion_failures:
                        raise OpenRouterError(
                            "model stopped with an unsatisfied completion contract after bounded repair: " + "; ".join(completion_failures)
                        )
                    final_text = continued_text + text
                    events.emit(AgentEventKind.MESSAGE_DELTA, {"text": final_text})
                    break
                if tool_budget_final_turn:
                    raise OpenRouterError("model requested a tool during its tool-budget final turn")
                parsed_tool_calls = []
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        raise OpenRouterError("model returned a tool call without a name")
                    raw_arguments = function.get("arguments") or "{}"
                    argument_error: str | None = None
                    try:
                        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    except json.JSONDecodeError as exc:
                        arguments = None
                        argument_error = str(exc)
                    if argument_error is None and not isinstance(arguments, dict):
                        argument_error = f"decoded arguments have type {type(arguments).__name__}, expected object"
                    if argument_error is not None:
                        # One repair: a truncated call is retried shorter; a malformed one is asked again for valid JSON.
                        if not tool_argument_repair_attempted:
                            tool_argument_repair_attempted = True
                            truncated = _hit_output_limit(termination)
                            messages.pop()
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (_TOOL_ARGUMENT_REPAIR_PROMPT if truncated else _TOOL_ARGUMENT_FORMAT_PROMPT).format(
                                        tool_name=name
                                    ),
                                }
                            )
                            payload = dict(payload)
                            payload["messages"] = messages
                            if truncated:
                                payload["max_tokens"] = min(
                                    int(request.spec.budget.max_output_tokens or _COMPLETION_REPAIR_MAX_TOKENS),
                                    _COMPLETION_REPAIR_MAX_TOKENS,
                                )
                                payload["reasoning"] = {"effort": "low"}
                            break
                        raise OpenRouterError(f"model returned invalid arguments for {name}: {argument_error}")
                    parsed_tool_calls.append((call, name, arguments))
                if len(parsed_tool_calls) != len(tool_calls):
                    continue
                for call, name, arguments in parsed_tool_calls:
                    _raise_if_cancelled(cancelled)
                    events.emit(
                        AgentEventKind.TOOL_REQUESTED,
                        {
                            "tool_call_id": call.get("id"),
                            "name": name,
                            "arguments": arguments,
                        },
                    )
                    remaining_tool_calls = tool_limit - tool_count
                    pending_completion_tools = (
                        _pending_completion_tool_calls(request, successful_tool_calls)
                        if 0 < remaining_tool_calls <= maximum_completion_reserve
                        else {}
                    )
                    pending_completion_artifacts = (
                        _pending_completion_artifacts(request, self.tool_invoker)
                        if 0 < remaining_tool_calls <= maximum_completion_reserve
                        else []
                    )
                    reserved_tool_calls = min(
                        tool_limit,
                        sum(pending_completion_tools.values()) + len(pending_completion_artifacts) + 1,
                    )
                    completion_call_reserved = bool(
                        (
                            (pending_completion_tools and name not in pending_completion_tools)
                            or (not pending_completion_tools and pending_completion_artifacts and name != "artifact_write")
                        )
                        and remaining_tool_calls <= reserved_tool_calls
                    )
                    if completion_call_reserved:
                        completion_tool_reserve_denied += 1
                        tool_payload = {
                            "ok": False,
                            "error": ("tool-call capacity is reserved for required artifact writes; call artifact_write now"),
                        }
                    elif tool_count >= tool_limit:
                        tool_budget_exhausted = True
                        tool_budget_denied += 1
                        tool_payload = {
                            "ok": False,
                            "error": "tool-call budget exhausted; finish without more tools",
                        }
                    else:
                        tool_count += 1
                        try:
                            if name == "skill_read":
                                result = self._read_skill(request, arguments)
                            elif self.tool_invoker is not None:
                                result = self.tool_invoker(name, arguments)
                            else:
                                raise OpenRouterError("model requested a tool but no tool invoker is configured")
                            tool_payload = {"ok": True, "result": result}
                            successful_tool_calls[name] = successful_tool_calls.get(name, 0) + 1
                        except Exception as exc:  # tool failure is model-visible
                            tool_payload = {"ok": False, "error": str(exc)}
                    events.emit(
                        AgentEventKind.TOOL_COMPLETED,
                        {"tool_call_id": call.get("id"), "name": name, **tool_payload},
                    )
                    _raise_if_cancelled(cancelled)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "name": name,
                            "content": json.dumps(tool_payload, ensure_ascii=False),
                        }
                    )
                if output_limit_artifact_recovery:
                    recovery_failures = _completion_failures(
                        request,
                        "",
                        self.tool_invoker,
                        successful_tool_calls,
                    )
                    if recovery_failures:
                        raise OpenRouterError(
                            "output-limit artifact recovery did not satisfy the completion contract: " + "; ".join(recovery_failures)
                        )
                    artifact_recovery_completed = True
                    final_text = "Recovered and verified the required artifact after the model exhausted its initial output limit."
                    events.emit(AgentEventKind.MESSAGE_DELTA, {"text": final_text})
                    break
                has_completion_contract = bool(
                    request.spec.completion_markers or request.spec.completion_artifacts or request.spec.completion_tool_calls
                )
                if (
                    has_completion_contract
                    and request.spec.output_schema is None
                    and not _completion_failures(
                        request,
                        "",
                        self.tool_invoker,
                        successful_tool_calls,
                    )
                ):
                    output_limit = request.spec.budget.max_output_tokens
                    output_remaining = None if output_limit is None else output_limit - _output_tokens_used(usage)
                    tool_only_contract = bool(
                        request.spec.completion_tool_calls and not request.spec.completion_markers and not request.spec.completion_artifacts
                    )
                    if (
                        tool_only_contract
                        and not completion_tool_finalization_activated
                        and (output_remaining is None or output_remaining > 0)
                    ):
                        completion_tool_finalization_activated = True
                        completion_tool_reserve_active = False
                        tool_budget_final_turn = True
                        messages.append(
                            {
                                "role": "user",
                                "content": _COMPLETION_TOOL_FINAL_PROMPT,
                            }
                        )
                        payload = dict(payload)
                        payload["messages"] = messages
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        if output_remaining is not None:
                            payload["max_tokens"] = min(
                                output_remaining,
                                _COMPLETION_TOOL_FINAL_MAX_TOKENS,
                            )
                        continue
                    completion_contract_auto_completed = True
                    final_text = "Reasoning verified every completion requirement."
                    if request.spec.completion_markers:
                        final_text += "\n\n" + "\n".join(request.spec.completion_markers)
                    events.emit(AgentEventKind.MESSAGE_DELTA, {"text": final_text})
                    break
                if tool_count >= tool_limit:
                    tool_budget_exhausted = True
                if tool_budget_exhausted:
                    tool_budget_final_turn = True
                    messages.append({"role": "user", "content": _TOOL_BUDGET_FINAL_PROMPT})
                    payload = dict(payload)
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    payload["messages"] = messages

            output_limit = request.spec.budget.max_output_tokens
            if output_limit is not None and _output_tokens_used(usage) > output_limit:
                raise OpenRouterError(f"OpenRouter exceeded the Reasoning cumulative output-token budget of {output_limit}")

            if request.spec.output_schema:
                try:
                    structured = json.loads(final_text)
                except json.JSONDecodeError as exc:
                    raise OpenRouterError(f"model output was not valid JSON: {exc}") from exc
                Draft202012Validator(request.spec.output_schema).validate(structured)
            run_metadata = {
                "completions": completion_metadata,
                "recovery_attempted": recovery_attempted,
            }
            if output_continuations:
                run_metadata["output_continuations"] = output_continuations
            if tool_budget_exhausted:
                run_metadata["tool_budget_exhausted"] = True
                run_metadata["tool_calls_denied"] = tool_budget_denied
            if completion_repair_attempted:
                run_metadata["completion_repair_attempted"] = True
                run_metadata["completion_repair_attempts"] = completion_repair_attempts
            if completion_contract_auto_completed:
                run_metadata["completion_contract_auto_completed"] = True
            if completion_tool_finalization_activated:
                run_metadata["completion_tool_finalization_activated"] = True
            if completion_tool_finalization_failed:
                run_metadata["completion_tool_finalization_failed"] = True
            if completion_tool_finalization_output_limited:
                run_metadata["completion_tool_finalization_output_limited"] = True
            if artifact_recovery_completed:
                run_metadata["artifact_recovery_completed"] = True
            if tool_argument_repair_attempted:
                run_metadata["tool_argument_repair_attempted"] = True
            if completion_tool_reserve_activated:
                run_metadata["completion_tool_reserve_activated"] = True
                run_metadata["completion_tool_calls_denied"] = completion_tool_reserve_denied
            if output_budget_completion_activated:
                run_metadata["output_budget_completion_activated"] = True
            if output_artifact_recovery_reserved:
                run_metadata["output_artifact_recovery_reserved"] = True
            if output_budget_exhausted:
                run_metadata["output_budget_exhausted"] = True
            if accounting_errors:
                run_metadata["accounting_errors"] = accounting_errors
            if successful_tool_calls or request.spec.completion_tool_calls:
                run_metadata["successful_tool_calls"] = dict(successful_tool_calls)
            events.emit(
                AgentEventKind.MESSAGE_COMPLETED,
                {"text": final_text, **completion_metadata[-1]},
            )
            events.emit(AgentEventKind.USAGE, usage)
            events.emit(
                AgentEventKind.RUN_COMPLETED,
                {"tool_calls": tool_count, **run_metadata},
            )
            return AgentRunResult(
                run_id=request.run_id,
                status="completed",
                output_text=final_text,
                structured_output=structured,
                usage=usage,
                metadata=run_metadata,
            )
        except Exception as exc:
            run_metadata = {
                "completions": completion_metadata,
                "recovery_attempted": recovery_attempted,
            }
            if output_continuations:
                run_metadata["output_continuations"] = output_continuations
            if tool_budget_exhausted:
                run_metadata["tool_budget_exhausted"] = True
                run_metadata["tool_calls_denied"] = tool_budget_denied
            if completion_repair_attempted:
                run_metadata["completion_repair_attempted"] = True
                run_metadata["completion_repair_attempts"] = completion_repair_attempts
            if completion_contract_auto_completed:
                run_metadata["completion_contract_auto_completed"] = True
            if completion_tool_finalization_activated:
                run_metadata["completion_tool_finalization_activated"] = True
            if completion_tool_finalization_failed:
                run_metadata["completion_tool_finalization_failed"] = True
            if completion_tool_finalization_output_limited:
                run_metadata["completion_tool_finalization_output_limited"] = True
            if artifact_recovery_completed:
                run_metadata["artifact_recovery_completed"] = True
            if tool_argument_repair_attempted:
                run_metadata["tool_argument_repair_attempted"] = True
            if completion_tool_reserve_activated:
                run_metadata["completion_tool_reserve_activated"] = True
                run_metadata["completion_tool_calls_denied"] = completion_tool_reserve_denied
            if output_budget_completion_activated:
                run_metadata["output_budget_completion_activated"] = True
            if output_artifact_recovery_reserved:
                run_metadata["output_artifact_recovery_reserved"] = True
            if output_budget_exhausted:
                run_metadata["output_budget_exhausted"] = True
            if accounting_errors:
                run_metadata["accounting_errors"] = accounting_errors
            if successful_tool_calls or request.spec.completion_tool_calls:
                run_metadata["successful_tool_calls"] = dict(successful_tool_calls)
            events.emit(AgentEventKind.USAGE, usage)
            if isinstance(exc, _RunCancelled):
                events.emit(
                    AgentEventKind.RUN_CANCELLED,
                    {"error": str(exc), **run_metadata},
                )
                return AgentRunResult(
                    run_id=request.run_id,
                    status="cancelled",
                    output_text=final_text,
                    usage=usage,
                    error=str(exc),
                    metadata={**run_metadata, "cancelled": True},
                )
            events.emit(
                AgentEventKind.RUN_FAILED,
                {"error": str(exc), **run_metadata},
            )
            return AgentRunResult(
                run_id=request.run_id,
                status="failed",
                output_text=final_text,
                usage=usage,
                error=str(exc),
                metadata=run_metadata,
            )
