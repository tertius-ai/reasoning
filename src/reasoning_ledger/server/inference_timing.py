"""Content-free attribution of local queue, provider, and research-tool waits.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import time
from time import perf_counter

# Closed span vocabulary. Each value is elapsed server time in milliseconds on
# the monotonic clock of the process that measured it, from the previous span
# in the same stage to the end of this one. Spans carry names and durations
# only: never prompts, context, credentials, tool arguments or answers.
SPANS = {
    # API request thread, after authentication, through the admission commit.
    "admission": {
        "admission_check": "topic write access, idempotent replay lookup, topic kind, organization key and positive budget",
        "catalog_quote": "provider model catalog fetch and quote construction",
        "transaction_acquire": "admission write transaction and organization credit lock",
        "access_check": "topic write access and idempotency recheck inside the admission transaction",
        "policy_check": "organization key/budget recheck, effort support and active/rate limits",
        "context_compile": "research context compilation, or workflow attachment context",
        "reservation": "quote reservation estimate and organization balance check",
        "ledger_write": "request, run, state-event and audit inserts",
    },
    # Inference worker, from claim through the first provider admission.
    "worker": {
        "dispatch": "claim handoff and execution-kind lookup",
        "context_read": "captured research snapshot read",
        "tool_permissions": "current tool grant and topic permission check",
        "compaction": "context compaction, including its own provider calls",
        "snapshot_write": "refreshed snapshot write after compaction",
        "tool_preparation": "available tool selection and schema preparation",
        "goal_lookup": "execution goal and tool-budget lookup",
        "executor_setup": "agent run specification and native executor construction",
        "executor_start": "native executor start until its first provider request",
    },
    # One provider call, before its ledger header records the call start.
    "provider": {
        "context_transform": "pending delegation, goal and review checks applied to the request",
        "purpose_lookup": "event-loop start and reviewer-run lookup that selects the call purpose",
        "credential_check": "execution fence, stop reasons and organization key decryption",
        "media_check": "captured attachment authorization",
        "quote_estimate": "priced-context encoding and reservation estimate",
        "reservation_lock": "reservation write transaction and organization credit lock",
        "goal_admission": "goal time and cost admission inside the reservation transaction",
        "reservation": "execution fence, stop reasons and balance check for the call reservation",
        "call_ledger_lock": "call-ledger write transaction",
        "call_ledger": "context fingerprint, project lookup and call header insert",
    },
    # One provider call, after its start and before the HTTP request is sent.
    "dispatch": {
        "request_record": "round event, retained request session and visible-output prefix, in one transaction",
        "client_setup": "HTTP client construction",
    },
}


class Spans:
    """Consecutive named durations for one stage; unmeasured gaps are excluded."""

    def __init__(self, stage: str):
        if stage not in SPANS:
            raise ValueError("undefined timing stage")
        self.stage, self.values, self.last = stage, {}, perf_counter()

    def mark(self, name: str) -> None:
        if name not in SPANS[self.stage]:
            raise ValueError("undefined timing span")
        now = perf_counter()
        self.values[name] = round(self.values.get(name, 0) + (now - self.last) * 1000, 2)
        self.last = now

    def restart(self) -> None:
        """Exclude time since the last mark that belongs to no defined span."""
        self.last = perf_counter()

    def record(self) -> dict:
        return dict(self.values)


def spans(stage: str, value) -> dict | None:
    """Read retained spans back through the closed vocabulary only."""
    if not isinstance(value, dict):
        return None
    result = {name: value[name] for name in SPANS[stage] if type(value.get(name)) in {int, float} and value[name] >= 0}
    return result or None


def run_spans(db, row, first_call) -> dict:
    """Admission and worker spans of the run; provider and dispatch spans of its first call."""
    stages = {"admission": None, "worker": None, "provider": None, "dispatch": None}
    for record in db.execute(
        "SELECT event_json FROM llm_events WHERE organization_id=? AND run_id=? "
        "AND json_extract(event_json,'$.type')='timing' ORDER BY sequence",
        (row["organization_id"], row["id"]),
    ).fetchall():
        body = json.loads(record[0])
        if body.get("stage") in {"admission", "worker"} and stages[body["stage"]] is None:
            stages[body["stage"]] = spans(body["stage"], body.get("spans_ms"))
    if first_call is not None:
        for record in db.execute(
            "SELECT kind,data_json FROM inference_call_events WHERE call_id=? AND kind IN ('submission','response_headers')",
            (first_call,),
        ).fetchall():
            body = json.loads(record[1])
            if record[0] == "submission":
                stages["provider"] = spans("provider", body.get("preparation_ms"))
            else:
                stages["dispatch"] = spans("dispatch", body.get("dispatch_ms"))
    return stages


def run_timing(db, row) -> dict:
    """Report only retained measurements; missing historical timing stays null."""
    state = db.execute(
        "SELECT MIN(CASE WHEN json_extract(event_json,'$.status')='queued' THEN "
        "CAST(json_extract(event_json,'$.recorded_at_ms') AS BIGINT) END), "
        "MIN(CASE WHEN json_extract(event_json,'$.status')='running' THEN "
        "CAST(json_extract(event_json,'$.recorded_at_ms') AS BIGINT) END), "
        "SUM(CASE WHEN json_extract(event_json,'$.phase')='tool_completed' THEN "
        "CAST(json_extract(event_json,'$.elapsed_ms') AS BIGINT) END) "
        "FROM llm_events WHERE organization_id=? AND run_id=?",
        (row["organization_id"], row["id"]),
    ).fetchone()
    queued, started, tools = state[0], state[1], state[2]
    calls = db.execute(
        "SELECT i.id,i.started_at_ms,e.data_json FROM inference_calls i JOIN inference_call_events e "
        "ON e.sequence=(SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id) "
        "WHERE i.organization_id=? AND i.run_id=? ORDER BY i.sequence",
        (row["organization_id"], row["id"]),
    ).fetchall()
    latest = json.loads(calls[-1]["data_json"]) if calls else {}
    active = row["status"] in {"queued", "running"}
    wait = "local_queue" if row["status"] == "queued" else "preparing_context" if active else None
    if calls and active:
        wait = "provider_stream" if latest.get("time_to_first_token_ms") is not None else "provider_wait"
        if latest.get("status") != "running":
            phase = db.execute(
                "SELECT json_extract(event_json,'$.phase') FROM llm_events WHERE organization_id=? AND run_id=? "
                "AND json_extract(event_json,'$.phase') IN ('tool_requested','tool_completed','provider_request','provider_response') "
                "ORDER BY sequence DESC LIMIT 1", (row["organization_id"], row["id"]),
            ).fetchone()
            wait = "tool_execution" if phase and phase[0] == "tool_requested" else "preparing_context"
    elapsed = latest.get("latency_ms")
    if calls and latest.get("status") == "running" and calls[-1]["started_at_ms"] is not None:
        elapsed = max(0, int(time.time() * 1000) - calls[-1]["started_at_ms"])
    return {
        "wait_phase": wait,
        "queue_wait_ms": max(0, started - queued) if queued is not None and started is not None else None,
        "preparation_ms": max(0, calls[0]["started_at_ms"] - started) if started is not None and calls
                          and calls[0]["started_at_ms"] is not None else None,
        "provider_elapsed_ms": elapsed,
        "time_to_response_headers_ms": latest.get("time_to_response_headers_ms"),
        "time_to_first_token_ms": latest.get("time_to_first_token_ms"),
        "tool_execution_ms": tools,
        "provider_calls": len(calls),
        "spans_ms": run_spans(db, row, calls[0]["id"] if calls else None),
    }
