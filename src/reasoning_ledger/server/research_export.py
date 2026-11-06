"""Authorized, complete topic diagnostic exports without operator secrets."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .compute_store import ComputeStore
from .research_context import HARNESS_VERSION
from .research_goals import RECOVERABLE_TURN_ERRORS
from .research_media import view as media_view
from .research_profiles import topic_instructions
from .side_chat import public_request
from .store import StoreError
from .topic_forks import feedback_export
from .usage_store import UsageStore


def decoded(row):
    value = dict(row)
    for key in tuple(value):
        if key.endswith("_json"):
            raw = value.pop(key)
            value[key[:-5]] = json.loads(raw) if raw is not None else None
    return value


def export_topic(inference, user, org, topic, *, research_only=True):
    store = inference.store
    with store.transaction() as db:
        role, project, _, channel, target = store._topic_access(db, user, org, topic)
        if research_only and target["kind"] != "beta":
            raise StoreError(400, "research diagnostics belong to beta topics")

        def rows(query, args=(org, topic)):
            return [decoded(row) for row in db.execute(query, args)]

        runs = []
        # Private side-chat replies belong to their owner's side chat, not to the topic's diagnostics.
        for row in db.execute(inference._joined() + "WHERE r.organization_id=? AND q.topic_id=? AND " + public_request("q.id")
                              + " ORDER BY q.created_at,r.id", (org, topic)):
            run = inference._view(row, db)
            run["events"] = rows(
                "SELECT sequence,created_at,event_json FROM llm_events WHERE organization_id=? AND run_id=? ORDER BY sequence",
                (org, row["id"]),
            )
            run["tool_receipts"] = rows(
                "SELECT * FROM research_tool_receipts WHERE organization_id=? AND run_id=? ORDER BY sequence", (org, row["id"])
            )
            run["latest_session"] = next(iter(rows("SELECT state_json FROM research_sessions WHERE run_id=?", (row["id"],))), None)
            runs.append(run)
        calls = []
        for row in db.execute(UsageStore._call_query() + " AND i.topic_id=? AND " + public_request("i.request_id")
                              + " ORDER BY i.sequence", (user, org, role, topic)):
            call = UsageStore._call_view(row)
            call["receipts"] = rows(
                "SELECT sequence,kind,recorded_at,data_json FROM inference_call_events "
                "WHERE organization_id=? AND call_id=? ORDER BY sequence",
                (org, call["id"]),
            )
            calls.append(call)
        goals = rows("SELECT * FROM agent_goals WHERE organization_id=? AND topic_id=? ORDER BY created_at,id")
        for goal in goals:
            goal["events"] = rows(
                "SELECT sequence,created_at,event_json FROM agent_goal_events WHERE goal_id=? ORDER BY sequence", (goal["id"],)
            )
        workflow_runs = rows("SELECT * FROM workflow_runs WHERE organization_id=? AND topic_id=? ORDER BY sequence")
        for run in workflow_runs:
            run["attempts"] = rows(
                "SELECT * FROM workflow_node_attempts WHERE organization_id=? AND run_id=? ORDER BY node_id,attempt", (org, run["id"])
            )
            run["events"] = rows(
                "SELECT sequence,created_at,event_json FROM workflow_run_events WHERE organization_id=? AND run_id=? ORDER BY sequence",
                (org, run["id"]),
            )
        compute_jobs = []
        for row in db.execute("SELECT * FROM compute_jobs WHERE organization_id=? AND topic_id=? ORDER BY created_at,id", (org, topic)):
            job = ComputeStore._public_job(row)
            job["events"] = rows(
                "SELECT sequence,created_at,event_json FROM compute_events WHERE organization_id=? AND job_id=? ORDER BY sequence",
                (org, job["id"]),
            )
            job["artifacts"] = rows("SELECT * FROM compute_artifacts WHERE organization_id=? AND job_id=? ORDER BY name", (org, job["id"]))
            job["log_chunks"] = rows("SELECT * FROM compute_log_chunks WHERE job_id=? ORDER BY chunk_index", (job["id"],))
            compute_jobs.append(job)
        beta = target["kind"] == "beta"
        from .bot_runtime import BotRuntime

        bot_runs = []
        for row in db.execute("SELECT r.*,u.name AS bot_name,b.created_by AS owner_user_id FROM bot_runs r "
                              "JOIN bots b ON b.id=r.bot_id JOIN users u ON u.id=r.bot_id "
                              "WHERE r.organization_id=? AND r.topic_id=? ORDER BY r.created_at,r.id", (org, topic)):
            run = BotRuntime.public(row)
            run["tool_receipts"] = rows("SELECT sequence,name,result_json,created_at FROM bot_tool_receipts "
                                        "WHERE run_id=? ORDER BY sequence", (row["id"],))
            run["executions"] = rows("SELECT id,command_json,command_sha256,status,created_at,decided_at,consumed_at "
                                     "FROM bot_execution_approvals WHERE run_id=? ORDER BY created_at,id", (row["id"],))
            bot_runs.append(run)
        file_versions = rows(
            "SELECT v.*,b.digest,o.total_characters FROM research_artifact_versions v "
            "JOIN research_artifacts a ON a.id=v.artifact_id "
            "LEFT JOIN research_artifact_blobs b ON b.artifact_id=a.id AND b.revision=v.revision "
            "LEFT JOIN research_blob_objects o ON o.organization_id=b.organization_id "
            "AND o.project_id=b.project_id AND o.digest=b.digest "
            "WHERE a.organization_id=? AND a.project_id=? ORDER BY a.path,v.revision", (org, project["id"])
        ) if target["kind"] != "alpha" else []
        for version in file_versions:
            if version.pop("digest") is not None:
                version["text"] = None
                version["content_reference"] = {
                    "kind": "research_artifact", "id": version["artifact_id"],
                    "revision": version["revision"], "sha256": version["sha256"],
                }
            else:
                version["total_characters"] = len(version["text"])
        return {
            "schema": "reasoning.beta-topic-diagnostics.v1" if beta else "reasoning.topic-diagnostics.v1",
            "exported_at": datetime.now(UTC).isoformat(),
            "scope": "Complete retained database records for this authorized topic, "
            "plus accessible shared project research files and ledger records; "
            "no message or run pagination limit. Uploaded attachment bytes, compute artifact bodies, and chunked text files "
            "are referenced by metadata, not embedded. Recorded image projections and tool payloads remain "
            "in full/raw contexts and provider traces.",
            "privacy": "Contains private research, participant names, and recorded media projections or tool payloads "
            "in full/raw traces. Share only with trusted recipients. "
            "Operator credentials, provider keys, login sessions, and unsent drafts are excluded.",
            "coverage": "Provider request/parsed-response events are retained when recorded by the executor. "
            "Older calls may have only a context fingerprint and the last session; absent detail is not reconstructed.",
            "organization": {"id": org, "name": db.execute("SELECT name FROM organizations WHERE id=?", (org,)).fetchone()[0]},
            "project": {"id": project["id"], "name": project["name"]},
            "channel": {"id": channel["id"], "name": channel["name"]},
            "topic": decoded(target),
            "current_instructions": {
                **topic_instructions(db, org, topic), "harness_version": HARNESS_VERSION,
            } if beta else None,
            "continuation_policy": {
                "default_output_token_cap": None, "reservation_output_estimate_tokens": 8192,
                "recoverable_turn_errors": sorted(RECOVERABLE_TURN_ERRORS),
                "stops": ["user pause/cancel", "explicit goal completion/blocker", "authorized cost/time/turn/tool limits",
                          "unknown provider charge", "unresolved tool outcome", "nonrecoverable provider/access failure"],
                "billing": "Reservations are estimates, not invoice ceilings. Configure OpenRouter key spending limits.",
            } if beta else None,
            "messages": [store._message(row, db) for row in db.execute(
                "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                "WHERE m.organization_id=? AND m.topic_id=? ORDER BY m.sequence", (org, topic),
            )],
            "message_revisions": rows("SELECT r.* FROM message_revisions r JOIN messages m ON m.id=r.message_id "
                                      "WHERE r.organization_id=? AND m.topic_id=? ORDER BY r.sequence"),
            "attachments": [media_view(row) for row in db.execute(
                "SELECT * FROM research_media WHERE organization_id=? AND topic_id=? AND complete=1 ORDER BY created_at,id",
                (org, topic),
            )],
            "runs": runs, "provider_calls": calls, "goals": goals,
            "feedback": feedback_export(db, org, topic),
            "agent_tasks": rows("SELECT t.* FROM topic_agent_tasks t JOIN topic_agent_threads a ON a.root_run=t.root_run "
                                "WHERE a.organization_id=? AND a.topic_id=? ORDER BY t.created_at,t.id"),
            "agent_threads": rows("SELECT * FROM topic_agent_threads WHERE organization_id=? AND topic_id=? ORDER BY root_run"),
            "agent_waits": rows("SELECT w.* FROM topic_agent_waits w JOIN topic_agent_threads a ON a.root_run=w.root_run "
                                "WHERE a.organization_id=? AND a.topic_id=? ORDER BY w.id"),
            "agent_handoffs": rows("SELECT h.* FROM topic_agent_handoffs h JOIN llm_runs r ON r.id=h.parent_run "
                                   "JOIN llm_requests q ON q.id=r.request_id WHERE r.organization_id=? AND q.topic_id=? "
                                   "ORDER BY h.parent_run"),
            "bot_runs": bot_runs,
            "bot_agent_tasks": rows("SELECT t.* FROM bot_agent_tasks t JOIN bot_runs r ON r.id=t.parent_run "
                                    "WHERE r.organization_id=? AND r.topic_id=? ORDER BY t.id"),
            "bot_agent_handoffs": rows("SELECT h.* FROM bot_agent_handoffs h JOIN bot_runs r ON r.id=h.parent_run "
                                       "WHERE r.organization_id=? AND r.topic_id=? ORDER BY h.parent_run"),
            "bot_compute_approvals": rows("SELECT a.id,a.run_id,a.status,a.job_id,a.request_sha256,a.image_digest,a.created_at "
                                          "FROM bot_compute_approvals a JOIN bot_runs r ON r.id=a.run_id "
                                          "WHERE r.organization_id=? AND r.topic_id=? ORDER BY a.created_at,a.id"),
            "workflows": rows("SELECT * FROM workflows WHERE organization_id=? AND topic_id=? ORDER BY created_at,id"),
            "workflow_versions": rows(
                "SELECT v.* FROM workflow_versions v JOIN workflows w ON w.organization_id=v.organization_id AND w.id=v.workflow_id "
                "WHERE w.organization_id=? AND w.topic_id=? ORDER BY v.workflow_id,v.version"
            ),
            "workflow_runs": workflow_runs, "compute_jobs": compute_jobs,
            "files": rows(
                "SELECT * FROM research_artifacts WHERE organization_id=? AND project_id=? ORDER BY path", (org, project["id"])
            ) if target["kind"] != "alpha" else [],
            "file_versions": file_versions,
            "captured_contexts": rows(
                "SELECT * FROM research_contexts WHERE organization_id=? AND topic_id=? AND "
                + public_request("research_contexts.request_id") + " ORDER BY created_at,request_id"
            ),
            "memory_revisions": rows("SELECT * FROM research_memories WHERE organization_id=? AND topic_id=? ORDER BY revision"),
            "pins": rows("SELECT * FROM research_pins WHERE organization_id=? AND topic_id=? ORDER BY created_at,message_id"),
            "research_settings": rows("SELECT * FROM research_topic_settings WHERE organization_id=? AND topic_id=?"),
            "proposals": rows("SELECT * FROM research_proposals WHERE organization_id=? AND topic_id=? ORDER BY created_at,id"),
            "reviews": rows("SELECT * FROM mathematical_reviews WHERE organization_id=? AND topic_id=? ORDER BY created_at,id"),
            "mathematical_records": rows(
                "SELECT * FROM mathematical_records WHERE organization_id=? AND "
                "(topic_id=? OR (topic_id IS NULL AND (project_id=? OR project_id IS NULL))) ORDER BY path",
                (org, topic, project["id"]),
            ),
        }
