"""User-owned research goals and bounded durable continuation."""

from __future__ import annotations

import asyncio
import json
import time

from .research_context import encoded
from .store import StoreError, _id, _row
from .worker_poll import database_busy

RECOVERABLE_TURN_ERRORS = {
    "agent_empty_response", "agent_invalid_tool_arguments", "agent_tool_limit", "output_token_limit", "agent_incomplete",
}
READ_ONLY_TOOLS = {
    "topic_history", "topic_message_read", "topic_search", "tool_receipt_read", "tool_search",
    "ledger_status", "ledger_search", "ledger_read", "knowledge_search", "knowledge_read", "model_catalog",
    "workflow_list", "compute_catalog", "compute_result", "artifact_read", "web_fetch", "web_search",
    "arxiv_search", "arxiv_lookup", "arxiv_read", "arxiv_source_list", "arxiv_source_read", "crossref_search", "crossref_lookup",
}


def failed_read(receipt):
    if "result_json" in dict(receipt) and receipt["result_json"]:
        result = json.loads(receipt["result_json"])
        if result.get("code") == "unknown_reference_id":
            return True  # Validated before any mathematical mutation.
    name = receipt["name"]
    if name == "tool_call":
        arguments = json.loads(receipt["arguments_json"])
        name = arguments.get("name") if isinstance(arguments, dict) else None
    return isinstance(name, str) and name in READ_ONLY_TOOLS


