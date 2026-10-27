"""Shared native workbench execution adapted to durable beta research topics."""

from __future__ import annotations

import hashlib
import json
import time

from ..agents.contracts import AgentEventKind, AgentRunSpec, PreparedAgentRun, PromptBinding, RunBudget, ToolBinding
from ..agents.native import ReasoningNativeExecutor
from ..agents.research_memory import memory_from_reply
from .research_context import COMPACTION_PROMPT, ResearchContext, encoded, token_bound
from .research_goals import ResearchGoals
from .research_provider import ResearchCompletion
from .research_tools import ResearchTools
from .store import StoreError


class TurnSuspended(BaseException):
    """Deterministic scheduler handoff, bypassing provider retry/error handlers."""


# A batch whose summary was rejected is not summarized again within this many seconds; turns answer without it.
COMPACTION_RETRY_SECONDS = 6 * 3600
# Output room for one batch's summary. Reasoning tokens count against it, so summaries also ask for the lowest effort.
COMPACTION_MAX_TOKENS = 16_384


def execution_error(result, finish_reason):
    """Classify trusted harness failures without persisting their raw text."""
    if finish_reason == "length" or result.metadata.get("output_budget_exhausted"):
        return "output_token_limit"
    if finish_reason == "content_filter":
        return "agent_content_filtered"
    error = result.error or ""
    if error.startswith((
        "OpenRouter returned no visible assistant content", "OpenRouter exhausted its output limit before producing visible"
    )):
        return "agent_empty_response"
    if error.startswith(("model returned invalid arguments", "model returned a tool call without a name")):
        return "agent_invalid_tool_arguments"
    if error.startswith("model requested a tool during its tool-budget final turn"):
        return "agent_tool_limit"
    if finish_reason in {"error", "function_call", "tool_calls"}:
        return "agent_provider_finish"
    return "agent_incomplete"


