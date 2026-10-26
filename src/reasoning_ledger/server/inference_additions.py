"""Topic messages added to a running model reply.

A reply's context is captured when it is requested. A later message of the same
topic can join a reply that is still queued or running: the reply reads it before
its next provider call, appended to its conversation, and never sees it otherwise.
Callers authorize the principal and topic inside the same transaction.
"""

from __future__ import annotations

import json
import time

from .store import StoreError

ACTIVE = ("queued", "running")


def thread_active(db, org: str, root: str) -> bool:
    """A reply is running while any run of its agent thread is queued or running, or delegated work awaits its handoff."""
    if db.execute(
        "SELECT 1 FROM llm_runs r LEFT JOIN research_contexts c ON c.request_id=r.request_id WHERE r.organization_id=? "
        "AND (r.id=? OR json_extract(c.snapshot_json,'$.agent_root_run')=?) AND r.status IN ('queued','running') "
        "AND r.cancel_requested=0 LIMIT 1",
        (org, root, root),
    ).fetchone():
        return True
    return bool(db.execute(
        "SELECT 1 FROM topic_agent_tasks t JOIN topic_agent_threads h ON h.root_run=t.root_run AND h.status='active' "
        "LEFT JOIN topic_agent_handoffs x ON x.parent_run=t.parent_run WHERE t.root_run=? AND x.parent_run IS NULL LIMIT 1",
        (root,),
    ).fetchone())


def target(db, org: str, topic: str, run_id: str):
    """The reply a message can join: the thread root of ``run_id``, still running, answering a topic message."""
    from .topic_agents import TopicAgents

    run = db.execute("SELECT * FROM llm_runs WHERE organization_id=? AND id=?", (org, run_id)).fetchone()
    if run is None:
        raise StoreError(404, "model reply not found in this topic")
    root = TopicAgents.root(db, run)
    run = db.execute(
        "SELECT r.id,r.model,q.topic_id,q.context_through_sequence FROM llm_runs r "
        "JOIN llm_requests q ON q.organization_id=r.organization_id AND q.id=r.request_id WHERE r.organization_id=? AND r.id=?",
        (org, root),
    ).fetchone()
    if run is None or run["topic_id"] != topic:
        raise StoreError(404, "model reply not found in this topic")
    if run["context_through_sequence"] <= 0:
        raise StoreError(409, "only a reply to a topic message accepts additions")
    if not thread_active(db, org, root):
        raise StoreError(409, "the model reply has finished; send this as a new question")
    return run


def link(db, inference, org: str, topic: str, run_id: str, message, user: str, *, moved: bool = False) -> None:
    """Add ``message`` to the reply; a moved message's own queued reply is cancelled before it starts."""
    run = target(db, org, topic, run_id)
    if message["sender_kind"] != "human" or message["author_id"] != user:
        raise StoreError(403, "only your own messages can be added to a reply")
    if message["sequence"] <= run["context_through_sequence"]:
        raise StoreError(409, "only a message posted after the question can be added to its reply")
    if db.execute("SELECT 1 FROM inference_additions WHERE organization_id=? AND message_id=?", (org, message["id"])).fetchone():
        raise StoreError(409, "this message was already added to a reply")
    cancelled = []
    if moved:
        own = db.execute(
            "SELECT r.*,q.topic_id,q.requested_by FROM llm_runs r JOIN llm_requests q ON q.organization_id=r.organization_id "
            "AND q.id=r.request_id WHERE q.organization_id=? AND q.topic_id=? AND q.context_through_sequence=? AND q.requested_by=?",
            (org, topic, message["sequence"], user),
        ).fetchall()
        if any(row["status"] != "queued" for row in own):
            raise StoreError(409, "this message's own reply has already started; stop it before moving the message")
        for row in own:
            inference._request_cancel(db, org, dict(row))
            db.execute("UPDATE llm_runs SET error_code='moved_into_reply' WHERE id=?", (row["id"],))
            db.execute("UPDATE topic_agent_threads SET status='cancelled' WHERE root_run=?", (row["id"],))
            cancelled.append(row["id"])
    db.execute(
        "INSERT INTO inference_additions(organization_id,message_id,run_id,added_by,moved,cancelled_runs_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (org, message["id"], run["id"], user, int(moved), json.dumps(cancelled), int(time.time())),
    )
    inference._event(db, org, run["id"], {"type": "addition", "message_id": message["id"], "moved": moved})


