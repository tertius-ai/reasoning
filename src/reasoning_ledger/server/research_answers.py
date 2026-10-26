"""User-facing answer state is separate from executor success and paid accounting."""

import json

from .research_context import unwrapped_reply


def answer_state(db, row, *, status=None, error=None):
    latest = {}
    for phase in ("answer_checkpoint", "waiting"):
        event = db.execute(
            "SELECT event_json FROM llm_events WHERE organization_id=? AND run_id=? "
            "AND json_extract(event_json,'$.phase')=? ORDER BY sequence DESC LIMIT 1",
            (row["organization_id"], row["id"], phase),
        ).fetchone()
        latest[phase] = json.loads(event[0]) if event else None
    waiting, checkpoint = latest["waiting"], latest["answer_checkpoint"]
    status = status or row["status"]
    state = "waiting" if waiting and status in {"running", "succeeded"} else "working" if status in {"queued", "running"} \
        else "response_ready" if status == "succeeded" else "partial"
    return {"status": state, "reason": waiting.get("reason") if state == "waiting" else error or row["error_code"],
            "checkpoint": {key: checkpoint[key] for key in ("summary", "remaining", "sources") if key in checkpoint}
            if checkpoint else None}


def retained_answer(db, row, status, error):
    state = answer_state(db, row, status=status, error=error)
    checkpoint = state["checkpoint"]
    text = unwrapped_reply(row["output_text"])
    if not text.strip() and not checkpoint:
        return ""
    if checkpoint and (not text.strip() or status != "succeeded" or state["status"] == "waiting"):
        text = (text + "\n\nLatest answer checkpoint:\n" if text.strip() else "") + checkpoint["summary"]
        if checkpoint.get("remaining"):
            text += "\n\nRemaining: " + checkpoint["remaining"]
        if checkpoint.get("sources"):
            text += "\n\nSources: " + "; ".join(checkpoint["sources"])
    if any(json.loads(event[0]).get("phase") in {"compaction_failed", "compaction_skipped"} for event in db.execute(
            "SELECT event_json FROM llm_events WHERE organization_id=? AND run_id=? AND event_json LIKE ?",
            (row["organization_id"], row["id"], '%"compaction_%'))):
        text += "\n\n*Earlier messages in this topic couldn't be summarized for this reply, so it drew only on recent " \
                "messages and history lookups.*"
    if state["status"] == "waiting":
        text += "\n\n*Waiting: " + str(state["reason"]) + ". This is a checkpoint, not a completed answer.*"
    elif status != "succeeded":
        text += f"\n\n*Partial response: {status} ({error or status}). Not a completed answer. " \
                "Continue from retained evidence after reviewing this run; do not replay uncertain paid calls.*"
    return text