class ResearchAgent:
    def __init__(self, inference, provider, row, quote, *, compute=None, workflows=None, stopped=None, timeout=1800, spans=None):
        self.inference, self.store, self.row, self.quote = inference, inference.store, row, quote
        self.spans = spans
        self.context = ResearchContext(self.store)
        self.client = ResearchCompletion(inference, provider, row, quote)
        self.compute, self.workflows = compute, workflows
        self.cancelled = False
        self.stop_requested = stopped or (lambda: False)
        self.timeout, self.deadline = timeout, time.monotonic() + timeout
        self.client.cancelled = self._stopped

    def _compact(self, messages, metadata):
        """Summarize the messages before the recent tail, batch by batch, and recompile the context.

        Returns the context, its snapshot and, when a batch's summary was rejected, that batch's range. Earlier batches
        keep their saved memory; the rejected batch and later ones stay out of the context.
        """
        through = metadata["compact_through_sequence"]
        with self.store.transaction() as db:
            old = self.context.memory(db, self.row["organization_id"], self.row["topic_id"], metadata["through_sequence"],
                                      metadata.get("message_edit_sequence", 0))
        memory = json.loads(old["body_json"]) if old else None
        after = old["through_sequence"] if old else 0
        failed = None
        batch_limit = max(1024, min(24_000, self.quote.context_length // 4))
        for batch in self.context.batches(self.row["organization_id"], self.row["topic_id"], after, through, batch_limit,
                                         edit_cutoff=metadata.get("message_edit_sequence", 0)):
            self._event("compacting", {"through_sequence": batch[-1]["sequence"], "source_messages": len(batch)})
            compact_messages = [
                {"role": "system", "content": COMPACTION_PROMPT},
                {"role": "user", "content": encoded({"previous_memory": memory, "source_messages": batch})},
            ]
            remaining = self._remaining_output()
            payload = {
                "model": self.row["model"],
                "messages": compact_messages,
                "max_tokens": min(self.quote.max_completion_tokens, COMPACTION_MAX_TOKENS, remaining),
            }
            # A summary needs no deliberation. Models that default to long reasoning (kimi-k3 defaults to max) otherwise
            # spend the whole allowance before writing it. Only an advertised effort is sent: routes require parameters.
            if self.quote.reasoning_efforts:
                payload["reasoning"] = {"effort": self.quote.reasoning_efforts[0]}
            result = self.client.complete(payload, timeout=min(1800, max(1, self.deadline - time.monotonic())))
            # Mark purpose on the ledger before submission, not after its completion.
            sources = {message["id"] for message in batch}
            if memory:
                sources |= {source for items in memory.values() for item in items for source in item["sources"]}
            raw = result["choices"][0]["message"].get("content") or ""
            truncated = result["choices"][0].get("finish_reason") == "length"
            try:
                if truncated:
                    raise ValueError("compaction reached its output limit")
                memory = memory_from_reply(raw, sources)
            except (ValueError, TypeError):
                failed = {"after": after, "through": batch[-1]["sequence"]}
                self._event("compaction_failed", {**failed, "reason": "output_token_limit" if truncated else "invalid_summary",
                    "summary": "The summary of earlier messages hit its output limit; answering from recent ones" if truncated
                    else "Couldn't summarize earlier messages; answering from recent ones"})
                break
            self.context.save_memory(self.row, batch[-1]["sequence"], memory, sorted(sources))
            after = batch[-1]["sequence"]
        with self.store.transaction() as db:
            compiled, snapshot = self.context.compile(
                db,
                self.row["organization_id"],
                self.row["topic_id"],
                metadata["through_sequence"],
                [self.quote],
                user=self.row["requested_by"],
                edit_cutoff=metadata.get("message_edit_sequence", 0),
                prompt_override={
                    "system_prompt": metadata["system_prompt"],
                    "prompt_version": metadata["prompt_version"],
                    "prompt_profile": metadata.get("prompt_profile", "workbench"),
                },
            )
        return compiled, snapshot, failed

    def _compaction_failed_recently(self, after):
        """Whether a turn of this topic rejected the summary of the batch starting after `after` within the retry window."""
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT e.event_json FROM llm_events e JOIN llm_runs r ON r.organization_id=e.organization_id AND r.id=e.run_id "
                "JOIN llm_requests q ON q.id=r.request_id "
                "WHERE r.organization_id=? AND q.topic_id=? AND e.created_at>=? AND e.event_json LIKE ? "
                "ORDER BY e.sequence DESC LIMIT 20",
                (self.row["organization_id"], self.row["topic_id"], int(time.time()) - COMPACTION_RETRY_SECONDS,
                 '%"compaction_failed"%'),
            ).fetchall()
        return any(json.loads(row[0]).get("phase") == "compaction_failed" and json.loads(row[0]).get("after") == after
                   for row in rows)

    def _remaining_output(self):
        maximum = json.loads(self.row["parameters_json"])["max_completion_tokens"]
        return maximum or self.quote.max_completion_tokens

    def _event(self, kind, body):
        with self.store.transaction(write=True) as db:
            self.inference._event(db, self.row["organization_id"], self.row["id"], {"type": "agent", "phase": kind, **body})

    def _transform(self, payload):
        from .inference_additions import prompt
        from .review_verdict import submissions
        from .topic_agents import TopicAgents

        with self.store.transaction() as db:
            pending_agents = TopicAgents.pending(db, self.row["id"])
            goal = ResearchGoals.execution_goal(db, self.row["id"])
            pending_review = db.execute("SELECT 1 FROM mathematical_reviews WHERE source_run_id=? AND status='pending'",
                                        (self.row["id"],)).fetchone()
            submitted_review = submissions(db, self.row["id"])
            checkpoint = goal and db.execute("SELECT 1 FROM agent_goal_events WHERE goal_id=? "
                "AND json_extract(event_json,'$.type')='checkpoint' AND json_extract(event_json,'$.run_id')=?",
                (goal["id"], self.row["id"])).fetchone()
        if submitted_review:
            raise TurnSuspended("structured review submitted")
        if goal and (goal["status"] == "complete" or goal["tool_calls"] >= goal["max_tool_calls"]):
            payload = {key: value for key, value in payload.items() if key not in {"tools", "tool_choice"}}
        messages = payload["messages"]
        if pending_agents:
            raise TurnSuspended("delegated work")
        if not pending_review:
            # Messages added to this reply join the native conversation itself, so every later step keeps them.
            additions = self.inference.take_additions(self.row, len(self.client.responses) + 1)
            if additions:
                messages.append(prompt(additions))
        if pending_review and not pending_agents:
            checkpoint_tools = [tool for tool in payload.get("tools", []) if tool["function"]["name"] == "goal_checkpoint"]
            if goal and goal["status"] == "active" and not checkpoint and checkpoint_tools:
                payload = {**payload, "tools": checkpoint_tools,
                           "tool_choice": {"type": "function", "function": {"name": "goal_checkpoint"}}}
                guidance = "A revision-pinned review is queued. Call goal_checkpoint with status active and the exact " \
                           "pending review/record IDs. The server needs this turn to end before starting its fresh-context reviewer."
            else:
                raise TurnSuspended("independent review")
            messages = [*messages, {"role": "system", "content": guidance}]
            payload = {**payload, "messages": messages}
        # Tool results have persisted receipts. Trim only completed tool-result bodies,
        # preserving the native call/response protocol and source locators.
        if token_bound(payload) > self.quote.context_length * 3 // 5:
            condensed = []
            for message in messages:
                if message.get("role") == "tool" and len(message.get("content", "")) > 2000:
                    try:
                        result = json.loads(message["content"])
                        receipt = (result.get("result") or {}).get("receipt_id")
                    except (ValueError, AttributeError):
                        receipt = None
                    condensed.append(
                        {**message, "content": encoded({"receipt_id": receipt, "preview": message["content"][:1600], "compressed": True})}
                    )
                else:
                    condensed.append(message)
            payload = {**payload, "messages": condensed}
        return payload

    def execute(self):
        from .inference_timing import Spans

        # Worker spans run from the claim handoff to the first provider request.
        spans = self.spans or Spans("worker")
        if self.spans is not None:
            spans.mark("dispatch")
        with self.store.transaction() as db:
            snapshot_row = db.execute(
                "SELECT snapshot_json FROM research_contexts WHERE request_id=?", (self.row["request_id"],)
            ).fetchone()
        metadata = json.loads(snapshot_row[0]) if snapshot_row else None
        messages = json.loads(self.row["context_json"])
        spans.mark("context_read")
        try:
            # Permission changes between enqueue and claim fail this run, not the worker.
            self.tools = ResearchTools(self.inference, self.row, compute=self.compute, workflows=self.workflows)
            spans.mark("tool_permissions")
            compacted = bool(metadata and metadata["compact_through_sequence"] > metadata["memory_through_sequence"])
            missing = None
            if compacted and self._compaction_failed_recently(metadata["memory_through_sequence"]):
                compacted, missing = False, metadata["compact_through_sequence"]
                self._event("compaction_skipped", {"after": metadata["memory_through_sequence"], "through": missing,
                    "summary": "Earlier messages stay unsummarized after a recent failure; answering from recent ones"})
            if compacted:
                # Completion purpose is pinned for the summarizer's individual call receipt.
                previous = self.client.complete
                self.client.complete = lambda payload, timeout: self._compaction_complete(payload, timeout)
                try:
                    messages, metadata, failed = self._compact(messages, metadata)
                finally:
                    self.client.complete = previous
                if failed:
                    missing = metadata["compact_through_sequence"]
                # Only revisions actually delivered by the refreshed context count as read.
                from ..service import RevisionObservations

                self.tools.observations = RevisionObservations()
                for record in metadata.get("ledger_revisions", []):
                    self.tools.observations.note(record["claim_id"], record["revision_id"])
                spans.mark("compaction")
                # Only compaction changes the captured snapshot; an unchanged one is not rewritten.
                with self.store.transaction(write=True) as db:
                    db.execute(
                        "UPDATE research_contexts SET snapshot_json=? WHERE request_id=?", (encoded(metadata), self.row["request_id"])
                    )
                spans.mark("snapshot_write")
            if missing is not None:
                # Say what the context omits, so the model recovers those messages instead of assuming they are absent.
                messages = [*messages[:-1], {"role": "system", "content": f"Messages up to sequence {missing} could not be "
                            "summarized and are not in this context. Recover any that matter with the history tools."}, messages[-1]]
            available = self.tools.available() if self.quote.supports_tools and metadata is not None \
                and not metadata.get("workflow_context_sha256") else []
            prepared_tools = self.tools.registry.prepared(available)
            from .research_capabilities import runtime_capabilities, runtime_guidance

            capabilities = runtime_capabilities(self.quote, metadata, self.tools)
            self._event("capabilities", capabilities)
            spans.mark("tool_preparation")
            with self.store.transaction() as db:
                goal = ResearchGoals.execution_goal(db, self.row["id"])
            spans.mark("goal_lookup")
            tool_limit = max(0, goal["max_tool_calls"] - goal["tool_calls"]) if goal else None
            if metadata:
                self.client.prompt_version = metadata.get("prompt_version", self.client.prompt_version)
            spec = AgentRunSpec(
                executor_id="reasoning-native",
                model=self.row["model"],
                effort=json.loads(self.row["parameters_json"]).get("effort"),
                system_prompt=PromptBinding("organization-mathematics", self.client.prompt_version),
                user_prompt=PromptBinding("topic-context", self.row["request_id"]),
                tools=tuple(ToolBinding(tool.name) for tool in prepared_tools),
                budget=RunBudget(max_output_tokens=None, max_tool_calls=tool_limit, timeout_seconds=self.timeout),
            )
            user_prompt = messages[-1]["content"]
            system_prompt = (metadata or {}).get("system_prompt") or messages[0]["content"]
            prepared = PreparedAgentRun(
                self.row["id"],
                spec,
                system_prompt,
                user_prompt,
                {"system": hashlib.sha256(system_prompt.encode()).hexdigest()},
                context_messages=({"role": "system", "content": runtime_guidance(capabilities)}, *messages[1:-1]),
                tools=prepared_tools,
            )
            self.client.context_transform = self._transform
            native = ReasoningNativeExecutor(
                self.client, tool_invoker=self.tools.invoke if available else None,
                rate_limit_retry_delays=(), max_output_continuations=None,
                continue_reasoning_only=True,
            )
            spans.mark("executor_setup")
            # The first provider request marks executor_start and retains these spans.
            self.client.worker_spans = spans

            tool_started = {}

            def emit(event):
                if event.kind in {AgentEventKind.TOOL_REQUESTED, AgentEventKind.TOOL_COMPLETED}:
                    identity = event.data.get("tool_call_id")
                    data = event.data
                    if event.kind == AgentEventKind.TOOL_REQUESTED:
                        tool_started[identity] = time.monotonic()
                    elif identity in tool_started:
                        data = {**data, "elapsed_ms": max(0, int((time.monotonic() - tool_started.pop(identity)) * 1000))}
                    self._event("tool_requested" if event.kind == AgentEventKind.TOOL_REQUESTED else "tool_completed", data)
                elif event.kind == AgentEventKind.MESSAGE_DELTA:
                    # The provider stream owns cumulative visible output, including
                    # progress emitted alongside tool calls. Native deltas describe
                    # only its final answer and must not replace that durable prefix.
                    self._event("answer_ready", {})

            result = native.execute(prepared, emit, cancelled=self._stopped)
            # A transport/admission refusal can finish before native asks its
            # cancellation callback again. Recheck authority before classifying
            # a revoked request as an agent failure or successful completion.
            self._stopped()
            usage = self.client.aggregate()
            unknown = any(not error["no_charge"] for error in self.client.errors)
            finish_reason = self.client.responses[-1]["choices"][0].get("finish_reason") if self.client.responses else None
            completed = result.status == "completed" and finish_reason in {None, "stop"} and not self.cancelled
            failure = None if completed else self.client.failure_code or execution_error(result, finish_reason)
            if failure:
                self._event("failed", {
                    "reason": failure, "finish_reason": finish_reason, "native_status": result.status,
                    "output_continuations": result.metadata.get("output_continuations", 0),
                    "tool_budget_exhausted": bool(result.metadata.get("tool_budget_exhausted")),
                })
            cancelled_status = "interrupted" if self.stop_requested() else "cancelled"
            self.inference.finish(
                self.row,
                "succeeded" if completed else cancelled_status if self.cancelled else "failed",
                usage=usage,
                no_charge=self.client.no_charge,
                error=self._cancel_reason()
                if self.cancelled
                else failure,
                accounting_unknown=unknown,
            )
            if not completed:
                with self.store.transaction(write=True) as db:
                    db.execute(
                        "UPDATE agent_goals SET status='blocked',reason=?,updated_at=? WHERE run_id=? AND status='complete'",
                        ("The final response failed; review this turn before resuming.", int(time.time()), self.row["id"]),
                    )
        except TurnSuspended as suspension:
            self._event("waiting", {"reason": str(suspension)})
            self.inference.finish(self.row, "succeeded", usage=self.client.aggregate(), no_charge=self.client.no_charge,
                                  accounting_unknown=any(not error["no_charge"] for error in self.client.errors))
        except Exception as exc:
            usage = self.client.aggregate()
            self.inference.finish(
                self.row,
                "interrupted" if self.stop_requested() else "cancelled" if self._stopped() else "failed",
                usage=usage,
                no_charge=self.client.no_charge,
                error=self._cancel_reason() if self.cancelled else self.client.failure_code or "agent_execution_error",
                accounting_unknown=any(not error["no_charge"] for error in self.client.errors),
            )
            self._event("failed", {"reason": self.client.failure_code or "agent_execution_error",
                "exception_type": type(exc).__name__
                if isinstance(exc, (ValueError, TypeError, KeyError, StoreError)) else "InternalError"})
        finally:
            from .research_reviews import ResearchReviews

            ResearchReviews(self.inference).finish(self.row)

    def _compaction_complete(self, payload, timeout):
        import asyncio

        return asyncio.run(self.client.submit_with_credit_retry(payload, timeout=timeout, purpose="compaction"))

    def _stopped(self):
        self.cancelled = bool(self.stop_requested() or time.monotonic() >= self.deadline or self.inference.stopped(self.row))
        return self.cancelled

    def _cancel_reason(self):
        if self.stop_requested():
            return "worker_interrupted"
        if time.monotonic() >= self.deadline:
            return "execution_timeout"
        return self.inference.stopped(self.row) or "execution_cancelled"
