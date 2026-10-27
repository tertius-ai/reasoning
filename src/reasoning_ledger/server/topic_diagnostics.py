"""Concise diagnostic views and reversible, content-addressed full traces."""

import hashlib
import json
from collections import Counter
from copy import deepcopy


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def pack_trace(value):
    """Deduplicate repeated large values without truncation or loss of JSON types."""
    counts = Counter()

    def census(item):
        if isinstance(item, (dict, list, str)):
            body = canonical(item)
            if len(body) >= 512:
                counts[body] += 1
        if isinstance(item, dict):
            for child in item.values():
                census(child)
        elif isinstance(item, list):
            for child in item:
                census(child)

    census(value)
    blobs = {}

    def children(item):
        if isinstance(item, dict):
            encoded = {key: pack(child) for key, child in item.items()}
            # Only reserved-key dictionaries need escaping; ordinary JSON stays directly readable.
            return {"$object": list(encoded.items())} if "$ref" in item or "$object" in item else encoded
        if isinstance(item, list):
            return [pack(child) for child in item]
        return item

    def pack(item):
        if isinstance(item, (dict, list, str)):
            body = canonical(item)
            if len(body) >= 512 and counts[body] > 1:
                identity = hashlib.sha256(body.encode()).hexdigest()
                if identity not in blobs:
                    blobs[identity] = children(item)
                return {"$ref": identity}
        return children(item)

    data = pack(value)
    return {"schema": "reasoning.topic-trace.v2", "encoding": "sha256-json-values-v1", "data": data, "blobs": blobs,
            "decode": "Resolve $ref through blobs recursively; decode $object entry arrays to dictionaries; otherwise recurse normally. "
            "All retained values are preserved."}


def summarize_topic(value):
    value = deepcopy(value)
    summary = {key: item for key, item in value.items() if key not in {"runs", "captured_contexts", "file_versions"}}
    summary["schema"] = "reasoning.topic-summary.v2"
    summary["scope"] = "Complete conversation and concise run/tool/accounting diagnostics. Full trace is available with detail=full; " \
        "raw database diagnostics with detail=raw. Repeated contexts, tool schemas, session snapshots, " \
        "and historical file bodies are omitted here. Provider exchanges contain request/response summaries, not full bodies."
    summary["runs"] = []
    for key in ("mathematical_records", "reviews", "agent_tasks", "bot_agent_tasks", "research_settings", "files", "proposals"):
        summary[key] = [preview_value(record) for record in value.get(key, [])]
    summary["bot_runs"] = [{**run, "tool_receipts": [
        {**receipt, "arguments": receipt_summary(receipt.get("arguments")), "result": receipt_summary(receipt.get("result"))}
        for receipt in run.get("tool_receipts", [])]}
                           for run in value.get("bot_runs", [])]
    for run in summary["bot_runs"]:
        if run["status"] == "succeeded":
            run.pop("output_text", None)  # Complete bot answers are retained once in the conversation.
    for original in value["runs"]:
        run = {key: item for key, item in original.items() if key not in {"events", "latest_session"}}
        exchanges = {}
        failures = []
        for record in original["events"]:
            event = dict(record["event"])
            if "request" in event:
                request = event["request"]
                call = exchanges.setdefault(event.get("call_id", str(record["sequence"])), {})
                call["request"] = {key: item for key, item in request.items() if key not in {"messages", "tools"}}
                call["request"].update(message_count=len(request.get("messages", [])), tool_count=len(request.get("tools", [])))
            if "response" in event:
                response = event["response"]
                call = exchanges.setdefault(event.get("call_id", str(record["sequence"])), {})
                call["response"] = {key: item for key, item in response.items() if key != "choices"}
                call["response"]["choices"] = []
                for choice in response.get("choices", []):
                    message = choice.get("message", {})
                    call["response"]["choices"].append({"finish_reason": choice.get("finish_reason"),
                        "content_characters": len(message.get("content") or ""),
                        "reasoning_characters": len(message.get("reasoning") or ""),
                        "tool_names": [tool.get("function", {}).get("name") for tool in message.get("tool_calls", [])],
                        "reasoning_present": bool(message.get("reasoning") or message.get("reasoning_details"))})
            if event.get("phase") == "failed":
                failures.append(record)
        run["failure_events"] = failures
        run["tool_receipts"] = [{**receipt, "arguments": preview_value(receipt.get("arguments")),
            "result": receipt_summary(receipt.get("result"))} for receipt in run.get("tool_receipts", [])]
        run["provider_exchanges"] = [{"call_id": identity, **exchange} for identity, exchange in exchanges.items()]
        if run.get("message_id"):
            run.pop("output_text", None)  # The complete answer is already in messages.
        summary["runs"].append(run)
    summary["provider_calls"] = [{key: item for key, item in call.items() if key != "receipts"}
                                 for call in value["provider_calls"]]
    summary["issues"] = [{"run_id": run["id"], "source": "bot" if run in summary["bot_runs"] else "openrouter",
                          "status": run["status"], "error_code": run.get("error_code")}
                         for run in [*summary["runs"], *summary["bot_runs"]]
                         if run["status"] in {"failed", "outcome_unknown", "interrupted"}]
    summary["contexts"] = [{key: item for key, item in context.items() if key != "snapshot"} | {
        "metadata": {key: item for key, item in context["snapshot"].items() if key in {
            "through_sequence", "input_token_upper_bound", "context_window", "prompt_version", "harness_version", "memory_revision",
            "tool_groups", "knowledge_collections", "policy",
        }}
    } for context in value["captured_contexts"]]
    assumptions = {}

    def compact(item):
        if isinstance(item, dict):
            if isinstance(item.get("assumptions"), dict):
                profile = item.pop("assumptions")
                identity = hashlib.sha256(canonical(profile).encode()).hexdigest()[:16]
                assumptions[identity] = profile
                item["assumptions_ref"] = identity
            for child in item.values():
                compact(child)
        elif isinstance(item, list):
            for child in item:
                compact(child)

    compact(summary)
    summary["energy_assumptions"] = assumptions
    summary["scope"] += (" Long tool arguments/results are previews with SHA-256 fingerprints; "
        "encoded media payloads are represented by their size and hash, without binary previews. "
        "recover complete receipts via the full trace. "
        "Energy assumptions are stored once in energy_assumptions and linked by assumptions_ref.")
    return summary


def receipt_summary(value):
    body = canonical(value)
    if len(body) <= 1600:
        return preview_value(value)
    important = {key: item for key, item in value.items() if key in {
        "error", "code", "receipt_id", "claim_id", "revision_id", "id", "status", "name", "source", "url",
    }} if isinstance(value, dict) else {}
    return {**preview_value(important), "characters": len(body), "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "preview": body[:240], "truncated": True}


def preview_value(value, field=None):
    if isinstance(value, str) and (
        isinstance(field, str) and field.endswith("_base64")
        or value.startswith("data:image/") and ";base64," in value
        or value.startswith("data:application/pdf;base64,")
    ):
        return {"encoded_characters": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest(),
                "omitted": "Binary payload; retrieve the authorized attachment or full trace."}
    if isinstance(value, str) and len(value) > 1000:
        return {"preview": value[:240], "characters": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest(), "truncated": True}
    if isinstance(value, dict):
        return {key: preview_value(child, key) for key, child in value.items()}
    if isinstance(value, list):
        return [preview_value(child) for child in value]
    return value
