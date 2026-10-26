"""The beta conversation is the coordinator; delegates use its native runtime.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import time

from .openrouter import Quote
from .research_context import encoded
from .security import digest
from .store import StoreError, _id

ROLES = {
    "worker": "Investigate the assigned task. Record reusable evidence in the granted shared knowledge tools.",
    "coordinator": "Decompose only when useful. Integrate exact delegate results and preserve unresolved obligations.",
    "librarian": "Retrieve primary sources, organize bibliography, and report exact citations and coverage gaps.",
    "adviser": "Propose alternative approaches and explicit tradeoffs; do not present speculation as evidence.",
    "critic": "Independently inspect the assigned evidence. Report precise flaws; agreement is not verification.",
    "executor": "Use approved computation tools, retain exact source and diagnostics, and report what was actually checked.",
    "specification_reviewer": "Inspect the assigned request for ambiguity, missing assumptions, and measurable acceptance criteria.",
}


class TopicAgents:
    def __init__(self, inference, compute=None):
        self.inference, self.store = inference, inference.store
        self.compute = compute

    @staticmethod
    def metadata(db, run):
        row = db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (run["request_id"],)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def root(db, run):
        return TopicAgents.metadata(db, run).get("agent_root_run", run["id"])

    @staticmethod
    def pending(db, parent):
        return bool(
            db.execute(
                "SELECT 1 FROM (SELECT parent_run FROM topic_agent_tasks UNION SELECT parent_run FROM topic_agent_waits) t "
                "LEFT JOIN topic_agent_handoffs h ON h.parent_run=t.parent_run "
                "WHERE t.parent_run=? AND h.parent_run IS NULL LIMIT 1",
                (parent,),
            ).fetchone()
        )

    def wait(self, run, kind, identity):
        tables = {"compute": "compute_jobs", "review": "mathematical_reviews", "proposal": "research_proposals"}
        if kind not in tables:
            raise StoreError(400, "unknown asynchronous result kind")
        with self.store.transaction(write=True) as db:
            source = db.execute("SELECT organization_id,topic_id FROM " + tables[kind] + " WHERE id=?", (identity,)).fetchone()
            if source is None or source["organization_id"] != run["organization_id"]:
                raise StoreError(404, "asynchronous result unavailable")
            self.store._topic_access(db, run["requested_by"], run["organization_id"], source["topic_id"], write=True)
            root = self.root(db, run)
            db.execute(
                "INSERT INTO topic_agent_threads(root_run,organization_id,topic_id) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (root, run["organization_id"], run["topic_id"]),
            )
            values = {"compute": None, "review": None, "proposal": None}
            values[kind] = identity
            db.execute(
                "INSERT INTO topic_agent_waits VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (digest(run["id"] + ":" + kind + ":" + identity), root, run["id"], values["compute"], values["review"], values["proposal"]),
            )

    @staticmethod
    def _wait_view(db, wait):
        if wait["compute_id"]:
            row = db.execute("SELECT status,error_code,id FROM compute_jobs WHERE id=?", (wait["compute_id"],)).fetchone()
            return {
                "kind": "compute",
                "id": row["id"],
                "status": row["status"],
                "error_code": row["error_code"],
                "pending": row["status"] in {"queued", "running"},
                "unknown": row["status"] == "outcome_unknown",
                "recovery_tool": "compute_result",
            }
        if wait["review_id"]:
            row = db.execute(
                "SELECT status,result_json,reviewer_run_id FROM mathematical_reviews WHERE id=?", (wait["review_id"],)
            ).fetchone()
            charge = db.execute("SELECT accounting_state FROM llm_runs WHERE id=?", (row["reviewer_run_id"],)).fetchone()
            return {
                "kind": "review",
                "id": wait["review_id"],
                "status": row["status"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "pending": row["status"] in {"pending", "running"},
                "unknown": bool(charge and charge[0] == "uncertain"),
                "recovery_tool": "ledger_reviews",
            }
        row = db.execute("SELECT status,result_json,body_json FROM research_proposals WHERE id=?", (wait["proposal_id"],)).fetchone()
        result = json.loads(row["result_json"]) if row["result_json"] else None
        action = json.loads(row["body_json"])["action"]
        if row["status"] == "approved" and result and action == "compute":
            return TopicAgents._wait_view(db, {"compute_id": result["id"]})
        if row["status"] == "approved" and result and action == "workflow":
            execution = db.execute("SELECT status FROM workflow_runs WHERE id=?", (result["id"],)).fetchone()
            return {
                "kind": "workflow",
                "id": result["id"],
                "status": execution[0],
                "pending": execution[0] not in {"succeeded", "failed", "cancelled", "outcome_unknown"},
                "unknown": execution[0] == "outcome_unknown",
                "recovery_tool": "workflow_result",
            }
        return {
            "kind": "proposal",
            "id": wait["proposal_id"],
            "status": row["status"],
            "result": result,
            "pending": row["status"] in {"pending", "reviewing"},
            "unknown": False,
        }

    def delegate(self, run, client_id, role, objective, context, bot_model=None):
        if role not in ROLES or not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 8000:
            raise StoreError(400, "delegate needs a supported role and a bounded task")
        if not isinstance(context, str) or len(context) > 16000:
            raise StoreError(400, "delegate context exceeds its allowance")
        request_hash = digest(encoded({"role": role, "objective": objective, "context": context, "bot_model": bot_model}))
        with self.store.transaction(write=True) as db:
            if self.inference._stop_reason(db, run):
                raise StoreError(409, "agent execution is no longer authorized")
            self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"], write=True)
            old = db.execute("SELECT * FROM topic_agent_tasks WHERE parent_run=? AND client_id=?", (run["id"], client_id)).fetchone()
            if old:
                if old["request_sha256"] != request_hash:
                    raise StoreError(409, "delegate identity was used for different input")
                return self._view(db, old)
            metadata = self.metadata(db, run)
            if metadata.get("review_id"):
                raise StoreError(403, "a revision-pinned verifier cannot delegate its independent review")
            from .research_goals import ResearchGoals

            goal = ResearchGoals.execution_goal(db, run["id"])
            if goal and (goal["status"] != "active" or goal["turns"] >= goal["max_turns"]):
                raise StoreError(409, "goal continuation allowance exhausted")
            root = metadata.get("agent_root_run", run["id"])
            db.execute(
                "INSERT INTO topic_agent_threads(root_run,organization_id,topic_id) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (root, run["organization_id"], run["topic_id"]),
            )
            thread = db.execute("SELECT status FROM topic_agent_threads WHERE root_run=?", (root,)).fetchone()
            if thread[0] != "active":
                raise StoreError(409, "topic agents are paused or cancelled")
            identity = _id()
            prompt = metadata["system_prompt"] + "\nAssigned agent role: " + role + ".\n" + ROLES[role]
            prompt += (
                "\nWork only on your assigned task. Do not call goal_checkpoint or alter the topic-wide task list. "
                "Your assignment is not permission to read the full conversation. Use granted shared knowledge "
                "and explicit source references. Delegated work is not a truth certificate.\n"
            )
            snapshot = {
                **metadata,
                "system_prompt": prompt,
                "prompt_version": metadata["prompt_version"] + "+" + role,
                "through_sequence": 0,
                "memory_through_sequence": 0,
                "compact_through_sequence": 0,
                "ledger_revisions": [],
                "agent_root_run": root,
                "agent_role": role,
                "agent_task_id": identity,
                "agent_parent_run": run["id"],
                "agent_narrow_context": True,
            }
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": encoded({"task": objective.strip(), "assigned_context": context})},
            ]
            from .media_context import narrow_attachment_snapshot
            from .research_context import token_bound

            messages, attachments = narrow_attachment_snapshot(metadata, messages)
            snapshot.update(
                messages=messages,
                attachments=attachments,
                recent_sequences=[],
                pinned_message_ids=[],
                memory_revision=None,
                layers={
                    "instructions": True,
                    "working_memory": False,
                    "pins": 0,
                    "mathematical_records": 0,
                    "independent_reviews": 0,
                    "recent_messages": 0,
                },
                input_token_upper_bound=token_bound(messages),
            )
            parameters = self.inference.parameters([run["model"]], None, 50, None,
                                                   effort=json.loads(run["parameters_json"]).get("effort"))
            bot_run = None
            if bot_model is not None:
                from .bot_runtime import BotRuntime
                from .bot_store import BotStore

                bot_run = BotRuntime(BotStore(self.store)).enqueue(
                    run["requested_by"],
                    run["organization_id"],
                    run["topic_id"],
                    bot_model,
                    "delegate-bot-" + identity,
                    prompt=encoded({"task": objective.strip(), "role": role, "assigned_context": context}),
                    db=db,
                )
                child = None
            else:
                child = self.inference._enqueue(
                    db,
                    run["requested_by"],
                    run["organization_id"],
                    run["topic_id"],
                    "delegate-" + identity,
                    parameters,
                    [Quote(**json.loads(run["quote_json"]))],
                    research_override=(messages, snapshot),
                    internal_agent=True,
                )[0]
            db.execute(
                "INSERT INTO topic_agent_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    root,
                    run["id"],
                    child["id"] if child else None,
                    bot_run["id"] if bot_run else None,
                    client_id,
                    role,
                    objective.strip(),
                    encoded(context),
                    request_hash,
                    int(time.time()),
                ),
            )
            if goal and child:
                db.execute("UPDATE agent_goals SET turns=turns+1 WHERE id=?", (goal["id"],))
                ResearchGoals.event(db, goal["id"], None, {"type": "turn", "run_id": child["id"], "delegated": True})
            self.inference._event(
                db,
                run["organization_id"],
                run["id"],
                {
                    "type": "agent",
                    "phase": "delegated",
                    "summary": role + ": " + objective.strip()[:200],
                    "task_id": identity,
                    "child_run": child["id"] if child else None,
                    "bot_run": bot_run["id"] if bot_run else None,
                },
            )
            return self._view(db, db.execute("SELECT * FROM topic_agent_tasks WHERE id=?", (identity,)).fetchone())

    @staticmethod
    def _view(db, task, *, offset=None, length=8000):
        fields = "status,error_code" + (",output_text" if offset is not None else "")
        if task["child_bot_run"]:
            run = dict(db.execute("SELECT " + fields + " FROM bot_runs WHERE id=?", (task["child_bot_run"],)).fetchone())
            run["accounting_state"] = "uncertain" if run["status"] == "outcome_unknown" else "external"
        else:
            run = db.execute("SELECT " + fields + ",accounting_state FROM llm_runs WHERE id=?", (task["child_run"],)).fetchone()
        result = {key: task[key] for key in ("id", "role", "objective", "parent_run", "child_run", "child_bot_run", "root_run")}
        result.update(status=run["status"], error_code=run["error_code"], accounting_state=run["accounting_state"])
        if offset is not None:
            text = run["output_text"]
            result.update(
                content=text[offset : offset + length],
                offset=offset,
                total_characters=len(text),
                sha256=digest(text),
                next_offset=offset + len(text[offset : offset + length]),
            )
        return result

    def tasks(self, run, after="", limit=20):
        with self.store.transaction() as db:
            self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"])
            root = self.root(db, run)
            tasks = db.execute(
                "WITH RECURSIVE lineage(id) AS (SELECT ? UNION SELECT h.parent_run FROM topic_agent_handoffs h "
                "JOIN lineage l ON h.next_run=l.id) SELECT * FROM topic_agent_tasks "
                "WHERE root_run=? AND parent_run IN (SELECT id FROM lineage) AND id>? ORDER BY id LIMIT ?",
                (run["id"], root, after, limit + 1),
            ).fetchall()
            page = tasks[:limit]
            return {"items": [self._view(db, task) for task in page], "next_after": page[-1]["id"] if len(tasks) > limit else None}

    def read(self, run, task_id, offset=0, length=8000):
        with self.store.transaction() as db:
            self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"])
            task = db.execute("SELECT * FROM topic_agent_tasks WHERE id=? AND root_run=?", (task_id, self.root(db, run))).fetchone()
            allowed = (
                task
                and db.execute(
                    "WITH RECURSIVE lineage(id) AS (SELECT ? UNION SELECT h.parent_run FROM topic_agent_handoffs h "
                    "JOIN lineage l ON h.next_run=l.id) SELECT 1 FROM lineage WHERE id=?",
                    (run["id"], task["parent_run"]),
                ).fetchone()
            )
            if not allowed:
                raise StoreError(404, "agent task is outside this assignment")
            return self._view(db, task, offset=offset, length=length)

    def control(self, user, org, topic, root, status):
        if status not in {"active", "paused", "cancelled"}:
            raise StoreError(400, "choose pause, resume, or cancel")
        with self.store.transaction(write=True) as db:
            role = self.store._topic_access(db, user, org, topic, write=True)[0]
            thread = db.execute(
                "SELECT a.*,q.requested_by FROM topic_agent_threads a JOIN llm_runs r ON r.id=a.root_run "
                "JOIN llm_requests q ON q.id=r.request_id WHERE a.root_run=? AND a.organization_id=? AND a.topic_id=?",
                (root, org, topic),
            ).fetchone()
            if not thread:
                raise StoreError(404, "agent conversation unavailable")
            if thread["requested_by"] != user and role not in {"owner", "admin"}:
                raise StoreError(403, "only the requester or administrator can control agents")
            if thread["status"] == "complete" or thread["status"] == "cancelled" and status != "cancelled":
                raise StoreError(409, "this agent conversation has ended; send a new message")
            from .research_goals import ResearchGoals

            goal = ResearchGoals.execution_goal(db, root)
            if goal and status == "active":
                if (
                    goal["status"] in {"complete", "cancelled"}
                    or goal["deadline_at"] <= int(time.time())
                    or goal["turns"] >= goal["max_turns"]
                    or goal["tool_calls"] >= goal["max_tool_calls"]
                ):
                    raise StoreError(409, "the goal is finished or its explicit continuation allowance is exhausted")
                if ResearchGoals.view(db, goal)["used_or_held_microusd"] >= goal["budget_microusd"]:
                    raise StoreError(409, "the goal's spending allowance is exhausted")
                db.execute("UPDATE agent_goals SET status='active',reason=NULL WHERE id=?", (goal["id"],))
            elif goal and goal["status"] not in {"complete", "cancelled"}:
                db.execute("UPDATE agent_goals SET status=? WHERE id=?", (status, goal["id"]))
            if status == "active":
                if any(
                    self._wait_view(db, wait)["unknown"]
                    for wait in db.execute("SELECT * FROM topic_agent_waits WHERE root_run=?", (root,)).fetchall()
                ):
                    raise StoreError(409, "an asynchronous execution or review outcome is unknown; reconcile it before resuming")
                self._resume(db, root)
            db.execute("UPDATE topic_agent_threads SET status=?,reason=NULL WHERE root_run=?", (status, root))
            if status == "cancelled":
                for wait in db.execute("SELECT * FROM topic_agent_waits WHERE root_run=?", (root,)).fetchall():
                    result = self._wait_view(db, wait)
                    if result["kind"] == "compute" and self.compute is not None:
                        job = self.compute._job(db, org, result["id"])
                        self.compute._access(db, user, org, job, write=True)
                        self.compute._request_cancel(db, org, job)
                    if wait["review_id"]:
                        review = db.execute("SELECT reviewer_run_id FROM mathematical_reviews WHERE id=?", (wait["review_id"],)).fetchone()
                        if review[0]:
                            run = db.execute("SELECT * FROM llm_runs WHERE id=?", (review[0],)).fetchone()
                            self.inference._request_cancel(db, org, run)
                        else:
                            db.execute(
                                "UPDATE mathematical_reviews SET status='cancelled',finished_at=? WHERE id=? AND status='pending'",
                                (int(time.time()), wait["review_id"]),
                            )
                    if wait["proposal_id"]:
                        db.execute(
                            "UPDATE research_proposals SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=? AND status='pending'",
                            (user, int(time.time()), wait["proposal_id"]),
                        )
                for row in db.execute(
                    "SELECT r.* FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id "
                    "JOIN research_contexts c ON c.request_id=q.id "
                    "WHERE r.organization_id=? AND q.topic_id=? AND "
                    "(r.id=? OR json_extract(c.snapshot_json,'$.agent_root_run')=?) AND r.status IN ('queued','running')",
                    (org, topic, root, root),
                ).fetchall():
                    self.inference._request_cancel(db, org, row)
                # A cancellation request is not proof that an owner's local process stopped.
                db.execute(
                    "UPDATE bot_runs SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'outcome_unknown' END, "
                    "error_code='cancelled' WHERE id IN "
                    "(SELECT child_bot_run FROM topic_agent_tasks WHERE root_run=?) "
                    "AND status IN ('queued','running','waiting_approval')",
                    (root,),
                )
                db.execute(
                    "UPDATE bot_execution_approvals SET status='denied',decided_at=? WHERE run_id IN "
                    "(SELECT child_bot_run FROM topic_agent_tasks WHERE root_run=?) AND status IN ('pending','approved')",
                    (int(time.time()), root),
                )
            self.store._audit(db, org, user, "topic.agents." + status, root)
            return {"root_run": root, "status": status}

    def control_topic(self, user, org, topic, status):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic, write=True)
            roots = [
                row[0]
                for row in db.execute(
                    "SELECT a.root_run FROM topic_agent_threads a JOIN llm_runs r ON r.id=a.root_run "
                    "JOIN llm_requests q ON q.id=r.request_id WHERE a.organization_id=? AND a.topic_id=? "
                    "AND q.requested_by=? AND a.status IN ('active','paused') ORDER BY a.root_run",
                    (org, topic, user),
                )
            ]
        results = [self.control(user, org, topic, root, status) for root in roots]
        from .research_goals import ResearchGoals

        goals = ResearchGoals(self.inference)
        goal = goals.current(user, org, topic)
        if goal and goal["owned_by"] == user and goal["status"] not in {"complete", "cancelled"} and goal["status"] != status:
            goals.control(user, org, topic, goal["id"], status)
        return {"status": status, "threads": results}

    def _resume(self, db, root):
        """An explicit resume may continue known effects, never resubmit an unknown call."""
        if db.execute(
            "SELECT 1 FROM topic_agent_tasks t JOIN bot_runs b ON b.id=t.child_bot_run WHERE t.root_run=? AND b.status='outcome_unknown'",
            (root,),
        ).fetchone():
            raise StoreError(409, "a local bot outcome is unknown; inspect its owner's retained invocation before continuing")
        rows = db.execute(
            self.inference._joined() + "JOIN research_contexts c ON c.request_id=q.id "
            "LEFT JOIN topic_agent_handoffs h ON h.parent_run=r.id "
            "WHERE (r.id=? OR json_extract(c.snapshot_json,'$.agent_root_run')=?) "
            "AND h.parent_run IS NULL AND r.status IN ('cancelled','interrupted','failed')",
            (root, root),
        ).fetchall()
        for value in rows:
            run = dict(value)
            if run["accounting_state"] == "uncertain":
                raise StoreError(409, "reconcile the interrupted agent charge before resuming")
            if db.execute(
                "SELECT 1 FROM inference_calls i JOIN inference_call_events e ON e.sequence="
                "(SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id) "
                "WHERE i.run_id=? AND json_extract(e.data_json,'$.accounting_state') IN ('reserved','uncertain')",
                (run["id"],),
            ).fetchone():
                raise StoreError(409, "reconcile the interrupted provider call before resuming")
            if run["status"] != "failed" and run["error_code"] not in {"agents_paused", "worker_interrupted"}:
                continue
            from .research_goals import failed_read

            receipts = db.execute("SELECT * FROM research_tool_receipts WHERE run_id=? ORDER BY sequence", (run["id"],)).fetchall()
            if any(
                receipt["status"] in {"requested", "uncertain"} or receipt["status"] == "failed" and not failed_read(receipt)
                for receipt in receipts
            ):
                raise StoreError(409, "an interrupted tool outcome needs review before resuming")
            if self.pending(db, run["id"]):
                continue  # The result handoff owns this continuation, after its delegates finish.
            snapshot = self.metadata(db, run)
            messages = json.loads(run["context_json"]) + [
                {
                    "role": "user",
                    "content": encoded(
                        {
                            "instruction": "The human explicitly resumed this assignment. "
                            "Continue from retained effects; do not replay writes.",
                            "previous_output": run["output_text"][-8000:],
                            "completed_receipts": [
                                {
                                    "id": receipt["id"],
                                    "name": receipt["name"],
                                    "status": receipt["status"],
                                    "result_preview": (receipt["result_json"] or "")[:1200],
                                }
                                for receipt in receipts[-20:]
                            ],
                        }
                    ),
                }
            ]
            snapshot = {
                **snapshot,
                "agent_root_run": root,
                "through_sequence": 0,
                "compact_through_sequence": 0,
                "memory_through_sequence": 0,
            }
            next_run = self.inference._enqueue(
                db,
                run["requested_by"],
                run["organization_id"],
                run["topic_id"],
                "resume-agent-" + run["id"],
                self.inference.parameters([run["model"]], None, 50, None,
                                          effort=json.loads(run["parameters_json"]).get("effort")),
                [Quote(**json.loads(run["quote_json"]))],
                research_override=(messages, snapshot),
                internal_agent=True,
            )[0]
            db.execute("INSERT INTO topic_agent_handoffs VALUES(?,?)", (run["id"], next_run["id"]))
            db.execute("UPDATE topic_agent_tasks SET child_run=? WHERE child_run=?", (next_run["id"], run["id"]))
            from .research_goals import ResearchGoals

            goal = ResearchGoals.execution_goal(db, run["id"])
            if goal:
                if not snapshot.get("agent_task_id"):
                    db.execute("UPDATE agent_goals SET run_id=?,turns=turns+1 WHERE id=?", (next_run["id"], goal["id"]))
                else:
                    db.execute("UPDATE agent_goals SET turns=turns+1 WHERE id=?", (goal["id"],))
                ResearchGoals.event(db, goal["id"], None, {"type": "turn", "run_id": next_run["id"], "explicit_resume": True})

    @staticmethod
    def stop_reason(db, run):
        root = TopicAgents.root(db, run)
        thread = db.execute("SELECT status FROM topic_agent_threads WHERE root_run=?", (root,)).fetchone()
        if thread and thread[0] == "cancelled":
            return "cancel_requested"
        if thread and thread[0] == "paused":
            return "agents_paused"
        if thread:
            from .research_goals import ResearchGoals

            goal = ResearchGoals.execution_goal(db, run["id"])
            if goal and (goal["status"] not in {"active", "complete"} or goal["deadline_at"] <= int(time.time())):
                return "goal_stopped"
        return None

    def threads(self, user, org, topic):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT a.*,q.requested_by,COUNT(t.id) AS tasks FROM topic_agent_threads a "
                    "JOIN llm_runs r ON r.id=a.root_run JOIN llm_requests q ON q.id=r.request_id "
                    "LEFT JOIN topic_agent_tasks t ON t.root_run=a.root_run "
                    "WHERE a.organization_id=? AND a.topic_id=? AND a.status IN ('active','paused') "
                    "GROUP BY a.root_run,a.organization_id,a.topic_id,a.status,a.reason,q.requested_by "
                    "ORDER BY a.root_run LIMIT 50",
                    (org, topic),
                )
            ]

    def dispatch(self):
        """No provider calls: atomically enqueue one handoff after all direct children finish."""
        with self.store.transaction(write=True) as db:
            db.execute(
                "UPDATE topic_agent_threads SET status='paused',reason='agent_charge_unknown' WHERE status='active' AND ("
                "root_run IN (SELECT id FROM llm_runs WHERE status NOT IN ('queued','running') AND accounting_state='uncertain') OR "
                "root_run IN (SELECT t.root_run FROM topic_agent_tasks t JOIN llm_runs r ON r.id=t.child_run "
                "WHERE r.status NOT IN ('queued','running') AND r.accounting_state='uncertain') OR "
                "root_run IN (SELECT t.root_run FROM topic_agent_tasks t JOIN bot_runs b ON b.id=t.child_bot_run "
                "WHERE b.status='outcome_unknown'))"
            )
            db.execute(
                "UPDATE topic_agent_threads SET status='paused',reason='agent_response_failed' WHERE status='active' "
                "AND root_run IN (SELECT id FROM llm_runs WHERE status='failed')"
            )
        with self.store.transaction() as db:
            query = (
                "SELECT DISTINCT t.parent_run FROM (SELECT parent_run,root_run FROM topic_agent_tasks UNION "
                "SELECT parent_run,root_run FROM topic_agent_waits) t JOIN topic_agent_threads a ON a.root_run=t.root_run "
                "JOIN llm_runs p ON p.id=t.parent_run LEFT JOIN topic_agent_handoffs h ON h.parent_run=t.parent_run "
                "WHERE a.status='active' AND (p.status='succeeded' OR (p.status='cancelled' AND p.error_code='agents_paused')) "
                "AND p.accounting_state IN ('settled','released') "
                "AND h.parent_run IS NULL AND t.parent_run>? ORDER BY t.parent_run LIMIT 100"
            )
            parents = db.execute(query, (self.inference._agent_handoff_after,)).fetchall()
            if not parents and self.inference._agent_handoff_after:
                parents = db.execute(query, ("",)).fetchall()
            # This cursor is only a fairness optimization; retained handoff rows
            # are the durable authority, so restart may safely start at the front.
            self.inference._agent_handoff_after = parents[-1][0] if parents else ""
        for parent in parents:
            try:
                self._handoff(parent[0])
            except Exception as exc:
                with self.store.transaction(write=True) as db:
                    task = db.execute(
                        "SELECT root_run FROM topic_agent_tasks WHERE parent_run=? UNION "
                        "SELECT root_run FROM topic_agent_waits WHERE parent_run=? LIMIT 1",
                        (parent[0], parent[0]),
                    ).fetchone()
                    if task:
                        reason = "agent_handoff_refused" if isinstance(exc, StoreError) else "agent_handoff_error"
                        db.execute(
                            "UPDATE topic_agent_threads SET status='paused',reason=? WHERE root_run=? AND status='active'",
                            (reason, task[0]),
                        )
        with self.store.transaction(write=True) as db:
            self._completed(db)

    def _handoff(self, parent):
        with self.store.transaction(write=True) as db:
            if db.execute("SELECT 1 FROM topic_agent_handoffs WHERE parent_run=?", (parent,)).fetchone():
                return
            tasks = db.execute(
                "SELECT t.*,COALESCE(r.status,b.status) AS status,"
                "CASE WHEN b.status='outcome_unknown' THEN 'uncertain' ELSE r.accounting_state END AS accounting_state "
                "FROM topic_agent_tasks t LEFT JOIN llm_runs r ON r.id=t.child_run "
                "LEFT JOIN bot_runs b ON b.id=t.child_bot_run "
                "WHERE t.parent_run=? ORDER BY t.id",
                (parent,),
            ).fetchall()
            waits = db.execute("SELECT * FROM topic_agent_waits WHERE parent_run=? ORDER BY id", (parent,)).fetchall()
            if not tasks and not waits:
                return
            root = (tasks or waits)[0]["root_run"]
            thread = db.execute("SELECT status FROM topic_agent_threads WHERE root_run=?", (root,)).fetchone()
            if thread[0] != "active":
                return
            if any(
                task["status"] in {"queued", "running", "waiting_approval"}
                or task["accounting_state"] == "uncertain"
                or task["child_run"]
                and self.pending(db, task["child_run"])
                for task in tasks
            ):
                return
            row = dict(db.execute(self.inference._joined() + "WHERE r.id=?", (parent,)).fetchone())
            if self.compute is not None and any(wait["compute_id"] or wait["proposal_id"] for wait in waits):
                self.compute._expire(db, row["organization_id"])
            external = [self._wait_view(db, wait) for wait in waits]
            if any(result["unknown"] for result in external):
                db.execute("UPDATE topic_agent_threads SET status='paused',reason='agent_charge_unknown' WHERE root_run=?", (root,))
                return
            if any(result["pending"] for result in external):
                return
            if self.inference._stop_reason(db, row):
                return
            snapshot = self.metadata(db, row)
            from .research_goals import ResearchGoals

            goal = ResearchGoals.execution_goal(db, row["id"])
            if goal and (goal["status"] != "active" or goal["turns"] >= goal["max_turns"] or goal["deadline_at"] <= int(time.time())):
                return
            messages = json.loads(row["context_json"])
            if not snapshot.get("agent_narrow_context"):
                # The human can redirect the coordinator while delegates run.
                # Delegates themselves never acquire the full topic transcript.
                from .research_context import ResearchContext

                through = db.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM messages WHERE organization_id=? AND topic_id=?",
                    (row["organization_id"], row["topic_id"]),
                ).fetchone()[0]
                messages, refreshed = ResearchContext(self.store).compile(
                    db,
                    row["organization_id"],
                    row["topic_id"],
                    through,
                    [Quote(**json.loads(row["quote_json"]))],
                    user=row["requested_by"],
                    prompt_override=snapshot,
                )
                snapshot = {**snapshot, **refreshed}
                from .inference_additions import included

                included(db, self.inference, row["organization_id"], root, through)
            messages += [
                {"role": "assistant", "content": row["output_text"]},
                {
                    "role": "user",
                    "content": encoded(
                        {
                            "instruction": "Delegated work has returned. Integrate agent and asynchronous tool results; "
                            "read exact agent outputs with agent_read and computation evidence with compute_result. "
                            "Continue the original request, not an unrelated objective. Failures are evidence, not completed work.",
                            "results": [self._view(db, task, offset=0, length=1200) for task in tasks[:20]],
                            "total_results": len(tasks),
                            "external_results": external,
                            "recovery_tool": "agent_tasks",
                        }
                    ),
                },
            ]
            snapshot = {
                **snapshot,
                "agent_root_run": root,
                "through_sequence": 0,
                "memory_through_sequence": 0,
                "compact_through_sequence": 0,
            }
            parameters = self.inference.parameters([row["model"]], None, 50, None,
                                                   effort=json.loads(row["parameters_json"]).get("effort"))
            next_run = self.inference._enqueue(
                db,
                row["requested_by"],
                row["organization_id"],
                row["topic_id"],
                "handoff-" + row["id"],
                parameters,
                [Quote(**json.loads(row["quote_json"]))],
                research_override=(messages, snapshot),
                internal_agent=True,
            )[0]
            db.execute("INSERT INTO topic_agent_handoffs VALUES(?,?)", (row["id"], next_run["id"]))
            db.execute("UPDATE topic_agent_tasks SET child_run=? WHERE child_run=?", (next_run["id"], row["id"]))
            if goal:
                db.execute("UPDATE agent_goals SET run_id=?,turns=turns+1 WHERE id=?", (next_run["id"], goal["id"]))
                ResearchGoals.event(db, goal["id"], None, {"type": "turn", "run_id": next_run["id"], "delegated_handoff": True})

    @staticmethod
    def _completed(db):
        db.execute(
            "UPDATE topic_agent_threads SET status='complete' WHERE status='active' AND "
            "NOT EXISTS (SELECT 1 FROM topic_agent_tasks t LEFT JOIN topic_agent_handoffs h ON h.parent_run=t.parent_run "
            "WHERE t.root_run=topic_agent_threads.root_run AND h.parent_run IS NULL) AND "
            "NOT EXISTS (SELECT 1 FROM topic_agent_waits w LEFT JOIN topic_agent_handoffs h ON h.parent_run=w.parent_run "
            "WHERE w.root_run=topic_agent_threads.root_run AND h.parent_run IS NULL) AND "
            "NOT EXISTS (SELECT 1 FROM llm_runs r JOIN research_contexts c ON c.request_id=r.request_id "
            "WHERE (r.id=topic_agent_threads.root_run OR json_extract(c.snapshot_json,'$.agent_root_run')=topic_agent_threads.root_run) "
            "AND r.status IN ('queued','running')) AND "
            "EXISTS (SELECT 1 FROM llm_runs r JOIN research_contexts c ON c.request_id=r.request_id "
            "LEFT JOIN topic_agent_handoffs h ON h.parent_run=r.id "
            "WHERE (r.id=topic_agent_threads.root_run OR json_extract(c.snapshot_json,'$.agent_root_run')=topic_agent_threads.root_run) "
            "AND json_extract(c.snapshot_json,'$.agent_task_id') IS NULL AND h.parent_run IS NULL "
            "AND r.status='succeeded')"
        )


def register_agents(research):
    from .research_tools import schema, string

    if research.reviewer or not research.enabled:
        return
    agents = TopicAgents(research.inference)

    @research.registry.tool(
        name="agent_delegate",
        description="Assign a narrowly scoped task to an agent using this selected model. "
        "Roles include worker, librarian, critic and coordinator. Optionally select an available owner-authorized "
        "bot model. Results return automatically; do not poll.",
        input_schema=schema(
            {
                "role": {"enum": list(ROLES)},
                "objective": string(8000),
                "context": {"type": "string", "maxLength": 16000},
                "bot_model": string(200),
            },
            ["role", "objective"],
        ),
    )
    def delegate(role, objective, context="", bot_model=None):
        return agents.delegate(research.row, "tool-" + str(research.sequence), role, objective, context, bot_model)

    @research.registry.tool(
        name="agent_tasks",
        description="Page direct delegated task headers after a result handoff; do not busy-poll.",
        input_schema=schema({"after": {"type": "string", "maxLength": 32}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    )
    def tasks(after="", limit=20):
        return agents.tasks(research.row, after, limit)

    @research.registry.tool(
        name="agent_read",
        description="Read an exact bounded delegated result with its digest and failure state.",
        input_schema=schema(
            {
                "task_id": string(32),
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 16000},
            },
            ["task_id"],
        ),
    )
    def read(task_id, offset=0, length=8000):
        return agents.read(research.row, task_id, offset, length)
