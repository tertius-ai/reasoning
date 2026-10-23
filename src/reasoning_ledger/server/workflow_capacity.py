"""Readable workflow admission accounting and narrowly scoped retained-run inspection."""

from .store import StoreError

_FROM = (
    " FROM workflow_runs r JOIN topics t ON t.organization_id=r.organization_id AND t.id=r.topic_id "
    "JOIN channels c ON c.organization_id=t.organization_id AND c.id=t.channel_id "
    "JOIN projects p ON p.organization_id=c.organization_id AND p.id=c.project_id "
    "JOIN workflow_versions v ON v.organization_id=r.organization_id AND v.workflow_id=r.workflow_id "
    "AND v.version=r.definition_version JOIN users u ON u.id=r.requested_by "
)
_FIELDS = (
    "r.id,r.topic_id,r.requested_by,r.created_at,r.status,r.revision,r.capacity_released,r.cancel_requested,r.finished_at,"
    "json_extract(v.definition_json,'$.name') AS workflow_name,u.name AS requested_by_name,"
    "t.name AS topic_name,c.name AS channel_name,p.name AS project_name,"
    "CASE WHEN t.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL OR p.deleted_at IS NOT NULL THEN 1 ELSE 0 END AS topic_deleted"
)


class WorkflowCapacity:
    def __init__(self, state, limit):
        self.state, self.store, self.limit = state, state.store, limit

    def visibility(self, db, user, org):
        role = self.store._require(db, user, org)
        if role in {"owner", "admin"}:
            return role, "", ()
        return (
            role,
            (
                " AND t.deleted_at IS NULL AND c.deleted_at IS NULL AND p.deleted_at IS NULL "
                "AND (p.visibility='organization' OR EXISTS (SELECT 1 FROM project_memberships pm "
                "WHERE pm.organization_id=p.organization_id AND pm.project_id=p.id AND pm.user_id=?))"
            ),
            (user,),
        )

    def entry(self, db, user, org, identity, *, write=False):
        role, clause, values = self.visibility(db, user, org)
        row = db.execute("SELECT " + _FIELDS + _FROM + "WHERE r.organization_id=? AND r.id=?" + clause, (org, identity, *values)).fetchone()
        if not row:
            raise StoreError(404, "workflow inspection unavailable")
        if not row["topic_deleted"]:
            self.store._topic_access(db, user, org, row["topic_id"], write=write)
        elif role not in {"owner", "admin"}:
            raise StoreError(404, "workflow inspection unavailable")
        # Deleted topics grant only this operational summary and administrator slot release.
        # Ordinary transcript, workflow reads, retries and execution keep their normal fences.
        return role, dict(row)

    def summary(self, user, org):
        with self.store.transaction() as db:
            _, clause, values = self.visibility(db, user, org)
            rows = db.execute(
                "SELECT "
                + _FIELDS
                + _FROM
                + "WHERE r.organization_id=? AND r.finished_at IS NULL"
                + clause
                + " ORDER BY r.capacity_released,r.created_at DESC,r.id DESC LIMIT 201",
                (org, *values),
            ).fetchall()
            visible = []
            for row in rows[:200]:
                if not row["topic_deleted"]:
                    try:
                        self.store._topic_access(db, user, org, row["topic_id"])
                    except StoreError as exc:
                        if exc.status not in {403, 404}:
                            raise
                        continue
                visible.append(dict(row))
            used = db.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE organization_id=? AND finished_at IS NULL AND capacity_released=0", (org,)
            ).fetchone()[0]
            released = db.execute(
                "SELECT COUNT(*)" + _FROM + "WHERE r.organization_id=? AND r.finished_at IS NULL AND r.capacity_released=1" + clause,
                (org, *values),
            ).fetchone()[0]
            visible_used = sum(not row["capacity_released"] for row in visible)
            return {
                "limit": self.limit,
                "in_use": used,
                "available": max(0, self.limit - used),
                "runs": visible,
                "hidden_in_use": max(0, used - visible_used),
                "released_count": released,
                "truncated": len(rows) > 200,
            }

    def inspect(self, user, org, identity):
        with self.store.transaction() as db:
            role, result = self.entry(db, user, org, identity)
            run = self.state._run(db, org, identity)
            details, active, missing = [], False, False
            for node in self.state._nodes(db, run).values():
                active |= node["status"] == "running"
                children = []
                for field, table, kind in (
                    ("bot_run_id", "bot_runs", "bot"),
                    ("compute_job_id", "compute_jobs", "compute"),
                    ("inference_run_id", "llm_runs", "model"),
                ):
                    if not node[field]:
                        continue
                    columns = "status,accounting_state" if table == "llm_runs" else "status"
                    child = db.execute(f"SELECT {columns} FROM {table} WHERE organization_id=? AND id=?", (org, node[field])).fetchone()
                    missing |= child is None
                    active |= child is not None and child["status"] in {"queued", "running", "waiting_approval"}
                    children.append(
                        {
                            "id": node[field],
                            "kind": kind,
                            "status": child["status"] if child else "record_unavailable",
                            "accounting_state": child["accounting_state"] if child and table == "llm_runs" else None,
                        }
                    )
                details.append({"node_id": node["node_id"], "kind": node["kind"], "status": node["status"], "children": children})
            reason = (
                "already_released"
                if run["capacity_released"]
                else "administrator_required"
                if role not in {"owner", "admin"}
                else "not_uncertain"
                if run["status"] != "outcome_unknown"
                else "missing_child_record"
                if missing
                else "active_children"
                if active
                else None
            )
            return {"run": result, "nodes": details, "can_release": reason is None, "release_blocked_reason": reason}