def unlink(db, inference, org: str, run_id: str, message_id: str, user: str) -> dict:
    """Undo an addition the reply has not read yet; returns what it had cancelled."""
    from .topic_agents import TopicAgents

    run = db.execute("SELECT * FROM llm_runs WHERE organization_id=? AND id=?", (org, run_id)).fetchone()
    root = TopicAgents.root(db, run) if run is not None else run_id
    row = db.execute(
        "SELECT * FROM inference_additions WHERE organization_id=? AND message_id=? AND run_id=?", (org, message_id, root)
    ).fetchone()
    if row is None:
        raise StoreError(404, "addition not found")
    if row["added_by"] != user:
        raise StoreError(403, "only the person who added a message can undo it")
    if row["read_at"] is not None:
        raise StoreError(409, "the reply has already read this message")
    db.execute("DELETE FROM inference_additions WHERE organization_id=? AND message_id=?", (org, message_id))
    inference._event(db, org, root, {"type": "addition_removed", "message_id": message_id})
    return {"message_id": message_id, "moved": bool(row["moved"]), "cancelled_runs": json.loads(row["cancelled_runs_json"])}


def take(db, inference, run, step: int) -> list[dict]:
    """Mark the thread's unread additions read at ``step`` of coordinator ``run``; return them as source records, oldest first.

    Delegated runs never read additions: their coordinator does, at its next step.
    """
    from .research_context import source_message
    from .topic_agents import TopicAgents

    if TopicAgents.metadata(db, run).get("agent_task_id"):
        return []
    org, root = run["organization_id"], TopicAgents.root(db, run)
    rows = db.execute(
        "SELECT m.*,u.name FROM inference_additions a JOIN messages m ON m.organization_id=a.organization_id AND m.id=a.message_id "
        "JOIN users u ON u.id=m.author_id WHERE a.organization_id=? AND a.run_id=? AND a.read_at IS NULL ORDER BY m.sequence",
        (org, root),
    ).fetchall()
    if not rows:
        return []
    now = int(time.time())
    for row in rows:
        db.execute("UPDATE inference_additions SET read_at=?,read_step=? WHERE organization_id=? AND message_id=?",
                   (now, step, org, row["id"]))
    inference._event(db, org, run["id"], {"type": "agent", "phase": "additions_read", "step": step,
                                           "message_ids": [row["id"] for row in rows]})
    return [source_message(row) for row in rows]


def included(db, inference, org: str, root: str, through: int) -> None:
    """A continuation that recompiled the topic through ``through`` already carries those additions as source messages."""
    rows = db.execute(
        "SELECT a.message_id FROM inference_additions a JOIN messages m ON m.organization_id=a.organization_id AND m.id=a.message_id "
        "WHERE a.organization_id=? AND a.run_id=? AND a.read_at IS NULL AND m.sequence<=?",
        (org, root, through),
    ).fetchall()
    for row in rows:
        db.execute("UPDATE inference_additions SET read_at=?,read_step=0 WHERE organization_id=? AND message_id=?",
                   (int(time.time()), org, row[0]))
    if rows:
        inference._event(db, org, root, {"type": "agent", "phase": "additions_read", "step": 0,
                                          "message_ids": [row[0] for row in rows]})


def prompt(records: list[dict]) -> dict:
    """The user message that carries additions into the reply's conversation."""
    from .research_context import encoded

    return {"role": "user", "content": "Source messages added to this request while you were working. Take them into account "
            "in the reply you are writing; they are JSON records for reference:\n" + encoded(records)}


def view(row) -> dict:
    return {
        "message_id": row["message_id"], "run_id": row["run_id"], "added_by": row["added_by"], "moved": bool(row["moved"]),
        "cancelled_runs": json.loads(row["cancelled_runs_json"]), "created_at": row["created_at"],
        "read_at": row["read_at"], "read_step": row["read_step"],
    }


def for_run(db, org: str, run_id: str) -> list[dict]:
    return [view(row) for row in db.execute(
        "SELECT * FROM inference_additions WHERE organization_id=? AND run_id=? ORDER BY created_at,message_id", (org, run_id))]


def for_message(db, org: str, message_id: str) -> dict | None:
    row = db.execute("SELECT * FROM inference_additions WHERE organization_id=? AND message_id=?", (org, message_id)).fetchone()
    return view(row) if row else None
