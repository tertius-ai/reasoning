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
    # A turn that waited stays incomplete only until the server hands its work to the next turn, which owns the answer.
    handoff = waiting and status == "succeeded" and db.execute(
        "SELECT next_run FROM topic_agent_handoffs WHERE parent_run=?", (row["id"],)).fetchone()
    state = "continued" if handoff else "waiting" if waiting and status in {"running", "succeeded"} \
        else "working" if status in {"queued", "running"} else "response_ready" if status == "succeeded" else "partial"
    return {"status": state, "reason": waiting.get("reason") if state in {"waiting", "continued"} else error or row["error_code"],
            **({"next_run": handoff[0]} if handoff else {}),
            **(waits(db, row) if state == "waiting" else {}),
            "checkpoint": {key: checkpoint[key] for key in ("summary", "remaining", "sources") if key in checkpoint}
            if checkpoint else None}


PROGRAM_PREVIEW = 4000


def waits(db, row):
    """What a waiting turn still needs, so the reply can show it and offer the decisions it waits on."""
    from .compute_store import ComputeStore
    from .topic_agents import TopicAgents

    found = []
    for wait in db.execute("SELECT * FROM topic_agent_waits WHERE parent_run=? ORDER BY id", (row["id"],)).fetchall():
        view = TopicAgents._wait_view(db, wait)
        if not view["pending"]:
            continue
        item = {key: view[key] for key in ("kind", "id", "status")}
        if view["kind"] == "proposal":
            body = json.loads(db.execute("SELECT body_json FROM research_proposals WHERE id=?", (view["id"],)).fetchone()[0])
            item["action"] = body["action"]
            if body["action"] == "compute":
                parameters = body["parameters"]
                item["compute"] = {
                    "image_id": parameters["image_id"], "language": parameters["language"], "resources": parameters["resources"],
                    "program": parameters["program"][:PROGRAM_PREVIEW],
                    "program_truncated": len(parameters["program"]) > PROGRAM_PREVIEW,
                }
                item["runnable"] = ComputeStore.runnable(db, row["organization_id"], parameters["image_id"], parameters["resources"])
        elif view["kind"] == "compute":
            job = db.execute("SELECT created_at,parameters_json FROM compute_jobs WHERE id=?", (view["id"],)).fetchone()
            parameters = json.loads(job["parameters_json"])
            # Whether any worker or scaler would claim it: one that fits nothing waits forever.
            item["created_at"] = job["created_at"]
            item["runnable"] = ComputeStore.runnable(db, row["organization_id"], parameters["image_id"], parameters["resources"])
        found.append(item)
    delegated = db.execute(
        "SELECT COUNT(*) FROM topic_agent_tasks t LEFT JOIN llm_runs r ON r.id=t.child_run LEFT JOIN bot_runs b ON b.id=t.child_bot_run "
        "WHERE t.parent_run=? AND COALESCE(r.status,b.status) IN ('queued','running','waiting_approval')",
        (row["id"],),
    ).fetchone()[0]
    if delegated:
        found.append({"kind": "agents", "count": delegated})
    return {"waits": found, "compute_available": ComputeStore.available(db, row["organization_id"])}


def retained_answer(db, row, status, error):
    state = answer_state(db, row, status=status, error=error)
    checkpoint = state["checkpoint"]
    text = unwrapped_reply(row["output_text"])
    if not text.strip() and not checkpoint:
        from .research_artifacts import reply_files

        topic = db.execute("SELECT topic_id FROM llm_requests WHERE organization_id=? AND id=?",
                           (row["organization_id"], row["request_id"])).fetchone()
        files = reply_files(db, {"organization_id": row["organization_id"], "topic_id": topic[0],
                                "inference_run_id": row["id"]}) if topic else []
        if not files:
            return ""
        text = "Saved project files:\n\n" + "\n".join(f"- [{file['path']}]({file['download_url']})" for file in files)
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
        from .openrouter import safe_diagnostics
        from .usage_store import UsageStore

        diagnostic = safe_diagnostics(UsageStore.diagnostics(db, row["id"]))
        if diagnostic:
            text += "\n\n" + diagnostic["message"] + " " + diagnostic["action"]
    return text
