"""Durable scoped execution, fenced leases and shared ancestor resource budgets."""

from __future__ import annotations

import hmac
import json
import time

from .research_context import encoded
from .security import digest, token
from .store import StoreError, _id, _row
from .work_contracts import WorkSpecification
from .work_controller_schema import wake
from .work_inputs import require_assigned
from .work_job_contracts import SchedulerPolicy
from .work_prompts import pin
from .work_store import WorkStore
from .work_tools import catalog as tool_catalog

LEASE_SECONDS = 60


class WorkJobs:
    def __init__(self, store):
        self.store, self.work = store, WorkStore(store)

    @staticmethod
    def _lease_token(credential, attempt):
        return hmac.digest(credential.encode(), ("reasoning-work-lease-v1:" + attempt).encode(), "sha256").hex()

    @staticmethod
    def _locked(db, query, parameters, *, table=None):
        if getattr(db, "dialect", None) == "postgresql":
            query += " FOR UPDATE" + (" OF " + table if table else "")
        return db.execute(query, parameters)

    def _worker(self, db, credential, *, lock=False):
        if not isinstance(credential, str) or not credential.startswith("rnaw_") or len(credential) != 48:
            raise StoreError(401, "invalid work worker credential")
        query = "SELECT * FROM work_workers WHERE token_hash=?"
        if lock and getattr(db, "dialect", None) == "postgresql":
            query += " FOR SHARE"
        result = db.execute(query, (digest(credential),)).fetchone()
        if result is None:
            raise StoreError(401, "invalid work worker credential")
        row = dict(result)
        if row["revoked_at"] is not None:
            raise StoreError(401, "work worker revoked")
        return row

    def enroll(self, user, org, body):
        with self.store.transaction(write=True, scope=f"work-dispatch:{org}") as db:
            self.store._require(db, user, org, admin=True)
            for project in body.project_ids:
                self.store._project_access(db, user, org, project, write=True)
            identity, credential, now = _id(), "rnaw_" + token(), int(time.time())
            db.execute(
                "INSERT INTO work_workers VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    identity,
                    org,
                    user,
                    body.name,
                    digest(credential),
                    encoded(body.roles),
                    body.max_parallel,
                    body.human_slots,
                    body.review_slots,
                    now,
                ),
            )
            for project in body.project_ids:
                db.execute("INSERT INTO work_worker_projects VALUES(?,?,?)", (org, identity, project))
            self.store._audit(db, org, user, "work.worker.enroll", identity)
            return {
                "id": identity,
                "token": credential,
                "name": body.name,
                "project_ids": body.project_ids,
                "roles": body.roles,
                "max_parallel": body.max_parallel,
                "human_slots": body.human_slots,
                "review_slots": body.review_slots,
            }

    def policy(self, user, org, body=None):
        with self.store.transaction(write=body is not None, scope=f"work-dispatch:{org}") as db:
            self.store._require(db, user, org, admin=body is not None)
            if body is not None:
                db.execute(
                    "INSERT INTO work_scheduler_policies VALUES(?,?,?,?) ON CONFLICT(organization_id) "
                    "DO UPDATE SET max_parallel=excluded.max_parallel,human_slots=excluded.human_slots,review_slots=excluded.review_slots",
                    (org, body.max_parallel, body.human_slots, body.review_slots),
                )
                self.store._audit(db, org, user, "work.scheduler.policy", org)
            row = db.execute(
                "SELECT max_parallel,human_slots,review_slots FROM work_scheduler_policies WHERE organization_id=?", (org,)
            ).fetchone()
            return dict(row) if row else SchedulerPolicy().model_dump()

    @staticmethod
    def _lanes(db, org, limits, worker=None):
        sql, args = (
            "SELECT lane,COUNT(*) AS active FROM work_jobs WHERE organization_id=? AND status IN ('leased','outcome_unknown')",
            [org],
        )
        if worker:
            sql += " AND worker_id=?"
            args.append(worker)
        counts = {row["lane"]: row["active"] for row in db.execute(sql + " GROUP BY lane", args)}
        count = sum(counts.values())
        reserved = {"human": limits["human_slots"], "review": limits["review_slots"]}
        return {
            lane
            for lane in ("human", "review", "research")
            if count + sum(max(0, slots - counts.get(other, 0)) for other, slots in reserved.items() if other != lane)
            < limits["max_parallel"]
        }

    def revoke(self, user, org, worker):
        with self.store.transaction(write=True, scope=f"work-dispatch:{org}") as db:
            self.store._require(db, user, org, admin=True)
            row = _row(db.execute("SELECT * FROM work_workers WHERE organization_id=? AND id=?", (org, worker)).fetchone())
            now = int(time.time())
            db.execute("UPDATE work_workers SET revoked_at=? WHERE id=?", (now, row["id"]))
            # Revocation prevents new mutations immediately, but does not invent
            # a known outcome for work already handed to an external process.
            db.execute("UPDATE work_jobs SET cancel_requested=1,lease_expires_at=? WHERE worker_id=? AND status='leased'", (now, worker))
            self.store._audit(db, org, user, "work.worker.revoke", worker)

    def control(self, user, org, project, program, body):
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            root = self.work._program(db, user, org, project, program, write=True)
            if root["head_revision"] != body.expected_revision:
                raise StoreError(409, "read the current specification before controlling work")
            if root["status"] == "complete":
                raise StoreError(409, "completed work requires explicit reopening")
            if root["status"] == "cancelled" and body.status != "cancelled":
                raise StoreError(409, "cancelled work requires explicit reopening")
            db.execute("UPDATE work_programs SET status=?,updated_at=? WHERE id=?", (body.status, int(time.time()), program))
            if body.status == "cancelled":
                db.execute(
                    "UPDATE work_jobs SET status='cancelled',finished_at=? WHERE program_id=? AND status='queued'",
                    (int(time.time()), program),
                )
                db.execute("UPDATE work_tasks SET status='cancelled' WHERE program_id=? AND status IN ('pending','ready')", (program,))
            if body.status in {"paused", "cancelled"}:
                db.execute("UPDATE work_jobs SET cancel_requested=1 WHERE program_id=? AND status='leased'", (program,))
            self.work._event(db, org, program, user, {"type": "control", "status": body.status, "reason": body.reason})
            wake(db, program)
            return {"id": program, "status": body.status}

    def enqueue(self, user, org, project, program, task, body):
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            return self._enqueue(db, user, org, project, program, task, body)

    def _enqueue(self, db, user, org, project, program, task, body):
        """Caller owns the program transaction and any local-operation receipt."""
        root = self.work._program(db, user, org, project, program, write=True)
        record = _row(
            db.execute("SELECT * FROM work_tasks WHERE organization_id=? AND program_id=? AND id=?", (org, program, task)).fetchone()
        )
        if record["epoch"] != body.expected_epoch:
            raise StoreError(409, "task epoch changed")
        pin(db, org, program, task, WorkSpecification.model_validate_json(record["body_json"]))
        lane = body.lane or ("review" if record["role"] in {"critic", "specification_reviewer"} else "research")
        old = db.execute("SELECT * FROM work_jobs WHERE task_id=? AND task_epoch=?", (task, body.expected_epoch)).fetchone()
        if old:
            if old["lane"] != lane:
                raise StoreError(409, "an existing job cannot change lanes")
            return self._view(old)
        if root["status"] in {"complete", "cancelled"} or record["status"] not in {"pending", "ready"}:
            raise StoreError(409, "this task cannot be queued")
        identity, now = _id(), int(time.time())
        db.execute(
            "INSERT INTO work_jobs(id,organization_id,program_id,task_id,task_epoch,lane,status,created_at) VALUES(?,?,?,?,?,?,'queued',?)",
            (identity, org, program, task, body.expected_epoch, lane, now),
        )
        db.execute("UPDATE work_tasks SET status='ready',updated_at=? WHERE id=?", (now, task))
        self.work._event(db, org, program, user, {"type": "queued", "job_id": identity, "lane": lane}, task)
        return self._view(db.execute("SELECT * FROM work_jobs WHERE id=?", (identity,)).fetchone())

    @staticmethod
    def _view(row):
        return {
            key: row[key]
            for key in (
                "id",
                "sequence",
                "program_id",
                "task_id",
                "task_epoch",
                "lane",
                "status",
                "fence",
                "worker_id",
                "cancel_requested",
                "lease_expires_at",
                "created_at",
                "finished_at",
            )
        }

    def read(self, user, org, project, program, job):
        with self.store.transaction() as db:
            self.work._program(db, user, org, project, program)
            row = _row(
                db.execute("SELECT * FROM work_jobs WHERE organization_id=? AND program_id=? AND id=?", (org, program, job)).fetchone()
            )
            result = db.execute("SELECT body_json,sha256 FROM work_results WHERE job_id=? AND fence=?", (job, row["fence"])).fetchone()
            return self._view(row) | {
                "result": json.loads(result["body_json"]) if result else None,
                "result_sha256": result["sha256"] if result else None,
            }

    def effects(self, user, org, project, program, job, after="", limit=50):
        if not 1 <= limit <= 100:
            raise StoreError(400, "page size must be between 1 and 100")
        with self.store.transaction() as db:
            self.work._program(db, user, org, project, program)
            if not db.execute("SELECT 1 FROM work_jobs WHERE organization_id=? AND program_id=? AND id=?", (org, program, job)).fetchone():
                raise StoreError(404, "job unavailable")
            rows = db.execute(
                "SELECT id,fence,kind,request_sha256,model,image_id,tool,held_microusd,held_cpu_seconds,actual_microusd,"
                "actual_cpu_seconds,state,created_at,finished_at FROM work_effects WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job, after, limit + 1),
            ).fetchall()
            return {"items": [dict(row) for row in rows[:limit]], "next": rows[limit - 1]["id"] if len(rows) > limit else None}

    def cancel(self, user, org, project, program, job):
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            self.work._program(db, user, org, project, program, write=True)
            row = _row(
                db.execute("SELECT * FROM work_jobs WHERE organization_id=? AND program_id=? AND id=?", (org, program, job)).fetchone()
            )
            if row["status"] == "queued":
                db.execute("UPDATE work_jobs SET status='cancelled',cancel_requested=1,finished_at=? WHERE id=?", (int(time.time()), job))
                db.execute("UPDATE work_tasks SET status='cancelled' WHERE id=? AND epoch=?", (row["task_id"], row["task_epoch"]))
            elif row["status"] == "leased":
                db.execute("UPDATE work_jobs SET cancel_requested=1 WHERE id=?", (job,))
            self.work._event(db, org, program, user, {"type": "cancel_requested", "job_id": job}, row["task_id"])

    @staticmethod
    def _account_delta(db, row, usd, cpu):
        for scope in db.execute("SELECT scope_kind,scope_id FROM work_effect_scopes WHERE effect_id=?", (row["id"],)).fetchall():
            db.execute(
                "UPDATE work_resource_accounts SET microusd=microusd+?,cpu_seconds=cpu_seconds+? WHERE scope_kind=? AND scope_id=?",
                (usd - row["held_microusd"], cpu - row["held_cpu_seconds"], scope["scope_kind"], scope["scope_id"]),
            )

    def reconcile(self, user, org, project, program, effect, body):
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            root = self.work._program(db, user, org, project, program, write=True)
            role = self.store._require(db, user, org)
            if user != root["owned_by"] and role not in {"owner", "admin"}:
                raise StoreError(403, "only the program owner or an administrator can reconcile effects")
            row = _row(db.execute("SELECT * FROM work_effects WHERE program_id=? AND id=?", (program, effect)).fetchone())
            if db.execute("SELECT 1 FROM work_computations WHERE effect_id=?", (effect,)).fetchone():
                raise StoreError(403, "reconcile the linked compute job; its server receipt owns work accounting")
            if row["kind"] == "inference":
                from .inference_credit import lock as credit_lock

                credit_lock(self.store, db, org)
            if row["request_sha256"] != body.expected_request_sha256:
                raise StoreError(409, "effect request digest does not match")
            receipt = encoded({"source": "human_reconciliation", **body.model_dump()})
            if row["state"] in {"settled", "rejected"}:
                if row["receipt_json"] != receipt:
                    raise StoreError(409, "effect is already reconciled with different evidence")
                return {"id": effect, "state": row["state"]}
            if db.execute(
                "SELECT 1 FROM work_jobs WHERE id=? AND status='leased' AND lease_expires_at>?", (row["job_id"], int(time.time()))
            ).fetchone():
                raise StoreError(409, "stop or fence the live worker before reconciling its effect")
            db.execute(
                "UPDATE work_effects SET state=?,actual_microusd=?,actual_cpu_seconds=?,receipt_json=?,finished_at=? WHERE id=?",
                (body.state, body.actual_microusd, body.actual_cpu_seconds, receipt, int(time.time()), effect),
            )
            self._account_delta(db, row, body.actual_microusd, body.actual_cpu_seconds)
            self.work._event(
                db, org, program, user, {"type": "effect_reconciled", "effect_id": effect, "state": body.state}, row["task_id"]
            )
            return {"id": effect, "state": body.state}

    def reopen(self, user, org, project, program, task, body):
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            root = self.work._program(db, user, org, project, program, write=True)
            row = _row(
                db.execute("SELECT * FROM work_tasks WHERE organization_id=? AND program_id=? AND id=?", (org, program, task)).fetchone()
            )
            if row["epoch"] != body.expected_epoch:
                raise StoreError(409, "task epoch changed")
            if root["status"] in {"cancelled", "complete"} or row["status"] not in {
                "blocked",
                "cancelled",
                "review",
                "outcome_unknown",
                "complete",
            }:
                raise StoreError(409, "this task cannot be reopened")
            if db.execute(
                "SELECT 1 FROM work_jobs WHERE task_id=? AND status='leased' AND lease_expires_at>?", (task, int(time.time()))
            ).fetchone():
                raise StoreError(409, "stop or fence the active worker before reopening")
            if db.execute("SELECT 1 FROM work_effects WHERE task_id=? AND state IN ('intent','unknown') LIMIT 1", (task,)).fetchone():
                raise StoreError(409, "reconcile uncertain effects before authorizing another attempt")
            db.execute("UPDATE work_tasks SET epoch=epoch+1,status='pending',updated_at=? WHERE id=?", (int(time.time()), task))
            db.execute(
                "UPDATE work_jobs SET status='cancelled',lease_hash=NULL,cancel_requested=1,finished_at=? "
                "WHERE task_id=? AND status IN ('outcome_unknown','leased')",
                (int(time.time()), task),
            )
            self.work._event(db, org, program, user, {"type": "reopened", "epoch": row["epoch"] + 1, "reason": body.reason}, task)
            return {"id": task, "epoch": row["epoch"] + 1, "status": "pending"}

    @staticmethod
    def _program_lock(store, db, program):
        if store._postgres:
            store._postgres.lock(db.native, f"work-program:{program}")

    def _expire(self, db, org, stopped=lambda: False):
        now = int(time.time())
        candidates = db.execute(
            "SELECT id,program_id FROM work_jobs WHERE organization_id=? AND status='leased' "
            "AND lease_expires_at<=? ORDER BY program_id,id",
            (org, now),
        ).fetchall()
        for candidate in candidates:
            if stopped():
                break
            self._program_lock(self.store, db, candidate["program_id"])
            row = self._locked(db, "SELECT * FROM work_jobs WHERE id=?", (candidate["id"],)).fetchone()
            if row["status"] != "leased" or row["lease_expires_at"] > now:
                continue
            effects = db.execute("SELECT 1 FROM work_effects WHERE job_id=? AND fence=? LIMIT 1", (row["id"], row["fence"])).fetchone()
            status = "outcome_unknown" if effects else "cancelled" if row["cancel_requested"] else "queued"
            db.execute("UPDATE work_jobs SET status=?,lease_hash=NULL WHERE id=?", (status, row["id"]))
            db.execute("UPDATE work_effects SET state='unknown' WHERE job_id=? AND fence=? AND state='intent'", (row["id"], row["fence"]))
            db.execute(
                "UPDATE work_tasks SET status=?,updated_at=? WHERE id=? AND epoch=?",
                ("ready" if status == "queued" else status, now, row["task_id"], row["task_epoch"]),
            )
            self.work._event(
                db, org, row["program_id"], None, {"type": "lease_expired", "job_id": row["id"], "status": status}, row["task_id"]
            )
            wake(db, row["program_id"])

    def _claim_view(self, db, credential, worker, row, claim):
        dispatched = (
            row["status"] == "leased"
            and row["fence"] == claim["fence"]
            and row["worker_id"] == worker["id"]
            and row["lease_expires_at"] > int(time.time())
            and not row["cancel_requested"]
        )
        packet = None
        if dispatched:
            try:
                packet = self._packet(db, row)
            except StoreError as exc:
                if exc.status not in {403, 404}:
                    raise
                dispatched = False
        return self._view(row) | {
            "dispatch": dispatched,
            "attempt_id": claim["attempt_id"],
            "fence": claim["fence"],
            "lease": self._lease_token(credential, claim["attempt_id"]),
            "lease_expires_at": row["lease_expires_at"],
            "packet": packet,
        }

    def _authorized_task(self, db, row):
        task = _row(db.execute("SELECT * FROM work_tasks WHERE id=?", (row["task_id"],)).fetchone())
        root = db.execute("SELECT * FROM work_programs WHERE id=?", (row["program_id"],)).fetchone()
        self.work._program(db, task["requested_by"], row["organization_id"], root["project_id"], root["id"], write=True)
        if not db.execute(
            "SELECT 1 FROM work_worker_projects WHERE worker_id=? AND project_id=?", (row["worker_id"], root["project_id"])
        ).fetchone():
            raise StoreError(403, "worker project grant no longer authorizes this task")
        worker = db.execute("SELECT roles_json FROM work_workers WHERE id=?", (row["worker_id"],)).fetchone()
        if task["role"] not in json.loads(worker["roles_json"]):
            raise StoreError(403, "worker role grant no longer authorizes this task")
        return task, root

    def _packet(self, db, row):
        task, root = self._authorized_task(db, row)
        spec = WorkSpecification.model_validate_json(task["body_json"])
        root_spec = WorkSpecification.model_validate_json(
            db.execute(
                "SELECT body_json FROM work_specifications WHERE program_id=? AND revision=?", (root["id"], root["head_revision"])
            ).fetchone()[0]
        )
        tool_grants = set(spec.resources.allowed_tools) & set(root_spec.resources.allowed_tools)
        prompt = pin(db, row["organization_id"], row["program_id"], task["id"], spec)
        compact = db.execute("SELECT * FROM work_compactions WHERE job_id=? ORDER BY sequence DESC LIMIT 1", (row["id"],)).fetchone()
        payload = {
            "task_id": task["id"],
            "task_epoch": task["epoch"],
            "root_revision": task["root_revision"],
            "authorization_revision": root["head_revision"],
            "specification": json.loads(task["body_json"]),
            "specification_sha256": task["sha256"],
            "prompt": json.loads(prompt["body_json"]),
            "prompt_sha256": prompt["sha256"],
            "tools": [item for item in tool_catalog() if item["function"]["name"] in tool_grants] if tool_grants else [],
            "discovered_input_count": db.execute("SELECT COUNT(*) FROM work_input_bindings WHERE task_id=?", (task["id"],)).fetchone()[0],
            "compaction": None
            if compact is None
            else {
                "sequence": compact["sequence"],
                "fence": compact["fence"],
                "note": json.loads(compact["body_json"]),
                "sha256": compact["sha256"],
            },
        }
        stage = db.execute(
            "SELECT s.phase,c.id,t.body_json,t.sha256 FROM work_controller_steps s "
            "JOIN work_controllers c ON c.id=s.controller_id JOIN work_tasks t ON t.id=c.target_task WHERE s.job_id=?",
            (row["id"],),
        ).fetchone()
        if stage:
            payload["controller"] = {
                "id": stage["id"],
                "phase": stage["phase"],
                "target_specification": json.loads(stage["body_json"]),
                "target_sha256": stage["sha256"],
            }
        if stage and stage["phase"] in {"specification", "checking", "review"}:
            from .work_controllers import review_schema

            payload["output_schema"] = review_schema()
        return payload | {"sha256": digest(encoded(payload))}

    def claim(self, credential, claim_id):
        with self.store.transaction() as db:
            org = self._worker(db, credential)["organization_id"]
        with self.store.transaction(write=True, scope=f"work-dispatch:{org}") as db:
            worker = self._worker(db, credential, lock=True)
            self._expire(db, org)
            old = db.execute("SELECT * FROM work_claims WHERE worker_id=? AND claim_id=?", (worker["id"], claim_id)).fetchone()
            if old:
                row = db.execute("SELECT * FROM work_jobs WHERE id=?", (old["job_id"],)).fetchone()
                return self._claim_view(db, credential, worker, row, old)
            row = db.execute(
                "SELECT max_parallel,human_slots,review_slots FROM work_scheduler_policies WHERE organization_id=?", (org,)
            ).fetchone()
            limits = dict(row) if row else SchedulerPolicy().model_dump()
            lanes = self._lanes(db, org, limits) & self._lanes(db, org, worker, worker["id"])
            if not lanes:
                return None
            roles = json.loads(worker["roles_json"])
            placeholders = ",".join("?" for _ in roles)
            lane_placeholders = ",".join("?" for _ in lanes)
            sql = (
                "SELECT j.* FROM work_jobs j JOIN work_tasks t ON t.id=j.task_id JOIN work_programs p ON p.id=j.program_id "
                "JOIN work_worker_projects g ON g.project_id=p.project_id AND g.worker_id=? "
                "LEFT JOIN work_dispatch_counts lc ON lc.scope_kind='lane' AND lc.scope_id=(j.organization_id||':'||j.lane) "
                "LEFT JOIN work_dispatch_counts xc ON xc.scope_kind='project' AND xc.scope_id=p.project_id "
                "LEFT JOIN work_dispatch_counts pc ON pc.scope_kind='program' AND pc.scope_id=p.id "
                "LEFT JOIN work_dispatch_counts bc ON bc.scope_kind='branch' AND bc.scope_id=COALESCE(t.parent_id,t.id) "
                "WHERE j.organization_id=? AND j.status='queued' AND p.status='active' AND t.epoch=j.task_epoch "
                "AND (SELECT COUNT(*) FROM work_jobs active WHERE active.program_id=p.id "
                "AND active.status IN ('leased','outcome_unknown'))<p.execution_slots "
                f"AND j.lane IN ({lane_placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM work_task_ancestors a JOIN work_tasks owner ON owner.id=a.ancestor_id "
                "WHERE a.task_id=t.id AND (SELECT COUNT(*) FROM work_task_ancestors d JOIN work_jobs live ON live.task_id=d.task_id "
                "WHERE d.ancestor_id=a.ancestor_id AND live.status IN ('leased','outcome_unknown')) "
                ">=json_extract(owner.body_json,'$.resources.max_concurrency')) "
                f"AND t.role IN ({placeholders}) AND NOT EXISTS (SELECT 1 FROM work_dependencies d "
                "JOIN work_tasks prerequisite ON prerequisite.id=d.prerequisite_id "
                "WHERE d.task_id=t.id AND prerequisite.status<>'complete') "
                "ORDER BY COALESCE(lc.dispatched,0),CASE j.lane WHEN 'human' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,"
                "COALESCE(xc.dispatched,0),COALESCE(pc.dispatched,0),COALESCE(bc.dispatched,0),j.sequence LIMIT 1"
            )
            while True:
                row = db.execute(sql, (worker["id"], org, *sorted(lanes), *roles)).fetchone()
                if row is None:
                    return None
                row = dict(row)
                self._program_lock(self.store, db, row["program_id"])
                # Lock order is dispatch -> program -> job on every mutation path.
                locked = "SELECT * FROM work_jobs WHERE id=? AND status='queued'"
                if getattr(db, "dialect", None) == "postgresql":
                    locked += " FOR UPDATE SKIP LOCKED"
                current = db.execute(locked, (row["id"],)).fetchone()
                if current is None:
                    return None
                row = dict(current)
                root = db.execute(
                    "SELECT p.*,v.body_json FROM work_programs p JOIN work_specifications v "
                    "ON v.program_id=p.id AND v.revision=p.head_revision WHERE p.id=?",
                    (row["program_id"],),
                ).fetchone()
                limits = WorkSpecification.model_validate_json(root["body_json"]).resources
                if root["status"] != "active":
                    continue
                active = db.execute(
                    "SELECT COUNT(*) FROM work_jobs WHERE program_id=? AND status IN ('leased','outcome_unknown')", (row["program_id"],)
                ).fetchone()[0]
                if active >= limits.max_concurrency:
                    continue
                task = db.execute("SELECT * FROM work_tasks WHERE id=?", (row["task_id"],)).fetchone()
                specification = WorkSpecification.model_validate_json(task["body_json"])
                try:
                    self.work._program(db, task["requested_by"], org, root["project_id"], row["program_id"], write=True)
                    self.work._policy(db, org, specification)
                    if not limits.contains(specification.resources):
                        raise StoreError(409, "queued task exceeds current root authorization")
                    if limits.deadline_at is not None and limits.deadline_at <= int(time.time()):
                        raise StoreError(409, "work authorization deadline has passed")
                except StoreError as exc:
                    if exc.status not in {403, 404, 409}:
                        raise
                    # A revoked or obsolete assignment must not poison the queue
                    # for unrelated projects. It needs explicit human reopening.
                    db.execute("UPDATE work_jobs SET status='failed',finished_at=? WHERE id=?", (int(time.time()), row["id"]))
                    db.execute("UPDATE work_tasks SET status='blocked',updated_at=? WHERE id=?", (int(time.time()), task["id"]))
                    self.work._event(
                        db,
                        org,
                        root["id"],
                        None,
                        {"type": "dispatch_refused", "job_id": row["id"], "reason": "assignment no longer authorized"},
                        task["id"],
                    )
                    continue
                break
            pin(db, org, row["program_id"], task["id"], specification)
            attempt, now, fence = _id(), int(time.time()), row["fence"] + 1
            lease = self._lease_token(credential, attempt)
            db.execute(
                "UPDATE work_jobs SET status='leased',worker_id=?,attempt_id=?,fence=?,lease_hash=?,lease_expires_at=? WHERE id=?",
                (worker["id"], attempt, fence, digest(lease), now + LEASE_SECONDS, row["id"]),
            )
            db.execute("UPDATE work_tasks SET status='running',updated_at=? WHERE id=?", (now, row["task_id"]))
            db.execute("INSERT INTO work_claims VALUES(?,?,?,?,?,?)", (worker["id"], claim_id, row["id"], attempt, fence, now))
            db.execute("INSERT INTO work_scheduler_clocks VALUES(?,0) ON CONFLICT DO NOTHING", (org,))
            tick = db.execute("UPDATE work_scheduler_clocks SET tick=tick+1 WHERE organization_id=? RETURNING tick", (org,)).fetchone()[0]
            for kind, identity in (
                ("lane", org + ":" + row["lane"]),
                ("project", root["project_id"]),
                ("program", row["program_id"]),
                ("branch", task["parent_id"] or task["id"]),
            ):
                db.execute(
                    "INSERT INTO work_dispatch_counts VALUES(?,?,?) ON CONFLICT(scope_kind,scope_id) "
                    "DO UPDATE SET dispatched=excluded.dispatched",
                    (kind, identity, tick),
                )
            db.execute("UPDATE work_workers SET last_seen_at=? WHERE id=?", (now, worker["id"]))
            self.work._event(db, org, row["program_id"], None, {"type": "leased", "job_id": row["id"], "fence": fence}, row["task_id"])
            claimed = db.execute("SELECT * FROM work_jobs WHERE id=?", (row["id"],)).fetchone()
            claim = db.execute("SELECT * FROM work_claims WHERE worker_id=? AND claim_id=?", (worker["id"], claim_id)).fetchone()
            return self._claim_view(db, credential, worker, claimed, claim)

    def _job_scope(self, credential, job):
        with self.store.transaction() as db:
            worker = self._worker(db, credential)
            row = _row(
                db.execute("SELECT program_id FROM work_jobs WHERE organization_id=? AND id=?", (worker["organization_id"], job)).fetchone()
            )
            return row["program_id"]

    def expire_due(self, stopped=lambda: False):
        """Installation-owned lease maintenance; never submits external work."""
        with self.store.transaction() as db:
            organizations = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT organization_id FROM work_jobs WHERE status='leased' AND lease_expires_at<=?", (int(time.time()),)
                )
            ]
        for org in organizations:
            if stopped():
                break
            with self.store.transaction(write=True, scope=f"work-dispatch:{org}") as db:
                self._expire(db, org, stopped)

    def _lease(self, db, credential, job, body, *, finished=False):
        worker = self._worker(db, credential, lock=True)
        row = _row(
            self._locked(db, "SELECT * FROM work_jobs WHERE organization_id=? AND id=?", (worker["organization_id"], job)).fetchone()
        )
        if (
            row["worker_id"] != worker["id"]
            or row["attempt_id"] != body.attempt_id
            or row["fence"] != body.fence
            or row["lease_hash"] is None
            or not hmac.compare_digest(row["lease_hash"], digest(body.lease.get_secret_value()))
        ):
            raise StoreError(409, "stale or invalid work lease")
        if not finished and (row["status"] != "leased" or row["lease_expires_at"] <= int(time.time())):
            raise StoreError(409, "work lease is no longer live")
        task = db.execute("SELECT epoch FROM work_tasks WHERE id=?", (row["task_id"],)).fetchone()
        if task["epoch"] != row["task_epoch"]:
            raise StoreError(409, "task epoch superseded this attempt")
        return row

    def heartbeat(self, credential, job, body):
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body)
            expires = int(time.time()) + LEASE_SECONDS
            db.execute("UPDATE work_jobs SET lease_expires_at=? WHERE id=?", (expires, job))
            return {"lease_expires_at": expires, "cancel_requested": bool(row["cancel_requested"])}

    def context(self, credential, job, body):
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body)
            return self._packet(db, row)

    def input_read(self, credential, job, body):
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body)
            task, root = self._authorized_task(db, row)
            return self._input_read(db, row, task, root, body)

    def _input_read(self, db, row, task, root, body):
        require_assigned(db, task, body.reference)
        self.work._input(db, row["organization_id"], root["project_id"], body.reference)
        ref = body.reference
        if ref.kind == "work_spec":
            text = db.execute(
                "SELECT body_json FROM work_specifications WHERE program_id=? AND revision=?", (ref.id, ref.revision)
            ).fetchone()[0]
        elif ref.kind == "work_task":
            text = db.execute("SELECT body_json FROM work_tasks WHERE id=?", (ref.id,)).fetchone()[0]
        elif ref.kind == "work_result":
            text = db.execute("SELECT body_json FROM work_results WHERE job_id=? AND fence=?", (ref.id, ref.revision)).fetchone()[0]
        elif ref.kind == "compute_result":
            text = db.execute("SELECT body_json FROM work_compute_receipts WHERE compute_job_id=?", (ref.id,)).fetchone()[0]
        elif ref.kind == "knowledge":
            evidence = db.execute(
                "SELECT title,body,state FROM knowledge_versions WHERE entry_id=? AND revision=?", (ref.id, ref.revision)
            ).fetchone()
            text = encoded(dict(evidence))
        elif ref.kind == "research_artifact":
            from .artifact_chunks import read

            return {"reference": ref.model_dump(), **read(db, ref.id, ref.revision, body.offset, body.length)}
        elif ref.kind == "source_snapshot":
            from .artifact_chunks import read_blob

            return {
                "reference": ref.model_dump(),
                **read_blob(db, row["organization_id"], root["project_id"], ref.sha256, body.offset, body.length),
            }
        else:
            text = db.execute(
                "SELECT body_json FROM mathematical_records WHERE organization_id=? AND path=?", (row["organization_id"], ref.id)
            ).fetchone()[0]
        return {
            "reference": ref.model_dump(),
            "text": text[body.offset : body.offset + body.length],
            "offset": body.offset,
            "total_characters": len(text),
            "truncated": body.offset + body.length < len(text),
        }

    def compact(self, credential, job, body):
        program = self._job_scope(credential, job)
        payload = encoded(body.model_dump(exclude={"attempt_id", "fence", "lease", "client_id"}))
        checksum = digest(payload)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body)
            task, root = self._authorized_task(db, row)
            old = db.execute(
                "SELECT sequence,sha256 FROM work_compactions WHERE job_id=? AND fence=? AND client_id=?", (job, body.fence, body.client_id)
            ).fetchone()
            if old:
                if old["sha256"] != checksum:
                    raise StoreError(409, "compaction replay differs from retained note")
                return dict(old)
            for ref in body.retained_inputs:
                require_assigned(db, task, ref)
                self.work._input(db, row["organization_id"], root["project_id"], ref)
            saved = db.execute(
                "INSERT INTO work_compactions(job_id,fence,client_id,body_json,sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING sequence,sha256",
                (job, body.fence, body.client_id, payload, checksum, row["worker_id"], int(time.time())),
            ).fetchone()
            self.work._provenance(
                db, row["organization_id"], root["project_id"], program, "compaction", str(saved["sequence"]), 1, body.retained_inputs
            )
            self.work._event(
                db,
                row["organization_id"],
                program,
                None,
                {"type": "compacted", "job_id": job, "sequence": saved["sequence"], "sha256": checksum},
                task["id"],
            )
            return dict(saved)

    def complete(self, credential, job, body):
        payload = encoded(body.model_dump(exclude={"attempt_id", "fence", "lease"}))
        checksum = digest(payload)
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body, finished=True)
            old = db.execute("SELECT * FROM work_results WHERE job_id=? AND fence=?", (job, body.fence)).fetchone()
            if old:
                if old["sha256"] != checksum:
                    raise StoreError(409, "completion replay differs from retained result")
                return {"sha256": checksum, "result": json.loads(old["body_json"])}
            if row["status"] != "leased" or row["lease_expires_at"] <= int(time.time()):
                raise StoreError(409, "expired attempts cannot publish a new result")
            if body.outcome != "cancelled":
                self._authorized_task(db, row)
            if db.execute(
                "SELECT 1 FROM work_effects WHERE job_id=? AND fence=? AND state IN ('intent','unknown') LIMIT 1", (job, body.fence)
            ).fetchone():
                raise StoreError(409, "resolve effect outcomes before completing work")
            root = db.execute("SELECT * FROM work_programs WHERE id=?", (program,)).fetchone()
            self.work._provenance(db, row["organization_id"], root["project_id"], program, "job", job, body.fence, body.outputs)
            now = int(time.time())
            db.execute(
                "INSERT INTO work_results VALUES(?,?,?,?,?,?,?)",
                (job, body.fence, body.attempt_id, row["worker_id"], payload, checksum, now),
            )
            status = {"result": "complete", "failed": "failed", "cancelled": "cancelled"}[body.outcome]
            if row["cancel_requested"] and body.outcome != "cancelled":
                raise StoreError(409, "cancelled work cannot publish a successful completion")
            db.execute("UPDATE work_jobs SET status=?,finished_at=? WHERE id=?", (status, now, job))
            db.execute(
                "UPDATE work_tasks SET status=?,updated_at=? WHERE id=? AND epoch=?",
                ({"result": "review", "failed": "blocked", "cancelled": "cancelled"}[body.outcome], now, row["task_id"], row["task_epoch"]),
            )
            self.work._event(
                db,
                row["organization_id"],
                program,
                None,
                {"type": "result", "job_id": job, "outcome": body.outcome, "sha256": checksum},
                row["task_id"],
            )
            wake(db, program)
            return {"sha256": checksum, "result": json.loads(payload)}

    @staticmethod
    def _usage(db, kind, scope):
        row = db.execute(
            "SELECT microusd,cpu_seconds FROM work_resource_accounts WHERE scope_kind=? AND scope_id=?", (kind, scope)
        ).fetchone()
        return dict(row) if row else {"microusd": 0, "cpu_seconds": 0}

    def effect_intent(self, credential, job, body):
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self._lease(db, credential, job, body)
            if row["cancel_requested"]:
                raise StoreError(409, "work cancellation requested")
            old = db.execute(
                "SELECT * FROM work_effects WHERE job_id=? AND fence=? AND client_id=?", (job, body.fence, body.client_id)
            ).fetchone()
            fields = ("kind", "request_sha256", "model", "image_id", "tool", "held_microusd", "held_cpu_seconds")
            if old:
                if any(old[key] != getattr(body, key) for key in fields):
                    raise StoreError(409, "effect identity was used for different input")
                return {"id": old["id"], "state": old["state"], "submit": False}
            root = db.execute(
                "SELECT p.*,v.body_json FROM work_programs p JOIN work_specifications v "
                "ON v.program_id=p.id AND v.revision=p.head_revision WHERE p.id=?",
                (program,),
            ).fetchone()
            if root["status"] != "active":
                raise StoreError(409, "work program is not active")
            self._authorized_task(db, row)
            if body.kind == "inference":
                from .inference_credit import lock as credit_lock
                credit_lock(self.store, db, row["organization_id"])
            ancestors = db.execute(
                "SELECT t.id,t.body_json FROM work_task_ancestors a JOIN work_tasks t ON t.id=a.ancestor_id "
                "WHERE a.task_id=? ORDER BY a.depth",
                (row["task_id"],),
            ).fetchall()
            scopes = [("program", program, WorkSpecification.model_validate_json(root["body_json"]).resources)]
            scopes += [("task", item["id"], WorkSpecification.model_validate_json(item["body_json"]).resources) for item in ancestors]
            for kind, identity, limits in scopes:
                usage = self._usage(db, kind, identity)
                account = db.execute(
                    "SELECT turns,tools FROM work_resource_accounts WHERE scope_kind=? AND scope_id=?", (kind, identity)
                ).fetchone()
                calls = dict(account) if account else {"turns": 0, "tools": 0}
                if (
                    limits.deadline_at is not None
                    and limits.deadline_at <= int(time.time())
                    or limits.budget_microusd is not None
                    and usage["microusd"] + body.held_microusd > limits.budget_microusd
                    or limits.cpu_seconds is not None
                    and usage["cpu_seconds"] + body.held_cpu_seconds > limits.cpu_seconds
                    or limits.max_turns is not None
                    and calls["turns"] + int(body.kind == "inference") > limits.max_turns
                    or limits.max_tool_calls is not None
                    and calls["tools"] + int(body.kind != "inference") > limits.max_tool_calls
                ):
                    raise StoreError(409, "shared work resource authorization cannot cover this effect")
                if body.kind == "inference" and body.model not in limits.allowed_models:
                    raise StoreError(403, "inference model is not authorized for this task")
                if body.kind == "compute" and body.image_id not in limits.allowed_images:
                    raise StoreError(403, "compute image is not authorized for this task")
                if body.kind == "local" and body.tool not in limits.allowed_tools:
                    raise StoreError(403, "local tool is not authorized for this task")
            self.work._policy(db, row["organization_id"], WorkSpecification.model_validate_json(ancestors[0]["body_json"]))
            identity, now = _id(), int(time.time())
            db.execute(
                "INSERT INTO work_effects(id,job_id,program_id,task_id,fence,client_id,kind,request_sha256,model,image_id,tool,"
                "held_microusd,held_cpu_seconds,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'intent',?)",
                (
                    identity,
                    job,
                    program,
                    row["task_id"],
                    body.fence,
                    body.client_id,
                    body.kind,
                    body.request_sha256,
                    body.model,
                    body.image_id,
                    body.tool,
                    body.held_microusd,
                    body.held_cpu_seconds,
                    now,
                ),
            )
            for kind, scope, _ in scopes:
                db.execute("INSERT INTO work_effect_scopes VALUES(?,?,?)", (identity, kind, scope))
                db.execute(
                    "INSERT INTO work_resource_accounts VALUES(?,?,?,?,?,?) ON CONFLICT(scope_kind,scope_id) DO UPDATE SET "
                    "microusd=work_resource_accounts.microusd+excluded.microusd,"
                    "cpu_seconds=work_resource_accounts.cpu_seconds+excluded.cpu_seconds,"
                    "turns=work_resource_accounts.turns+excluded.turns,tools=work_resource_accounts.tools+excluded.tools",
                    (kind, scope, body.held_microusd, body.held_cpu_seconds, int(body.kind == "inference"), int(body.kind != "inference")),
                )
            return {"id": identity, "state": "intent", "submit": True}

    def effect_receipt(self, credential, job, effect, body):
        program = self._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            assignment = self._lease(db, credential, job, body)
            if db.execute("SELECT 1 FROM work_provider_calls WHERE effect_id=?", (effect,)).fetchone():
                raise StoreError(403, "server-brokered inference is accounted from its retained provider receipt")
            if db.execute("SELECT 1 FROM work_computations WHERE effect_id=?", (effect,)).fetchone():
                raise StoreError(403, "server-brokered compute is accounted from its retained execution receipt")
            row = _row(db.execute("SELECT * FROM work_effects WHERE id=? AND job_id=? AND fence=?", (effect, job, body.fence)).fetchone())
            if row["kind"] == "inference":
                from .inference_credit import lock as credit_lock

                credit_lock(self.store, db, assignment["organization_id"])
            receipt = encoded(
                {
                    "source": "worker_report",
                    "state": body.state,
                    "actual_microusd": body.actual_microusd,
                    "actual_cpu_seconds": body.actual_cpu_seconds,
                    "receipt": body.receipt,
                }
            )
            if row["state"] != "intent":
                if row["receipt_json"] != receipt:
                    raise StoreError(409, "effect receipt replay differs from retained accounting")
                return {"id": effect, "state": row["state"]}
            db.execute(
                "UPDATE work_effects SET state=?,actual_microusd=?,actual_cpu_seconds=?,receipt_json=?,finished_at=? WHERE id=?",
                (body.state, body.actual_microusd, body.actual_cpu_seconds, receipt, int(time.time()), effect),
            )
            if body.state != "unknown":
                usd, cpu = (body.actual_microusd, body.actual_cpu_seconds) if body.state == "settled" else (0, 0)
                self._account_delta(db, row, usd, cpu)
            return {"id": effect, "state": body.state}
