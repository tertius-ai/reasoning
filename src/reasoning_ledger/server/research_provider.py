"""Organization-priced completion transport for the shared workbench agent loop."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import re
import time
from decimal import Decimal

import httpx

from .inference_timing import Spans
from .openrouter import (
    MAX_EVENT_BYTES,
    MAX_STREAM_BYTES,
    ProviderError,
    StreamLines,
    Usage,
    json_type,
    protocol_diagnostics,
    response_rejection,
    stream_error_diagnostics,
)
from .research_context import HARNESS_VERSION, PROMPT_VERSION, encoded
from .store import StoreError
from .usage_store import UsageStore

REASONING_TEXT_FIELDS = ("text", "summary", "data", "signature")


class ReasoningFieldError(Exception):
    """A reasoning field outside the documented shape; carries only closed names."""

    def __init__(self, field, value):
        super().__init__(field)
        self.field, self.value_type = field, json_type(value)


class ReasoningDetails:
    """Accumulate streamed OpenRouter ``reasoning_details`` fragments in arrival order.

    A fragment continues the latest detail with its index (or, without an
    index, the latest detail) when its type, id, format and other non-text
    fields agree; text, summary, data and signature fragments are concatenated
    and null text fragments add nothing. A disagreeing fragment starts a new
    detail. Unknown well-formed types and fields are preserved for replay.
    """

    def __init__(self):
        self.items = []

    def add(self, fragment):
        if not isinstance(fragment, dict):
            raise ReasoningFieldError("reasoning_details[]", fragment)
        index = fragment.get("index")
        if index is not None and (type(index) is not int or index < 0):
            raise ReasoningFieldError("reasoning_details[].index", index)
        kind = fragment.get("type")
        if kind is not None and (not isinstance(kind, str) or not kind):
            raise ReasoningFieldError("reasoning_details[].type", kind)
        for field in ("id", "format", *REASONING_TEXT_FIELDS):
            value = fragment.get(field)
            if value is not None and not isinstance(value, str):
                raise ReasoningFieldError("reasoning_details[]." + field, value)
        target = self._continued(fragment, index)
        if target is None:
            target = {}
            self.items.append(target)
        for field, value in fragment.items():
            if field in REASONING_TEXT_FIELDS:
                if value is None:
                    target.setdefault(field, None)
                else:
                    target[field] = (target.get(field) or "") + value
            elif target.get(field) is None:
                target[field] = value
        return target

    def _continued(self, fragment, index):
        candidate = next((item for item in reversed(self.items) if index is None or item.get("index") == index), None)
        if candidate is None:
            return None
        for field, value in fragment.items():
            if field in REASONING_TEXT_FIELDS or field == "index" or value is None:
                continue
            if candidate.get(field) is not None and candidate[field] != value:
                return None
        return candidate

    @staticmethod
    def summary(detail):
        return detail.get("summary") or (detail.get("text") if detail.get("type") == "reasoning.summary" else None)


class ResearchCompletion:
    def __init__(self, inference, provider, row, quote):
        self.inference, self.provider, self.row, self.quote = inference, provider, row, quote
        self.ledger = UsageStore(inference.store)
        self.calls, self.errors, self.responses = [], [], []
        self.last_call_id, self.last_generation = None, None
        self.context_transform = None
        self.failure_code = None
        self.no_charge = True
        self.state_sequence = 0
        self.cancelled = lambda: False
        self.partial_usage, self.partial_generation = None, None
        self.prompt_version = PROMPT_VERSION
        self.protocol_stage = "stream_framing"
        self.received_bytes = 0
        self.next_cancel_check = 0.0
        self._last_visible_finish = None
        self.worker_spans, self._dispatch = None, None
        self._reviewer, self._prefix = None, None

    def complete(self, payload, *, timeout):
        if self.worker_spans is not None and "executor_start" not in self.worker_spans.values:
            self.worker_spans.mark("executor_start")
        spans = Spans("provider")
        if self.context_transform:
            payload = self.context_transform(payload)
        spans.mark("context_transform")
        try:
            return asyncio.run(self.submit(payload, timeout=timeout, spans=spans))
        except StoreError:
            # Admission failed before a submission receipt. Earlier paid calls
            # stay in aggregate(); do not reserve or replay the refused call.
            self.failure_code = "execution_not_authorized"
            raise ProviderError(self.failure_code, no_charge=True) from None

    async def submit(self, payload, *, timeout, purpose=None, spans=None):
        """``spans`` times this call's preparation; values are durations only, never request content."""
        spans = spans or Spans("provider")
        self.partial_usage, self.partial_generation, self._prefix = None, None, None
        messages = payload["messages"]
        if self._reviewer is None:
            # A reviewer run is linked to its review when it is enqueued, so one lookup serves every call.
            with self.inference.store.transaction() as db:
                self._reviewer = bool(
                    db.execute("SELECT 1 FROM mathematical_reviews WHERE reviewer_run_id=?", (self.row["id"],)).fetchone()
                )
        review = self._reviewer
        purpose = purpose or (
            "review" if review else "continuation" if self.calls else "workflow" if self.row["context_through_sequence"] == 0 else "answer"
        )
        if review:
            from .research_reviews import REVIEW_PROMPT_VERSION

            self.prompt_version = REVIEW_PROMPT_VERSION
        spans.mark("purpose_lookup")
        key = self.inference.execution_key(self.row)
        spans.mark("credential_check")
        from .media_context import authorize_captured_media

        authorize_captured_media(self.inference.store, self.row, messages)
        if any(ord(character) < 32 or ord(character) > 126 for character in key):
            raise ProviderError("provider_key_invalid", no_charge=True)
        spans.mark("media_check")
        tools = payload.get("tools", [])
        # Serialize the entire tool protocol into the conservative quote allowance.
        has_tool_protocol = tools or any(
            message.get("role") == "tool"
            or message.get("tool_calls")
            or message.get("reasoning")
            or message.get("reasoning_details")
            or not isinstance(message.get("content"), str)
            for message in messages
        )
        priced_context = [{"role": "user", "content": encoded({"messages": messages, "tools": tools})}] if has_tool_protocol else messages
        requested = json.loads(self.row["parameters_json"])["max_completion_tokens"]
        if purpose == "compaction":
            requested = min(payload.get("max_tokens", 8192), requested or self.quote.max_completion_tokens)
        estimated_output = self.inference.output_capacity(self.quote, priced_context, requested)
        maximum = estimated_output if requested is not None else None
        reserve = self.quote.reservation(priced_context, estimated_output)
        if has_tool_protocol:
            reserve += self.quote.image_reservation(messages)
        spans.mark("quote_estimate")
        self.inference.reserve_next_call(self.row, reserve, spans=spans, admit=lambda db: self._goal_admission(reserve, db))
        spans.mark("reservation")
        call = self.ledger.begin_call(
            self.row,
            self.quote,
            purpose=purpose,
            parent_call_id=self.last_call_id,
            harness_version=HARNESS_VERSION,
            prompt_version=self.prompt_version,
            context=messages,
            reserved_microusd=reserve,
            route=self.quote.route() | {"region": (self.quote.routing or {}).get("region", "global")},
            preparation=spans,
        )
        self.last_call_id = call
        self.stream_visible = purpose != "compaction"
        self._dispatch = Spans("dispatch")
        opening = {"type": "agent", "phase": "round", "round": len(self.responses) + 1, "purpose": purpose}
        usage, generation, submitted, recorded_usage = None, None, False, False
        try:
            request = {key: value for key, value in payload.items() if key not in {"max_tokens", "stream"}}
            if maximum is not None:
                request["max_completion_tokens"] = maximum
            request["stream"] = True
            request["usage"] = {"include": True}
            request["provider"] = self.quote.route()
            if not tools:
                request = {
                    key: value
                    for key, value in request.items()
                    if key in {"model", "messages", "max_completion_tokens", "stream", "usage", "provider", "reasoning"}
                }
            self._save_session(request, call, opening=opening)
            self._dispatch.mark("request_record")
            if not tools and not hasattr(self.provider, "client"):
                text, finish = "", None
                prefix = self._stream_prefix()
                submitted = True
                first_token = False
                async for chunk in self._checked_chunks(self.provider.stream(key, self.quote, messages, maximum)):
                    self._check_cancel()
                    text += chunk.text
                    generation = chunk.generation_id or generation
                    usage = chunk.usage or usage
                    finish = chunk.finish_reason or finish
                    if chunk.text and not first_token:
                        self._stream_first_token()
                        first_token = True
                    if purpose != "compaction" and text:
                        self.inference.progress(self.row, self._display_round(prefix, text), generation)
                response = {
                    "id": generation,
                    "model": self.row["model"],
                    "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish or "stop"}],
                }
            else:
                submitted = True
                response = await self._agent_stream(key, request, timeout)
                generation = response.get("id")
                if response.get("usage"):
                    usage = Usage.parse(response["usage"])
            if usage is None:
                raise ProviderError("usage_unavailable")
            response["usage"] = {
                "cost": usage.cost_microusd / 1_000_000,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "cached_tokens": usage.cached_tokens,
            }
            # Native's usage accumulator requires numbers for optional known fields.
            response["usage"] = {key: value for key, value in response["usage"].items() if value is not None}
            # Validate the entire native protocol before persisting or exposing it.
            # JSON permits escaped surrogate codepoints which are not UTF-8 text.
            self.protocol_stage = "text_encoding"
            encoded(response).encode("utf8")
            self._save_session(request, call, response)
            self.calls.append(usage)
            recorded_usage = True
            self.responses.append(response)
            if self.stream_visible and response["choices"][0]["message"].get("content"):
                self._last_visible_finish = response["choices"][0].get("finish_reason")
            self.last_generation = generation
            self.no_charge = False
            self.ledger.finish_call(call, status="succeeded", usage=usage, provider_generation_id=generation)
            self.inference.reserve_next_call(self.row, 0)
            return response
        except BaseException as exc:
            usage = usage or self.partial_usage
            generation = generation or self.partial_generation
            if isinstance(exc, ProviderError):
                code, free = exc.code, not submitted or exc.no_charge
                diagnostics = exc.diagnostics
            elif isinstance(exc, StoreError):
                code, free, diagnostics = "execution_not_authorized", not submitted, None
            elif isinstance(exc, (UnicodeError, ValueError, TypeError, KeyError, AttributeError, RecursionError)):
                code, free, diagnostics = "provider_protocol", not submitted, None
            else:
                code, free, diagnostics = "provider_network", not submitted, None
            if code == "provider_protocol" and diagnostics is None:
                rule = (
                    "invalid_json"
                    if isinstance(exc, json.JSONDecodeError)
                    else ("invalid_utf8" if isinstance(exc, UnicodeError) else "response_structure")
                )
                diagnostics = protocol_diagnostics(self.protocol_stage, rule, self.received_bytes)
            self.failure_code = code
            if usage and not recorded_usage:
                self.calls.append(usage)
            self.errors.append({"call_id": call, "no_charge": free or usage is not None})
            self.no_charge = self.no_charge and free
            self.ledger.finish_call(
                call,
                status="failed",
                usage=usage,
                provider_generation_id=generation,
                no_charge=free,
                error_code=code,
                diagnostics=diagnostics,
            )
            raise ProviderError(code, no_charge=free, diagnostics=diagnostics) from None

    def _goal_admission(self, reserve, db):
        """Goal time and cost admission, inside the caller's reservation transaction."""
        from .research_goals import ResearchGoals

        goal = ResearchGoals.execution_goal(db, self.row["id"])
        if not goal:
            return
        if goal["status"] not in {"active", "complete"} or goal["deadline_at"] <= int(time.time()):
            raise StoreError(409, "goal time budget exhausted")
        known = sum(usage.cost_microusd for usage in self.calls)
        prior = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN r.accounting_state='settled' THEN COALESCE(r.actual_microusd,0) "
            "WHEN r.accounting_state='released' THEN 0 ELSE r.reserved_microusd END),0) "
            "FROM llm_runs r JOIN agent_goal_events e ON json_extract(e.event_json,'$.run_id')=r.id "
            "WHERE e.goal_id=? AND json_extract(e.event_json,'$.type') IN ('turn','review') AND r.id<>?",
            (goal["id"], self.row["id"]),
        ).fetchone()[0]
        if prior + known + reserve > goal["budget_microusd"]:
            raise StoreError(409, "goal cost budget cannot cover this provider call")

    def _save_session(self, payload, call, response=None, *, opening=None):
        """Retain the session; ``opening`` also records the call's round event and reads the output prefix."""
        self.state_sequence += 1
        record = {
            "harness_version": HARNESS_VERSION,
            "prompt_version": self.prompt_version,
            "call_id": call,
            "sequence": self.state_sequence,
            "messages": payload["messages"],
            "tools": payload.get("tools", []),
            "response": response,
            "context_sha256": hashlib.sha256(encoded(payload["messages"]).encode()).hexdigest(),
        }
        with self.inference.store.transaction(write=True) as db:
            if opening is not None:
                self.inference._event(db, self.row["organization_id"], self.row["id"], opening)
                if self.worker_spans is not None and opening["purpose"] != "compaction":
                    # Claim-to-first-request worker spans, retained once with the first answer call.
                    self.inference._event(db, self.row["organization_id"], self.row["id"],
                                          {"type": "timing", "stage": "worker", "spans_ms": self.worker_spans.record()})
                    self.worker_spans = None
            db.execute(
                "INSERT INTO research_sessions VALUES(?,?,?,?,?) ON CONFLICT(run_id) "
                "DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                (self.row["id"], self.row["organization_id"], self.row["topic_id"], encoded(record), int(time.time())),
            )
            event = {"type": "agent", "phase": "provider_response" if response else "provider_request", "call_id": call}
            event["response" if response else "request"] = response if response else payload
            self.inference._event(db, self.row["organization_id"], self.row["id"], event)
            if opening is not None:
                self._prefix = db.execute("SELECT output_text FROM llm_runs WHERE id=?", (self.row["id"],)).fetchone()[0]

    def _check_cancel(self):
        now = time.monotonic()
        if now < self.next_cancel_check:
            return
        self.next_cancel_check = now + 0.05
        if self.cancelled() or self.inference.stopped(self.row):
            raise ProviderError("execution_cancelled")

    async def _check_cancel_async(self):
        self._check_cancel()

    async def _checked_chunks(self, stream):
        pending = None
        try:
            while True:
                pending = asyncio.create_task(anext(stream))
                while not pending.done():
                    await asyncio.wait({pending}, timeout=0.1)
                    await self._check_cancel_async()
                try:
                    yield pending.result()
                except StopAsyncIteration:
                    return
                pending = None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()

    async def _agent_stream(self, key, payload, timeout):
        pending = asyncio.create_task(self._read_agent_stream(key, payload, timeout))
        try:
            while not pending.done():
                await asyncio.wait({pending}, timeout=0.1)
                await self._check_cancel_async()
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def _stream_prefix(self):
        prefix, self._prefix = self._prefix, None
        if prefix is not None:
            return prefix  # Read with the call's request record; only this run's stream appends to it.
        with self.inference.store.transaction() as db:
            return db.execute("SELECT output_text FROM llm_runs WHERE id=?", (self.row["id"],)).fetchone()[0]

    def _stream_content(self, text, generation):
        return self.inference.progress(self.row, text, generation)

    def _display_round(self, prefix, text):
        """Separate tool rounds, but concatenate output-limit continuations exactly."""
        continuation = getattr(self, "_last_visible_finish", None) == "length"
        separator = "" if not prefix or not text or continuation or prefix.endswith("\n\n") else (
            "\n" if prefix.endswith("\n") else "\n\n")
        return prefix + separator + text

    def _stream_first_token(self):
        # Standalone wire-parser checks need not own an accounting call. Actual
        # provider execution binds a durable call before opening the stream.
        if self.last_call_id is not None:
            self.ledger.first_token(self.last_call_id)

    def _stream_client_ready(self):
        if getattr(self, "_dispatch", None) is not None:
            self._dispatch.mark("client_setup")

    def _stream_response_headers(self):
        if self.last_call_id is not None:
            dispatch, self._dispatch = self._dispatch, None
            self.ledger.response_headers(self.last_call_id, dispatch=dispatch.record() if dispatch else None)

    def _stream_summary(self, brief):
        with self.inference.store.transaction(write=True) as db:
            self.inference._event(
                db, self.row["organization_id"], self.row["id"], {"type": "agent", "phase": "reasoning_summary", "summary": brief}
            )

    async def _stream_prefix_async(self):
        return self._stream_prefix()

    async def _stream_content_async(self, text, generation):
        return self._stream_content(text, generation)

    async def _stream_summary_async(self, brief):
        self._stream_summary(brief)

    async def _read_agent_stream(self, key, payload, timeout):
        self.protocol_stage = "stream_framing"
        text, generation, usage, finish, tools = "", None, None, None, {}
        reasoning, details = "", ReasoningDetails()
        prefix = await self._stream_prefix_async()
        last_flush = 0.0
        last_summary = ""
        last_summary_flush = 0.0
        first_token = False

        async def summarize(summary):
            nonlocal last_summary, last_summary_flush
            brief = summary[-400:]
            try:
                brief.encode("utf8")
            except UnicodeError:
                return  # Invalid scalars must not discard a later paid usage receipt.
            await self._stream_summary_async(brief)
            last_summary, last_summary_flush = summary, time.monotonic()

        client = self.provider.client()
        self._stream_client_ready()
        async with (
            client,
            client.stream(
                "POST",
                self.quote.origin(self.provider.base_url) + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + key, "Accept": "text/event-stream"},
                timeout=httpx.Timeout(min(timeout, 1800), connect=10),
            ) as response,
        ):
            self._stream_response_headers()
            if response.status_code != 200:
                raise await response_rejection(response)
            if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                raise ProviderError("provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "content_type"))
            lines, event, event_bytes, total, done = StreamLines(), [], 0, 0, False
            decoder = codecs.getincrementaldecoder("utf8")()
            async for raw in self._checked_chunks(response.aiter_bytes()):
                await self._check_cancel_async()
                self.protocol_stage = "stream_framing"
                total += len(raw)
                self.received_bytes = total
                if total > MAX_STREAM_BYTES:
                    raise ProviderError("provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "stream_size", total))
                for line in lines.feed(decoder.decode(raw), total):
                    if line.startswith("data:"):
                        event.append(line[5:].lstrip())
                        event_bytes += len(event[-1].encode("utf8")) + 1
                        if event_bytes > MAX_EVENT_BYTES:
                            raise ProviderError(
                                "provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "event_size", total)
                            )
                    elif not line and event:
                        self.protocol_stage = "stream_framing"
                        data, event = "\n".join(event), []
                        event_bytes = 0
                        if data == "[DONE]":
                            done = True
                            break
                        if len(data.encode("utf8")) > MAX_EVENT_BYTES:
                            raise ProviderError(
                                "provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "event_size", total)
                            )
                        body = json.loads(data, parse_float=Decimal)
                        if not isinstance(body, dict):
                            raise ProviderError("provider_protocol")
                        if "error" in body:
                            raise ProviderError("provider_stream_error", diagnostics=stream_error_diagnostics(body["error"]))
                        if body.get("id") is not None:
                            self.protocol_stage = "generation"
                            if not isinstance(body["id"], str) or not re.fullmatch(r"(?:gen-|chatcmpl-)[A-Za-z0-9_-]{1,180}", body["id"]):
                                raise ProviderError("provider_protocol")
                            if generation and generation != body["id"]:
                                raise ProviderError("provider_protocol")
                            generation = body["id"]
                            self.partial_generation = generation
                        if body.get("usage"):
                            self.protocol_stage = "usage"
                            usage = body["usage"]
                            self.partial_usage = Usage.parse(usage)
                        self.protocol_stage = "choices"
                        choices = body.get("choices") or []
                        if not isinstance(choices, list) or len(choices) > 1:
                            raise ProviderError("provider_protocol")
                        if choices:
                            choice = choices[0]
                            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                                raise ProviderError("provider_protocol")
                            if choice.get("finish_reason") is not None and choice["finish_reason"] not in {
                                "stop",
                                "length",
                                "content_filter",
                                "tool_calls",
                                "function_call",
                                "error",
                            }:
                                raise ProviderError("provider_protocol")
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta") or {}
                            if not first_token and any(delta.get(field) for field in (
                                "content", "reasoning", "reasoning_content", "reasoning_details", "tool_calls"
                            )):
                                self._stream_first_token()
                                first_token = True
                            self.protocol_stage = "content"
                            addition = delta.get("content") or ""
                            if not isinstance(addition, str):
                                raise ProviderError("provider_protocol")
                            text += addition
                            if self.stream_visible and addition and time.monotonic() - last_flush >= 0.2:
                                self.protocol_stage = "text_encoding"
                                try:
                                    text.encode("utf8")
                                except UnicodeError:
                                    pass  # Consume the usage receipt, but never store invalid scalars.
                                else:
                                    if len(prefix) + len(text) > 2_000_000:
                                        raise ProviderError("output_limit")
                                    if not await self._stream_content_async(self._display_round(prefix, text), generation):
                                        raise ProviderError("execution_fenced")
                                    last_flush = time.monotonic()
                            self.protocol_stage = "reasoning"
                            try:
                                for field in ("reasoning", "reasoning_content"):
                                    if delta.get(field) is not None and not isinstance(delta[field], str):
                                        raise ReasoningFieldError(field, delta[field])
                                reasoning += delta.get("reasoning") or delta.get("reasoning_content") or ""
                                fragments = delta.get("reasoning_details")
                                if fragments is not None and not isinstance(fragments, list):
                                    raise ReasoningFieldError("reasoning_details", fragments)
                                merged = [details.add(fragment) for fragment in fragments or []]
                            except ReasoningFieldError as exc:
                                raise ProviderError("provider_protocol", diagnostics=protocol_diagnostics(
                                    "reasoning", "response_structure", total, exc.field, exc.value_type)) from None
                            for current_detail in merged:
                                summary = ReasoningDetails.summary(current_detail)
                                if (
                                    self.stream_visible
                                    and summary
                                    and summary != last_summary
                                    and time.monotonic() - last_summary_flush >= 1
                                ):
                                    await summarize(summary)
                            self.protocol_stage = "tool_calls"
                            if delta.get("tool_calls") and not payload.get("tools"):
                                raise ProviderError("unsupported_tool_call")
                            for call in delta.get("tool_calls") or []:
                                index = call.get("index")
                                if type(index) is not int or index < 0:
                                    raise ProviderError("provider_protocol")
                                current = tools.setdefault(
                                    index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                                )
                                current["id"] = call.get("id") or current["id"]
                                function = call.get("function") or {}
                                for field in ("name", "arguments"):
                                    current["function"][field] += function.get(field) or ""
            if not done:
                raise ProviderError("provider_incomplete")
        message = {"role": "assistant", "content": text or None}
        if self.stream_visible:
            for detail in details.items:
                summary = ReasoningDetails.summary(detail)
                if summary and summary != last_summary:
                    await summarize(summary)
        if self.stream_visible and text:
            self.protocol_stage = "text_encoding"
            text.encode("utf8")
            await self._stream_content_async(self._display_round(prefix, text), generation)
        if reasoning:
            message["reasoning"] = reasoning
        if details.items:
            message["reasoning_details"] = details.items
        if tools:
            self.protocol_stage = "tool_validation"
            message["tool_calls"] = [tools[index] for index in sorted(tools)]
            if any(not call["id"] or not call["function"]["name"] for call in message["tool_calls"]):
                raise ProviderError("provider_protocol")
            for call in message["tool_calls"]:
                if (
                    not isinstance(call["id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", call["id"])
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call["function"]["name"])
                    or len(call["function"]["arguments"]) > 65536
                ):
                    raise ProviderError("provider_protocol")
        return {
            "id": generation,
            "model": payload["model"],
            "usage": usage,
            "choices": [{"message": message, "finish_reason": finish or ("tool_calls" if tools else "stop")}],
        }

    def aggregate(self):
        if any(not error["no_charge"] for error in self.errors):
            return None
        if not self.calls:
            return None
        return Usage(
            sum(item.cost_microusd for item in self.calls),
            sum(item.prompt_tokens for item in self.calls),
            sum(item.completion_tokens for item in self.calls),
        )