class ResearchGoals:
    def __init__(self, inference):
        self.inference, self.store = inference, inference.store

    @staticmethod
    def execution_goal(db, run_id):
        """Return the author or review run's owning goal for shared tool admission."""
        direct = db.execute(
            "SELECT g.* FROM agent_goals g WHERE g.run_id=? OR EXISTS "
            "(SELECT 1 FROM agent_goal_events e WHERE e.goal_id=g.id "
            "AND json_extract(e.event_json,'$.type')='review' AND json_extract(e.event_json,'$.run_id')=?)",
            (run_id, run_id),
        ).fetchone()
        if direct:
            return direct
        context = db.execute("SELECT c.snapshot_json FROM research_contexts c JOIN llm_runs r ON r.request_id=c.request_id "
                             "WHERE r.id=?", (run_id,)).fetchone()
        root = json.loads(context[0]).get("agent_root_run") if context else None
        if root:
            return db.execute("SELECT g.* FROM agent_goals g WHERE g.run_id=? OR EXISTS "
                              "(SELECT 1 FROM agent_goal_events e WHERE e.goal_id=g.id "
                              "AND json_extract(e.event_json,'$.run_id')=?)", (root, root)).fetchone()
        return None

    @staticmethod
    def event(db, goal, actor, event):
        db.execute(
            "INSERT INTO agent_goal_events(goal_id,actor_id,event_json,created_at) VALUES(?,?,?,?)",
            (goal, actor, encoded(event), int(time.time())),
        )

    @staticmethod
    def view(db, row):
        result = dict(row)
        result["acceptance"] = json.loads(result.pop("acceptance_json"))
        calls = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN r.accounting_state='settled' THEN COALESCE(r.actual_microusd,0) "
            "WHEN r.accounting_state='released' THEN 0 ELSE r.reserved_microusd END),0) "
            "FROM llm_runs r JOIN agent_goal_events e ON e.goal_id=? AND json_extract(e.event_json,'$.run_id')=r.id "
            "WHERE json_extract(e.event_json,'$.type') IN ('turn','review')",
            (row["id"],),
        ).fetchone()[0]
        result["used_or_held_microusd"] = calls
        result["billing_source"] = "bot_account" if row["model"].startswith("bot/") else "openrouter"
        result["events"] = [
            dict(item) | {"event": json.loads(item["event_json"])}
            for item in db.execute(
                "SELECT sequence,event_json,created_at FROM agent_goal_events WHERE goal_id=? ORDER BY sequence DESC LIMIT 40", (row["id"],)
            )
        ]
        return result

    def current(self, user, org, topic):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            row = db.execute(
                "SELECT * FROM agent_goals WHERE organization_id=? AND topic_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (org, topic)
            ).fetchone()
            return self.view(db, row) if row else None

    def create(
        self, user, org, topic, *, client_id, objective, acceptance, model, budget_microusd, max_turns, max_tool_calls,
        duration_seconds, effort=None
    ):
        now = int(time.time())
        if (
            not isinstance(objective, str)
            or not 1 <= len(objective.strip()) <= 8000
            or not isinstance(acceptance, list)
            or not 1 <= len(acceptance) <= 20
            or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 2000 for item in acceptance)
            or not 1 <= budget_microusd <= 10**12
            or not 1 <= max_turns <= 100
            or not 1 <= max_tool_calls <= 1000
            or not 60 <= duration_seconds <= 86400
        ):
            raise StoreError(400, "a goal needs an objective, acceptance criteria, and bounded cost, time, turns, and tools")
        with self.store.transaction(write=True) as db:
            target = self.store._topic_access(db, user, org, topic, write=True)[4]
            if target["kind"] != "beta":
                raise StoreError(400, "research goals belong to beta topics")
            old = db.execute(
                "SELECT * FROM agent_goals WHERE organization_id=? AND owned_by=? AND client_id=?", (org, user, client_id)
            ).fetchone()
            if old:
                if (
                    old["topic_id"] != topic
                    or old["objective"] != objective.strip()
                    or json.loads(old["acceptance_json"]) != acceptance
                    or old["model"] != model
                    or old["effort"] != effort
                    or old["budget_microusd"] != budget_microusd
                    or old["max_turns"] != max_turns
                    or old["max_tool_calls"] != max_tool_calls
                    or old["deadline_at"] - old["created_at"] != duration_seconds
                ):
                    raise StoreError(409, "goal request ID was used for different input")
                return self.view(db, old)
            if db.execute(
                "SELECT 1 FROM agent_goals WHERE organization_id=? AND topic_id=? AND status IN ('active','paused','blocked')", (org, topic)
            ).fetchone():
                raise StoreError(409, "this topic already has an active goal; finish or cancel it first")
            from .bot_runtime import BotRuntime, bot_model
            from .bot_store import BotStore
            from .reasoning_effort import checked

            checked(effort)
            if bot_model(model):
                if model not in {item["id"] for item in BotRuntime(BotStore(self.store)).models(user, org, topic)}:
                    raise StoreError(400, "select an accessible bot model")
            else:
                self.inference._preflight(db, user, org, topic)
            goal = _id()
            db.execute(
                "INSERT INTO agent_goals(id,organization_id,topic_id,owned_by,client_id,objective,acceptance_json,model,status,"
                "budget_microusd,max_turns,max_tool_calls,deadline_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)",
                (
                    goal,
                    org,
                    topic,
                    user,
                    client_id,
                    objective.strip(),
                    encoded(acceptance),
                    model,
                    budget_microusd,
                    max_turns,
                    max_tool_calls,
                    now + duration_seconds,
                    now,
                    now,
                ),
            )
            self.event(db, goal, user, {"type": "created", "objective": objective.strip(), "acceptance": acceptance})
            db.execute("UPDATE agent_goals SET effort=? WHERE id=?", (effort, goal))
            self.store._audit(db, org, user, "goal.create", goal)
            return self.view(db, db.execute("SELECT * FROM agent_goals WHERE id=?", (goal,)).fetchone())

    def control(self, user, org, topic, goal, status):
        if status not in {"paused", "active", "cancelled"}:
            raise StoreError(400, "choose pause, resume, or cancel")
        with self.store.transaction(write=True) as db:
            role = self.store._topic_access(db, user, org, topic, write=True)[0]
            row = _row(
                db.execute("SELECT * FROM agent_goals WHERE id=? AND organization_id=? AND topic_id=?", (goal, org, topic)).fetchone()
            )
            if row["owned_by"] != user and role not in {"owner", "admin"}:
                raise StoreError(403, "only the goal owner or an administrator can control it")
            if row["status"] in {"complete", "cancelled"}:
                raise StoreError(409, "finished goals cannot resume")
            if status == "active":
                if db.execute(
                    "SELECT 1 FROM llm_runs r JOIN agent_goal_events e ON json_extract(e.event_json,'$.run_id')=r.id "
                    "WHERE e.goal_id=? AND json_extract(e.event_json,'$.type')='review' AND r.accounting_state='uncertain'",
                    (goal,),
                ).fetchone():
                    raise StoreError(409, "reconcile the independent review charge before resuming")
                if row["deadline_at"] <= int(time.time()) or row["turns"] >= row["max_turns"] or row["tool_calls"] >= row["max_tool_calls"]:
                    raise StoreError(409, "goal budget is exhausted; create a new goal with reviewed limits")
                if row["run_id"]:
                    call = db.execute("SELECT status,accounting_state FROM llm_runs WHERE id=?", (row["run_id"],)).fetchone()
                    if call and call["accounting_state"] == "uncertain":
                        raise StoreError(409, "reconcile the interrupted provider call before resuming this goal")
                    if call and call["status"] not in {"queued", "running", "succeeded"}:
                        if call["accounting_state"] not in {"settled", "released"}:
                            raise StoreError(409, "reconcile the previous provider call before resuming this goal")
                        # Explicit resume starts a fresh turn; the original provider request remains immutable.
                        db.execute("UPDATE agent_goals SET run_id=NULL WHERE id=?", (goal,))
                if row["bot_run_id"]:
                    bot_run = db.execute("SELECT status FROM bot_runs WHERE id=?", (row["bot_run_id"],)).fetchone()
                    if bot_run and bot_run["status"] == "outcome_unknown":
                        raise StoreError(409, "the bot invocation outcome is unknown; owner reconciliation is required")
                    if bot_run and bot_run["status"] in {"failed", "cancelled"}:
                        db.execute("UPDATE agent_goals SET bot_run_id=NULL WHERE id=?", (goal,))
                if self.view(db, row)["used_or_held_microusd"] >= row["budget_microusd"]:
                    raise StoreError(409, "goal cost budget is exhausted; create a new goal with reviewed limits")
            elif row["run_id"]:
                run = db.execute("SELECT * FROM llm_runs WHERE id=?", (row["run_id"],)).fetchone()
                if run:
                    self.inference._request_cancel(db, org, run)
            elif row["bot_run_id"]:
                from .bot_runtime import BotRuntime
                from .bot_store import BotStore

                BotRuntime(BotStore(self.store)).stop(user, org, row["bot_run_id"])
            db.execute("UPDATE agent_goals SET status=?,reason=NULL,updated_at=? WHERE id=?", (status, int(time.time()), goal))
            self.event(db, goal, user, {"type": "status", "status": status})
            self.store._audit(db, org, user, "goal." + status, goal)
        return self.current(user, org, topic)

    def checkpoint(self, row, summary, status="active"):
        if status not in {"active", "complete", "blocked"} or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 4000:
            raise StoreError(400, "checkpoint needs evidence and an active, complete, or blocked state")
        with self.store.transaction(write=True) as db:
            from .topic_agents import TopicAgents

            if status == "complete" and TopicAgents.pending(db, row["id"]):
                raise StoreError(409, "delegated work is pending; checkpoint progress, not completion")
            self.store._topic_access(db, row["requested_by"], row["organization_id"], row["topic_id"], write=True)
            goal = db.execute("SELECT * FROM agent_goals WHERE run_id=? AND status='active'", (row["id"],)).fetchone()
            if not goal:
                raise StoreError(409, "this run has no active goal")
            if (
                status == "complete"
                and db.execute(
                    "SELECT 1 FROM compute_jobs WHERE organization_id=? AND topic_id=? "
                    "AND status IN ('queued','running','outcome_unknown')",
                    (row["organization_id"], row["topic_id"]),
                ).fetchone()
            ):
                raise StoreError(409, "computation is still pending; checkpoint progress and let the job finish")
            if (
                status == "complete"
                and db.execute(
                    "SELECT 1 FROM mathematical_reviews WHERE organization_id=? AND topic_id=? AND status IN ('pending','running')",
                    (row["organization_id"], row["topic_id"]),
                ).fetchone()
            ):
                raise StoreError(409, "independent review is still pending; checkpoint progress and let the review finish")
            db.execute(
                "UPDATE agent_goals SET status=?,reason=?,updated_at=? WHERE id=?", (status, summary.strip(), int(time.time()), goal["id"])
            )
            self.event(db, goal["id"], None, {"type": "checkpoint", "run_id": row["id"], "summary": summary, "status": status})
            return {"goal_id": goal["id"], "status": status, "summary": summary}

    async def dispatch(self, provider, stopped=None):
        """Schedule one exact turn per goal; provider failures never replay automatically."""
        with self.store.transaction() as db:
            candidates = [dict(row) for row in db.execute("SELECT * FROM agent_goals WHERE status='active' "
                                                        "AND model NOT LIKE 'bot/%' ORDER BY created_at LIMIT 8")]
        for goal in candidates:
            try:
                self._check(goal)
                pending = asyncio.create_task(provider.quotes([goal["model"]]))
                try:
                    while not pending.done():
                        await asyncio.wait({pending}, timeout=0.1)
                        if stopped and stopped():
                            return
                    quote = (await pending)[0]
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                if not quote.supports_tools:
                    raise StoreError(409, "Choose a model with tool use for an autonomous research goal.")
                with self.store.transaction(write=True) as db:
                    current = _row(db.execute("SELECT * FROM agent_goals WHERE id=?", (goal["id"],)).fetchone())
                    if current["status"] != "active" or current["run_id"] != goal["run_id"]:
                        continue
                    self._check_in_transaction(db, current)
                    if db.execute(
                        "SELECT 1 FROM llm_requests q JOIN llm_runs r ON r.request_id=q.id "
                        "WHERE q.organization_id=? AND q.topic_id=? AND r.status IN ('queued','running')",
                        (goal["organization_id"], goal["topic_id"]),
                    ).fetchone():
                        continue
                    if db.execute(
                        "SELECT 1 FROM mathematical_reviews WHERE organization_id=? AND topic_id=? AND status IN ('pending','running')",
                        (goal["organization_id"], goal["topic_id"]),
                    ).fetchone():
                        continue
                    remaining = current["budget_microusd"] - self.view(db, current)["used_or_held_microusd"]
                    if db.execute(
                        "SELECT 1 FROM compute_jobs WHERE organization_id=? AND topic_id=? AND status IN ('queued','running')",
                        (goal["organization_id"], goal["topic_id"]),
                    ).fetchone():
                        continue
                    # The first goal message establishes a durable source boundary.
                    through = db.execute(
                        "SELECT MAX(sequence) FROM messages WHERE organization_id=? AND topic_id=?",
                        (goal["organization_id"], goal["topic_id"]),
                    ).fetchone()[0]
                    if through is None:
                        raise StoreError(409, "send the goal objective to this topic before starting its goal")
                    parameters = self.inference.parameters([goal["model"]], None, 50, through, effort=goal["effort"])
                    runs = self.inference._enqueue(
                        db,
                        goal["owned_by"],
                        goal["organization_id"],
                        goal["topic_id"],
                        "goal-" + goal["id"] + "-" + str(current["turns"]),
                        parameters,
                        [quote],
                    )
                    if runs[0]["reserved_microusd"] > remaining:
                        raise StoreError(409, "goal cost budget cannot cover the next turn")
                    db.execute(
                        "UPDATE agent_goals SET run_id=?,turns=turns+1,updated_at=? WHERE id=?",
                        (runs[0]["id"], int(time.time()), goal["id"]),
                    )
                    self.event(db, goal["id"], None, {"type": "turn", "run_id": runs[0]["id"], "turn": current["turns"] + 1})
            except StoreError as exc:
                if exc.status in {425, 429}:
                    continue
                self.stop(goal["id"], "budget_limited" if "budget" in exc.message else "blocked", exc.message)
            except Exception as exc:
                if database_busy(exc):
                    raise
                self.stop(goal["id"], "blocked", "model quote unavailable; resume after reviewing the provider")

    def _check(self, goal):
        with self.store.transaction() as db:
            current = _row(db.execute("SELECT * FROM agent_goals WHERE id=?", (goal["id"],)).fetchone())
            self._check_in_transaction(db, current)

    def _check_in_transaction(self, db, goal):
        from .topic_agents import TopicAgents

        if db.execute(
            "SELECT 1 FROM compute_jobs WHERE organization_id=? AND topic_id=? AND status='outcome_unknown'",
            (goal["organization_id"], goal["topic_id"]),
        ).fetchone():
            raise StoreError(409, "computation outcome is unknown; reconcile cleanup before resuming this goal")
        if db.execute(
            "SELECT 1 FROM llm_runs r JOIN agent_goal_events e ON json_extract(e.event_json,'$.run_id')=r.id "
            "WHERE e.goal_id=? AND json_extract(e.event_json,'$.type')='review' AND r.accounting_state='uncertain'",
            (goal["id"],),
        ).fetchone():
            raise StoreError(409, "independent review charge is unknown; reconcile it before resuming")
        if goal["status"] != "active":
            raise StoreError(425, "goal is no longer active")
        if goal["deadline_at"] <= int(time.time()) or goal["turns"] >= goal["max_turns"] or goal["tool_calls"] >= goal["max_tool_calls"]:
            raise StoreError(409, "goal time, turn, or tool budget exhausted")
        if goal["run_id"] and TopicAgents.pending(db, goal["run_id"]):
            raise StoreError(425, "delegated agents are still returning results")
        self.store._topic_access(db, goal["owned_by"], goal["organization_id"], goal["topic_id"], write=True)
        if goal["run_id"]:
            run = _row(db.execute("SELECT * FROM llm_runs WHERE id=?", (goal["run_id"],)).fetchone())
            if run["status"] in {"queued", "running"}:
                raise StoreError(425, "goal turn still running")
            if run["accounting_state"] == "uncertain":
                raise StoreError(409, "previous provider charge is unknown; reconcile it before continuing")
            pending_tools = db.execute(
                "SELECT name,status,arguments_json,result_json FROM research_tool_receipts "
                "WHERE run_id=? AND status IN ('requested','uncertain','failed')",
                (run["id"],),
            ).fetchall()
            if any(receipt["status"] != "failed" or not failed_read(receipt) for receipt in pending_tools):
                raise StoreError(409, "previous tool outcome needs review before continuing")
            recoverable = (
                run["status"] == "failed" and run["accounting_state"] in {"settled", "released"}
                and run["error_code"] in RECOVERABLE_TURN_ERRORS
            )
            if run["status"] != "succeeded" and not recoverable:
                raise StoreError(409, "previous goal turn needs review; this failure cannot continue automatically")

    def stop(self, goal, status, reason):
        with self.store.transaction(write=True) as db:
            changed = db.execute(
                "UPDATE agent_goals SET status=?,reason=?,updated_at=? WHERE id=? AND status='active'",
                (status, reason, int(time.time()), goal),
            ).rowcount
            if changed:
                self.event(db, goal, None, {"type": "status", "status": status, "reason": reason})
