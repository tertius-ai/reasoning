"""Durable tenant-scoped compute admission, worker leases, and artifact delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from .compute_catalog import DIGEST, LANGUAGES, ComputeImage, canonical, fits, resources
from .security import digest, private_file, token
from .store import Store, StoreError, _id, _row
from .workflow_inputs import input_bytes

TERMINAL = {"succeeded", "failed", "cancelled"}
LEASE_SECONDS = 30
CHUNK_BYTES = 32768
LOG_BYTES = 1024**2
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
ERROR_CODES = {
    "execution_failed",
    "resource_limit",
    "wall_time_limit",
    "log_limit",
    "artifact_limit",
    "image_verification_failed",
    "worker_stopped",
}


def _bounded_int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


class ComputeStore:
    def __init__(self, store: Store, images: dict[str, ComputeImage]):
        self.store, self.images = store, dict(images)
        self.artifacts_dir = store.data_dir / "artifacts"
        self._directory(self.artifacts_dir)

    @staticmethod
    def _directory(path: Path, *, create: bool = True) -> None:
        created = False
        if create:
            try:
                path.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("artifact directories must be owner-owned, private, and not symlinks")
        if created:
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)

    @staticmethod
    def parameters(image_id: str, language: str, program: str, limits: dict) -> dict:
        if not isinstance(image_id, str) or not 1 <= len(image_id) <= 64 or not isinstance(language, str) or language not in LANGUAGES:
            raise StoreError(400, "select an approved image and supported language")
        if not isinstance(program, str) or not program.strip() or len(program) > 16000 or "\x00" in program:
            raise StoreError(400, "program must contain 1–16000 characters without NUL bytes")
        try:
            program.encode("utf8")
        except UnicodeEncodeError as exc:
            raise StoreError(400, "program must contain valid UTF-8 text") from exc
        try:
            validated = resources(limits)
        except ValueError as exc:
            raise StoreError(400, str(exc)) from exc
        return {"image_id": image_id, "language": language, "program": program, "resources": validated}

    @staticmethod
    def _event(db: sqlite3.Connection, org: str, job: str, event: dict) -> None:
        db.execute(
            "INSERT INTO compute_events(organization_id,job_id,event_json,created_at) VALUES(?,?,?,?)",
            (org, job, canonical(event), int(time.time())),
        )

    @staticmethod
    def _policy(db: sqlite3.Connection, org: str) -> dict:
        return json.loads(
            _row(db.execute("SELECT compute_policy_json FROM organizations WHERE id=?", (org,)).fetchone())["compute_policy_json"]
        )

    @staticmethod
    def _balance(db: sqlite3.Connection, org: str) -> dict:
        month = int(datetime.fromtimestamp(time.time(), UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        row = db.execute(
            """SELECT
            COALESCE(SUM(CASE WHEN accounted_at>=? THEN used_cpu_seconds ELSE 0 END),0) AS used_cpu_seconds,
            COALESCE(SUM(CASE WHEN accounted_at IS NULL THEN reserved_cpu_seconds ELSE 0 END),0) AS held_cpu_seconds
            FROM compute_jobs WHERE organization_id=?""",
            (month, org),
        ).fetchone()
        result = dict(row)
        result["monthly_cpu_seconds"] = ComputeStore._policy(db, org)["monthly_cpu_seconds"]
        result["available_cpu_seconds"] = max(0, result["monthly_cpu_seconds"] - result["used_cpu_seconds"] - result["held_cpu_seconds"])
        return result

    def configuration(self, user: str, org: str) -> dict:
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org)
            self._expire(db, org)
            return {
                "policy": self._policy(db, org),
                "balance": self._balance(db, org),
                "images": [image.record() for image in self.images.values()],
            }

    def set_policy(self, user: str, org: str, allowed: list[str], monthly: int, active: int,
                   pending: int | None = None, requests: int | None = None) -> dict:
        if (
            not isinstance(allowed, list)
            or len(allowed) > 32
            or any(not isinstance(item, str) for item in allowed)
            or len(allowed) != len(set(allowed))
            or not set(allowed) <= self.images.keys()
            or not _bounded_int(monthly, 0, 10**12)
            or not _bounded_int(active, 1, 4096)
            or pending is not None and not _bounded_int(pending, 1, 1_000_000)
            or requests is not None and not _bounded_int(requests, 1, 1_000_000)
        ):
            raise StoreError(400, "select approved images, a bounded CPU quota, execution concurrency, and optional queue/request limits")
        policy = {"allowed_images": allowed, "monthly_cpu_seconds": monthly, "max_active_jobs": active}
        if pending is not None:
            policy["max_pending_jobs"] = pending
        if requests is not None:
            policy["requests_per_user_15_minutes"] = requests
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            db.execute("UPDATE organizations SET compute_policy_json=? WHERE id=?", (canonical(policy), org))
            self.store._audit(db, org, user, "compute.policy", org)
        return policy

    @staticmethod
    def _public_job(row: sqlite3.Row | dict) -> dict:
        fields = (
            "id",
            "organization_id",
            "topic_id",
            "project_id",
            "requested_by",
            "client_id",
            "program_digest",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "worker_id",
            "deadline_at",
            "cancel_requested",
            "error_code",
            "exit_code",
            "elapsed_ms",
            "log_bytes",
            "reserved_cpu_seconds",
            "used_cpu_seconds",
            "accounted_at",
        )
        return {
            **{key: row[key] for key in fields},
            "parameters": json.loads(row["parameters_json"]),
            "image": json.loads(row["image_json"]),
        }

    @staticmethod
    def _job(db: sqlite3.Connection, org: str, job: str) -> dict:
        return _row(db.execute("SELECT * FROM compute_jobs WHERE organization_id=? AND id=?", (org, job)).fetchone())

    def _access(self, db, user, org, row, *, write=False):
        if row["topic_id"] is not None:
            return self.store._topic_access(db, user, org, row["topic_id"], write=write)[0]
        return self.store._project_access(db, user, org, row["project_id"], write=write)[0]

    def _admission(self, db: sqlite3.Connection, user: str, org: str, topic: str | None, parameters: dict, *, project=None) -> ComputeImage:
        if topic is None:
            self.store._project_access(db, user, org, project, write=True)
        else:
            target = self.store._topic_access(db, user, org, topic, write=True)[4]
            if target["kind"] not in {"beta", "gamma"}:
                raise StoreError(400, "compute jobs require a beta or gamma topic")
        image = self.images.get(parameters["image_id"])
        policy = self._policy(db, org)
        if not image or image.id not in policy["allowed_images"] or policy["monthly_cpu_seconds"] <= 0:
            raise StoreError(409, "an administrator must enable an approved image and a positive compute quota")
        if parameters["language"] not in image.languages or not fits(parameters["resources"], image.maximum_resources):
            raise StoreError(400, "language or resource request exceeds the approved image profile")
        return image

    def enqueue(self, user: str, org: str, topic: str, client: str, parameters: dict) -> dict:
        with self.store.transaction(write=True) as db:
            return self._enqueue(db, user, org, topic, client, parameters)

    def enqueue_project(self, user, org, project, client, parameters):
        with self.store.transaction(write=True) as db:
            return self._enqueue(db, user, org, None, client, parameters, project=project)

    def _enqueue(self, db, user, org, topic, client, parameters, *, inputs=None, project=None):
        """Transaction-scoped admission; workflow callers atomically link the child."""
        if not isinstance(client, str) or not 1 <= len(client) <= 128:
            raise StoreError(400, "invalid compute client identifier")
        parameters = self.parameters(parameters["image_id"], parameters["language"], parameters["program"], parameters["resources"])
        if inputs is not None:
            try:
                encoded_inputs = input_bytes(inputs)
            except (ValueError, TypeError, RecursionError) as exc:
                raise StoreError(400, "invalid or excessive workflow inputs") from exc
            parameters.update(inputs=json.loads(encoded_inputs), inputs_digest="sha256:" + hashlib.sha256(encoded_inputs).hexdigest())
        encoded = canonical(parameters)
        if topic is None:
            self.store._project_access(db, user, org, project, write=True)
        else:
            project = self.store._topic_access(db, user, org, topic, write=True)[1]["id"]
        existing = db.execute(
            "SELECT * FROM compute_jobs WHERE organization_id=? AND requested_by=? AND client_id=?", (org, user, client)
        ).fetchone()
        if existing:
            if existing["topic_id"] != topic or existing["project_id"] != project or existing["parameters_json"] != encoded:
                raise StoreError(409, "client identifier already names a different compute request")
            return self._public_job(existing)
        image = self._admission(db, user, org, topic, parameters, project=project)
        self._expire(db, org)
        policy = self._policy(db, org)
        if policy.get("max_pending_jobs") is not None:
            active = db.execute(
                "SELECT COUNT(*) FROM compute_jobs WHERE organization_id=? AND status IN ('queued','running','outcome_unknown')", (org,)
            ).fetchone()[0]
            if active >= policy["max_pending_jobs"]:
                raise StoreError(429, "organization compute queue reached its configured pending-job limit")
        now = int(time.time())
        if policy.get("requests_per_user_15_minutes") is not None:
            count = db.execute(
                "SELECT COUNT(*) FROM compute_jobs WHERE organization_id=? AND requested_by=? AND created_at>?", (org, user, now - 900)
            ).fetchone()[0]
            if count >= policy["requests_per_user_15_minutes"]:
                raise StoreError(429, "compute requests reached the configured per-user 15-minute limit")
        reserve = parameters["resources"]["cpu"] * parameters["resources"]["wall_time_seconds"]
        if reserve > self._balance(db, org)["available_cpu_seconds"]:
            raise StoreError(409, "organization compute quota cannot cover this reservation")
        job = _id()
        db.execute(
            """INSERT INTO compute_jobs(id,organization_id,topic_id,requested_by,client_id,parameters_json,
            image_json,program_digest,status,created_at,reserved_cpu_seconds,project_id) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)""",
            (job, org, topic, user, client, encoded, canonical(image.record()),
             "sha256:" + digest(parameters["program"]), now, reserve, project),
        )
        self._event(db, org, job, {"type": "state", "status": "queued"})
        db.execute("INSERT INTO compute_job_sequences(job_id) VALUES(?)", (job,))
        self.store._audit(db, org, user, "compute.request", job)
        return self._public_job(self._job(db, org, job))

    def job(self, user: str, org: str, job: str) -> dict:
        with self.store.transaction(write=True) as db:
            self._expire(db, org)
            row = self._job(db, org, job)
            self._access(db, user, org, row)
            return self._public_job(row)

    def jobs(self, user: str, org: str, topic: str, limit: int = 20, before: int = 2**63 - 1, standalone: bool = False) -> list[dict]:
        if not _bounded_int(limit, 1, 50):
            raise StoreError(400, "invalid job page size")
        if not _bounded_int(before, 1, 2**63 - 1) or type(standalone) is not bool:
            raise StoreError(400, "invalid job cursor")
        with self.store.transaction(write=True) as db:
            self.store._topic_access(db, user, org, topic)
            self._expire(db, org)
            return [
                {**self._public_job(row), **({"sequence": row["sequence"]} if standalone or before < 2**63 - 1 else {})}
                for row in db.execute(
                    "SELECT j.*,s.sequence FROM compute_jobs j JOIN compute_job_sequences s ON s.job_id=j.id "
                    "WHERE organization_id=? AND topic_id=? AND s.sequence<? "
                    + ("AND NOT EXISTS(SELECT 1 FROM workflow_node_attempts w WHERE w.compute_job_id=j.id) " if standalone else "")
                    + "ORDER BY s.sequence DESC LIMIT ?",
                    (org, topic, before, limit),
                )
            ]

    def project_jobs(self, user, org, project, limit=20, before=2**63 - 1):
        if not _bounded_int(limit, 1, 50) or not _bounded_int(before, 1, 2**63 - 1):
            raise StoreError(400, "invalid project compute page")
        with self.store.transaction(write=True) as db:
            self.store._project_access(db, user, org, project)
            self._expire(db, org)
            return [
                {**self._public_job(row), "sequence": row["sequence"]}
                for row in db.execute(
                    "SELECT j.*,s.sequence FROM compute_jobs j JOIN compute_job_sequences s ON s.job_id=j.id "
                    "WHERE j.organization_id=? AND j.project_id=? AND j.topic_id IS NULL AND s.sequence<? "
                    "ORDER BY s.sequence DESC LIMIT ?", (org, project, before, limit),
                )
            ]

    def events(self, user: str, org: str, job: str, after: int = 0) -> list[dict]:
        if not _bounded_int(after, 0, 2**63 - 1):
            raise StoreError(400, "invalid event cursor")
        with self.store.transaction(write=True) as db:
            self._expire(db, org)
            row = self._job(db, org, job)
            self._access(db, user, org, row)
            return [
                {"sequence": row["sequence"], "created_at": row["created_at"], "event": json.loads(row["event_json"])}
                for row in db.execute(
                    "SELECT * FROM compute_events WHERE organization_id=? AND job_id=? AND sequence>? ORDER BY sequence LIMIT 200",
                    (org, job, after),
                )
            ]

    def log_text(self, user: str, org: str, job: str) -> str:
        """Read one authorized snapshot of the job's at-most-one-MiB retained output."""
        with self.store.transaction() as db:
            self._access(db, user, org, self._job(db, org, job))
            return "".join(row[0] for row in db.execute(
                "SELECT json_extract(event_json,'$.text') FROM compute_events WHERE organization_id=? AND job_id=? "
                "AND json_extract(event_json,'$.type')='log' ORDER BY sequence", (org, job)))

    def cancel(self, user: str, org: str, job: str) -> dict:
        with self.store.transaction(write=True) as db:
            self._expire(db, org)
            row = self._job(db, org, job)
            role = self._access(db, user, org, row, write=True)
            if row["requested_by"] != user and role not in {"owner", "admin"}:
                raise StoreError(403, "only the requester or an administrator can cancel this job")
            self._request_cancel(db, org, row)
            self.store._audit(db, org, user, "compute.cancel", job)
            return self._public_job(self._job(db, org, job))

    def _request_cancel(self, db, org, row):
        if row["status"] == "queued":
            self._settle(db, row, "cancelled", 0, None, None)
        elif row["status"] not in TERMINAL and not row["cancel_requested"]:
            db.execute("UPDATE compute_jobs SET cancel_requested=1 WHERE organization_id=? AND id=?", (org, row["id"]))
            self._event(db, org, row["id"], {"type": "cancel_requested"})

    @staticmethod
    def _worker_view(row: sqlite3.Row | dict) -> dict:
        return {
            **{
                key: row[key]
                for key in (
                    "id",
                    "organization_id",
                    "name",
                    "created_at",
                    "last_seen_at",
                    "draining",
                    "revoked_at",
                    "retired_at",
                    "retirement_id",
                )
            },
            "image_ids": json.loads(row["image_ids_json"]),
            "resources": json.loads(row["resources_json"]),
        }

    def _worker_parameters(self, name: str, image_ids: list[str], limits: dict) -> dict:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 200
            or "\x00" in name
            or not isinstance(image_ids, list)
            or not image_ids
            or any(not isinstance(item, str) for item in image_ids)
            or len(image_ids) != len(set(image_ids))
            or not set(image_ids) <= self.images.keys()
        ):
            raise StoreError(400, "worker requires a name and approved images")
        try:
            name.encode("utf8")
            limits = resources(limits)
        except ValueError as exc:
            raise StoreError(400, str(exc)) from exc
        return limits

    def _register_worker(self, db, user, org, name, image_ids, limits, credential):
        worker = _id()
        try:
            db.execute(
                "INSERT INTO compute_workers(id,organization_id,name,token_hash,image_ids_json,resources_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (worker, org, name, digest(credential), canonical(image_ids), canonical(limits), int(time.time())),
            )
        except sqlite3.IntegrityError as exc:
            raise StoreError(409, "worker credential already belongs to another enrollment") from exc
        self.store._audit(db, org, user, "compute.worker-register", worker)
        return self._worker_view(db.execute("SELECT * FROM compute_workers WHERE id=?", (worker,)).fetchone())

    def register_worker(self, user: str, org: str, name: str, image_ids: list[str], limits: dict) -> dict:
        limits = self._worker_parameters(name, image_ids, limits)
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            credential = token()
            return {**self._register_worker(db, user, org, name, image_ids, limits, credential), "token": credential}

    def workers(self, user: str, org: str) -> list[dict]:
        with self.store.transaction() as db:
            self.store._require(db, user, org, admin=True)
            return [
                self._worker_view(row)
                for row in db.execute("SELECT * FROM compute_workers WHERE organization_id=? ORDER BY created_at,id", (org,))
            ]

    def scaling_snapshot(self, user: str, org: str) -> dict:
        """One read-only admin observation, never a claim or physical cleanup proof."""
        with self.store.transaction() as db:
            self.store._require(db, user, org, admin=True)
            return self._scaling_snapshot(db, org)

    def _scaling_snapshot(self, db: sqlite3.Connection, org: str) -> dict:
        now = int(time.time())
        capacity = self._policy(db, org)["max_active_jobs"]
        jobs = db.execute(
            "SELECT j.id,j.organization_id,j.project_id,j.topic_id,j.requested_by,j.status,j.worker_id,j.created_at,"
            "j.image_json,j.lease_expires_at,j.deadline_at,json_extract(j.parameters_json,'$.image_id') AS image_id,"
            "json_extract(j.parameters_json,'$.language') AS language,"
            "json_extract(j.parameters_json,'$.resources') AS resources_json "
            "FROM compute_jobs j JOIN compute_job_sequences s ON s.job_id=j.id "
            "WHERE j.organization_id=? AND (j.status IN ('running','outcome_unknown') OR j.id IN "
            "(SELECT q.id FROM compute_jobs q JOIN compute_job_sequences qs ON qs.job_id=q.id "
            "WHERE q.organization_id=? AND q.status='queued' ORDER BY q.created_at,qs.sequence LIMIT ?)) "
            "ORDER BY j.created_at,s.sequence", (org, org, capacity),
        ).fetchall()
        workers = db.execute(
            "SELECT w.*,MAX(w.created_at,COALESCE((SELECT MAX(j.finished_at) FROM compute_jobs j "
            "WHERE j.organization_id=w.organization_id AND j.worker_id=w.id),w.created_at)) AS idle_since_at "
            "FROM compute_workers w WHERE w.organization_id=? AND (w.revoked_at IS NULL OR w.id IN "
            "(SELECT worker_id FROM compute_jobs WHERE organization_id=? AND status IN ('running','outcome_unknown'))) "
            "ORDER BY w.created_at,w.id",
            (org, org),
        ).fetchall()
        queued_total = db.execute("SELECT COUNT(*) FROM compute_jobs WHERE organization_id=? AND status='queued'", (org,)).fetchone()[0]
        remaining = max(0, queued_total - sum(row["status"] == "queued" for row in jobs))
        requirements = []
        for entry in jobs:
            row = dict(entry)
            parameters = {"image_id": row["image_id"], "language": row["language"], "resources": json.loads(row["resources_json"])}
            row["parameters_json"] = canonical(parameters)
            image = json.loads(row["image_json"])
            requirements.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "worker_id": row["worker_id"],
                    "created_at": row["created_at"],
                    "image_id": parameters["image_id"],
                    "image_reference": image["reference"],
                    "resources": parameters["resources"],
                    "runnable": self._may_run(db, row) if row["status"] == "queued" else False,
                    "lease_expires_at": row["lease_expires_at"],
                    "deadline_at": row["deadline_at"],
                }
            )
        expanded = remaining > 0 or capacity > 8 or len(jobs) > 8 or len(workers) > 72
        return {
            "format": 2 if expanded else 1,
            **({"queued_remaining": remaining} if expanded else {}),
            "organization_id": org,
            "observed_at": now,
            "max_active_jobs": capacity,
            "images": [image.record() for image in self.images.values()],
            "jobs": requirements,
            "workers": [{**self._worker_view(row), "idle_since_at": row["idle_since_at"]} for row in workers],
        }

    def worker_identity(self, credential: str) -> dict:
        """Verify enrollment without claiming a job or extending an attempt."""
        with self.store.transaction() as db:
            return self._worker_view(self._authenticate_worker(db, credential))

    def worker_status(self, credential: str) -> dict:
        """Read unsettled attempts without extending leases or claiming work."""
        with self.store.transaction() as db:
            worker = self._authenticate_worker(db, credential)
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT id,status FROM compute_jobs WHERE organization_id=? AND worker_id=? "
                    "AND status IN ('running','outcome_unknown') ORDER BY created_at,id LIMIT 9",
                    (worker["organization_id"], worker["id"]),
                )
            ]
            return {**self._worker_view(worker), "unsettled_jobs": jobs, "quiescent": not jobs}

    def drain_self(self, credential: str) -> dict:
        with self.store.transaction(write=True) as db:
            worker = self._authenticate_worker(db, credential)
            if not worker["draining"]:
                db.execute("UPDATE compute_workers SET draining=1 WHERE id=?", (worker["id"],))
                self.store._audit(db, worker["organization_id"], None, "compute.worker-self-drain", worker["id"])
            return self._worker_view(db.execute("SELECT * FROM compute_workers WHERE id=?", (worker["id"],)).fetchone())

    @staticmethod
    def _retirement_view(row: dict | sqlite3.Row) -> dict:
        return {
            "worker_id": row["id"],
            "organization_id": row["organization_id"],
            "retired": row["retirement_id"] is not None,
            "retirement_id": row["retirement_id"],
            "retired_at": row["retired_at"],
        }

    @staticmethod
    def _retirement_worker(db: sqlite3.Connection, credential: str) -> sqlite3.Row:
        # A retired token can only inspect/replay its own fixed retirement receipt.
        # Administrative revocation without completed retirement grants no exception.
        row = db.execute(
            "SELECT * FROM compute_workers WHERE token_hash=? AND (revoked_at IS NULL OR retirement_id IS NOT NULL)",
            (digest(credential),),
        ).fetchone()
        if row is None:
            raise StoreError(401, "valid compute worker retirement credential required")
        return row

    def worker_retirement(self, credential: str) -> dict:
        with self.store.transaction() as db:
            return self._retirement_view(self._retirement_worker(db, credential))

    def retire_worker(self, credential: str, cleanup_confirmed: bool) -> dict:
        if cleanup_confirmed is not True:
            raise StoreError(400, "worker retirement requires verified physical cleanup")
        with self.store.transaction(write=True) as db:
            worker = self._retirement_worker(db, credential)
            if worker["retirement_id"] is not None:
                return self._retirement_view(worker)
            if (
                not worker["draining"]
                or db.execute(
                    "SELECT 1 FROM compute_jobs WHERE organization_id=? AND worker_id=? AND status IN ('running','outcome_unknown')",
                    (worker["organization_id"], worker["id"]),
                ).fetchone()
            ):
                raise StoreError(409, "worker must be drained and all assigned outcomes settled before retirement")
            now, receipt = int(time.time()), _id()
            db.execute(
                "UPDATE compute_workers SET draining=1,revoked_at=?,retired_at=?,retirement_id=? WHERE id=?",
                (now, now, receipt, worker["id"]),
            )
            self.store._audit(db, worker["organization_id"], None, "compute.worker-retire", worker["id"])
            return self._retirement_view(db.execute("SELECT * FROM compute_workers WHERE id=?", (worker["id"],)).fetchone())

    def change_worker(self, user: str, org: str, worker: str, *, draining: bool | None = None, revoke: bool = False) -> None:
        if draining is not None and type(draining) is not bool:
            raise StoreError(400, "invalid worker drain setting")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            current = _row(db.execute("SELECT * FROM compute_workers WHERE organization_id=? AND id=?", (org, worker)).fetchone())
            if current["retirement_id"] is not None and draining is False:
                raise StoreError(409, "retired workers cannot resume claims; enroll a replacement")
            if revoke:
                db.execute("UPDATE compute_workers SET revoked_at=COALESCE(revoked_at,?),draining=1 WHERE id=?", (int(time.time()), worker))
            elif draining is not None:
                db.execute("UPDATE compute_workers SET draining=? WHERE id=?", (int(draining), worker))
            self.store._audit(db, org, user, "compute.worker-revoke" if revoke else "compute.worker-drain", worker)

    @staticmethod
    def _authenticate_worker(db: sqlite3.Connection, credential: str) -> dict:
        row = db.execute("SELECT * FROM compute_workers WHERE token_hash=? AND revoked_at IS NULL", (digest(credential),)).fetchone()
        if row is None:
            raise StoreError(401, "valid compute worker credential required")
        return dict(row)

    def _expire(self, db: sqlite3.Connection, org: str) -> None:
        now = int(time.time())
        for row in db.execute(
            "SELECT * FROM compute_jobs WHERE organization_id=? AND status='running' AND (lease_expires_at<=? OR deadline_at<=?)",
            (org, now, now),
        ).fetchall():
            db.execute("UPDATE compute_jobs SET status='outcome_unknown',error_code='worker_lost' WHERE id=?", (row["id"],))
            self._event(db, org, row["id"], {"type": "state", "status": "outcome_unknown", "error_code": "worker_lost"})

    def _may_run(self, db: sqlite3.Connection, row: dict) -> bool:
        from .work_compute import authorized

        if not authorized(db, self.store, row):
            return False
        try:
            current = self._admission(
                db, row["requested_by"], row["organization_id"], row["topic_id"],
                json.loads(row["parameters_json"]), project=row["project_id"]
            )
        except StoreError:
            return False
        if canonical(current.record()) != row["image_json"]:
            return False
        balance = self._balance(db, row["organization_id"])
        return balance["used_cpu_seconds"] + balance["held_cpu_seconds"] <= balance["monthly_cpu_seconds"]

    @staticmethod
    def _claim_lease(credential: str, worker: str, attempt: str) -> str:
        return hmac.new(credential.encode(), f"reasoning-compute-lease-v1:{worker}:{attempt}".encode(), hashlib.sha256).hexdigest()

    def _claim_view(self, row: dict, lease: str) -> dict:
        return {
            **self._public_job(row),
            "attempt_id": row["attempt_id"],
            "lease": lease,
            "heartbeat_seconds": 10,
            "lease_seconds": LEASE_SECONDS,
        }

    def claim(self, credential: str, claim_id: str | None = None, *, supports_workflow_inputs: bool = False) -> dict | None:
        if type(supports_workflow_inputs) is not bool:
            raise StoreError(400, "workflow input support must be boolean")
        if claim_id is not None and (not isinstance(claim_id, str) or not re.fullmatch(r"[a-f0-9]{32}", claim_id)):
            raise StoreError(400, "claim ID must be 32 lowercase hexadecimal characters")
        with self.store.transaction(write=True) as db:
            worker = self._authenticate_worker(db, credential)
            org, now = worker["organization_id"], int(time.time())
            self._expire(db, org)
            db.execute("UPDATE compute_workers SET last_seen_at=? WHERE id=?", (now, worker["id"]))
            if claim_id is not None:
                old = db.execute("SELECT * FROM compute_jobs WHERE worker_id=? AND claim_id=?", (worker["id"], claim_id)).fetchone()
                if old is not None:
                    if "inputs" in json.loads(old["parameters_json"]) and not supports_workflow_inputs:
                        raise StoreError(409, "this claim requires workflow input support")
                    return self._claim_view(dict(old), self._claim_lease(credential, worker["id"], old["attempt_id"]))
            if (
                worker["draining"]
                or db.execute(
                    "SELECT 1 FROM compute_jobs WHERE worker_id=? AND status IN ('running','outcome_unknown')", (worker["id"],)
                ).fetchone()
            ):
                return None
            active = db.execute(
                "SELECT COUNT(*) FROM compute_jobs WHERE organization_id=? AND status IN ('running','outcome_unknown')", (org,)
            ).fetchone()[0]
            if active >= self._policy(db, org)["max_active_jobs"]:
                return None
            images, maximum = json.loads(worker["image_ids_json"]), json.loads(worker["resources_json"])
            for entry in db.execute(
                "SELECT j.* FROM compute_jobs j JOIN compute_job_sequences s ON s.job_id=j.id "
                "WHERE organization_id=? AND status='queued' ORDER BY created_at,s.sequence", (org,)
            ):
                row, parameters = dict(entry), json.loads(entry["parameters_json"])
                if "inputs" in parameters and not supports_workflow_inputs:
                    continue
                if not self._may_run(db, row):
                    self._settle(db, row, "failed", 0, "policy_changed", None)
                    continue
                if parameters["image_id"] not in images or not fits(parameters["resources"], maximum):
                    continue
                lease, attempt = token(), _id()
                if claim_id is not None:
                    lease = self._claim_lease(credential, worker["id"], attempt)
                # Delivery/log/artifact grace does not extend the program's declared wall-time limit.
                # Large outputs require many bounded, fsynced HTTP chunks.
                delivery_seconds = 120 + 2 * ((parameters["resources"]["artifact_bytes"] + 1024**2 - 1) // 1024**2)
                deadline = now + parameters["resources"]["wall_time_seconds"] + delivery_seconds
                db.execute(
                    "UPDATE compute_jobs SET status='running',worker_id=?,attempt_id=?,lease_hash=?,lease_expires_at=?,"
                    "deadline_at=?,started_at=?,claim_id=? WHERE id=?",
                    (worker["id"], attempt, digest(lease), now + LEASE_SECONDS, deadline, now, claim_id, row["id"]),
                )
                self._event(db, org, row["id"], {"type": "state", "status": "running", "worker_id": worker["id"]})
                return self._claim_view(self._job(db, org, row["id"]), lease)
            return None

    def _lease(
        self, db: sqlite3.Connection, credential: str, job: str, attempt: str, lease: str, *, finishing: bool = False
    ) -> tuple[dict, dict]:
        worker = self._authenticate_worker(db, credential)
        self._expire(db, worker["organization_id"])
        row = self._job(db, worker["organization_id"], job)
        if (
            row["worker_id"] != worker["id"]
            or row["attempt_id"] != attempt
            or not row["lease_hash"]
            or not hmac.compare_digest(row["lease_hash"], digest(lease))
        ):
            raise StoreError(409, "compute attempt is no longer authorized")
        if not finishing and row["status"] != "running":
            raise StoreError(409, "compute lease is no longer running")
        return worker, row

    def _delivery_lease(self, db: sqlite3.Connection, credential: str, job: str, attempt: str, lease: str) -> tuple[dict, dict]:
        worker, row = self._lease(db, credential, job, attempt, lease, finishing=True)
        if row["status"] not in {"running", "outcome_unknown"}:
            raise StoreError(409, "compute attempt is already settled")
        return worker, row

    def heartbeat(self, credential: str, job: str, attempt: str, lease: str) -> dict:
        with self.store.transaction(write=True) as db:
            worker, row = self._lease(db, credential, job, attempt, lease, finishing=True)
            stop = row["status"] != "running" or bool(row["cancel_requested"]) or not self._may_run(db, row)
            now = int(time.time())
            db.execute("UPDATE compute_workers SET last_seen_at=? WHERE id=?", (now, worker["id"]))
            if row["status"] == "running":
                db.execute("UPDATE compute_jobs SET lease_expires_at=? WHERE id=?", (min(now + LEASE_SECONDS, row["deadline_at"]), job))
            return {"stop": stop, "status": row["status"], "deadline_at": row["deadline_at"], "lease_seconds": LEASE_SECONDS}

    def append_log(self, credential: str, job: str, attempt: str, lease: str, index: int, text: str) -> None:
        try:
            encoded = text.encode("utf8") if isinstance(text, str) else b""
        except UnicodeEncodeError as exc:
            raise StoreError(400, "log chunks must contain valid UTF-8 text") from exc
        if not _bounded_int(index, 0, 4095) or not encoded or len(encoded) > 8192:
            raise StoreError(400, "log chunks require an index and 1–8192 UTF-8 bytes")
        with self.store.transaction(write=True) as db:
            _, row = self._delivery_lease(db, credential, job, attempt, lease)
            old = db.execute("SELECT content_hash FROM compute_log_chunks WHERE job_id=? AND chunk_index=?", (job, index)).fetchone()
            if old:
                if old[0] != digest(text):
                    raise StoreError(409, "log chunk index already contains different content")
                return
            count = db.execute("SELECT COUNT(*) FROM compute_log_chunks WHERE job_id=?", (job,)).fetchone()[0]
            if index != count:
                raise StoreError(409, "log chunks must arrive in order")
            if row["log_bytes"] + len(encoded) > LOG_BYTES:
                raise StoreError(413, "compute log exceeds 1 MiB")
            db.execute("INSERT INTO compute_log_chunks VALUES(?,?,?)", (job, index, digest(text)))
            db.execute("UPDATE compute_jobs SET log_bytes=log_bytes+? WHERE id=?", (len(encoded), job))
            self._event(db, row["organization_id"], job, {"type": "log", "chunk_index": index, "text": text})

    def _settle(self, db: sqlite3.Connection, row: dict, status: str, elapsed: int, error: str | None, exit_code: int | None) -> None:
        limits = json.loads(row["parameters_json"])["resources"]
        charged = min(row["reserved_cpu_seconds"], ((elapsed + 999) // 1000) * limits["cpu"])
        db.execute(
            "UPDATE compute_jobs SET status=?,finished_at=?,error_code=?,exit_code=?,elapsed_ms=?,"
            "used_cpu_seconds=?,accounted_at=? WHERE id=?",
            (status, int(time.time()), error, exit_code, elapsed, charged, int(time.time()), row["id"]),
        )
        self._event(
            db,
            row["organization_id"],
            row["id"],
            {
                "type": "state",
                "status": status,
                "error_code": error,
                "exit_code": exit_code,
                "elapsed_ms": elapsed,
                "used_cpu_seconds": charged,
            },
        )
        from .work_compute import settle

        settle(db, self.store, self._job(db, row["organization_id"], row["id"]))

    def finish(
        self, credential: str, job: str, attempt: str, lease: str, status: str, elapsed: int, exit_code: int | None, error: str | None
    ) -> dict:
        if (
            status not in TERMINAL
            or not _bounded_int(elapsed, 0, 86400 * 1000)
            or exit_code is not None
            and not _bounded_int(exit_code, -255, 255)
            or error is not None
            and error not in ERROR_CODES
            or status == "succeeded"
            and (exit_code != 0 or error is not None)
        ):
            raise StoreError(400, "invalid bounded compute outcome")
        with self.store.transaction(write=True) as db:
            _, row = self._lease(db, credential, job, attempt, lease, finishing=True)
            if row["status"] in TERMINAL:
                return self._public_job(row)
            if row["cancel_requested"] or not self._may_run(db, row):
                status, error = "cancelled", None
            limits = json.loads(row["parameters_json"])["resources"]
            if elapsed > limits["wall_time_seconds"] * 1000:
                status, error = "failed", "wall_time_limit"
            self._settle(db, row, status, elapsed, error, exit_code)
            return self._public_job(self._job(db, row["organization_id"], job))

    def reconcile(self, user: str, org: str, job: str, elapsed: int, cleanup_confirmed: bool) -> dict:
        if cleanup_confirmed is not True or not _bounded_int(elapsed, 0, 86400 * 1000):
            raise StoreError(400, "confirm that the lost container has stopped and supply bounded elapsed time")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            self._expire(db, org)
            row = self._job(db, org, job)
            if row["status"] != "outcome_unknown":
                raise StoreError(409, "only uncertain compute outcomes require reconciliation")
            self._settle(db, row, "cancelled" if row["cancel_requested"] else "failed", elapsed, "worker_lost", None)
            self.store._audit(db, org, user, "compute.reconcile", job)
            return self._public_job(self._job(db, org, job))

    def begin_artifact(self, credential: str, job: str, attempt: str, lease: str, name: str, size: int, checksum: str) -> dict:
        if (
            not isinstance(name, str)
            or not ARTIFACT_NAME.fullmatch(name)
            or name in {".", ".."}
            or not _bounded_int(size, 0, 1024**3)
            or not isinstance(checksum, str)
            or not DIGEST.fullmatch(checksum)
        ):
            raise StoreError(400, "artifact requires a safe filename, bounded size, and SHA-256 digest")
        with self.store.transaction(write=True) as db:
            _, row = self._delivery_lease(db, credential, job, attempt, lease)
            old = db.execute("SELECT * FROM compute_artifacts WHERE job_id=? AND name=?", (job, name)).fetchone()
            if old:
                if old["size_bytes"] != size or old["digest"] != checksum:
                    raise StoreError(409, "artifact name already contains different metadata")
                return dict(old)
            totals = db.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM compute_artifacts WHERE job_id=?", (job,)).fetchone()
            if totals[0] >= 64 or totals[1] + size > json.loads(row["parameters_json"])["resources"]["artifact_bytes"]:
                raise StoreError(413, "artifact count or total bytes exceed the job allowance")
            artifact = _id()
            db.execute(
                "INSERT INTO compute_artifacts(id,organization_id,job_id,name,size_bytes,digest) VALUES(?,?,?,?,?,?)",
                (artifact, row["organization_id"], job, name, size, checksum),
            )
            return dict(db.execute("SELECT * FROM compute_artifacts WHERE id=?", (artifact,)).fetchone())

    def _artifact(self, db: sqlite3.Connection, org: str, job: str, artifact: str) -> dict:
        return _row(
            db.execute("SELECT * FROM compute_artifacts WHERE organization_id=? AND job_id=? AND id=?", (org, job, artifact)).fetchone()
        )

    def artifact_chunk(self, credential: str, job: str, attempt: str, lease: str, artifact: str, index: int, content: str) -> None:
        if not _bounded_int(index, 0, 32767) or not isinstance(content, str) or len(content) > 43692:
            raise StoreError(400, "invalid artifact chunk")
        try:
            body = base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise StoreError(400, "artifact chunk requires valid base64") from exc
        with self.store.transaction(write=True) as db:
            _, row = self._delivery_lease(db, credential, job, attempt, lease)
            meta = self._artifact(db, row["organization_id"], job, artifact)
            expected = min(CHUNK_BYTES, meta["size_bytes"] - index * CHUNK_BYTES)
            if not 1 <= expected <= CHUNK_BYTES or len(body) != expected:
                raise StoreError(400, "artifact chunk does not match its declared size")
            checksum = "sha256:" + hashlib.sha256(body).hexdigest()
            old = db.execute(
                "SELECT digest FROM compute_artifact_chunks WHERE artifact_id=? AND chunk_index=?", (artifact, index)
            ).fetchone()
            if old:
                if old[0] != checksum:
                    raise StoreError(409, "artifact chunk index already contains different bytes")
                return
            if meta["committed_at"] is not None:
                raise StoreError(409, "artifact is already committed")
            self._directory(self.artifacts_dir)
            directory = self.artifacts_dir / artifact
            self._directory(directory)
            # File is durable before its database reference. An orphan from a crash
            # can only be adopted by a retry carrying precisely the same bytes.
            if private_file(directory / str(index), body) != body:
                raise StoreError(409, "artifact retry differs from its durable chunk")
            db.execute("INSERT INTO compute_artifact_chunks VALUES(?,?,?,?)", (artifact, index, len(body), checksum))

    def _read_chunk(self, artifact: str, index: int) -> bytes:
        self._directory(self.artifacts_dir, create=False)
        self._directory(self.artifacts_dir / artifact, create=False)
        fd = os.open(self.artifacts_dir / artifact / str(index), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > CHUNK_BYTES or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError("invalid private artifact chunk")
            return stream.read(CHUNK_BYTES + 1)

    def commit_artifact(self, credential: str, job: str, attempt: str, lease: str, artifact: str) -> dict:
        with self.store.transaction(write=True) as db:
            _, row = self._delivery_lease(db, credential, job, attempt, lease)
            meta = self._artifact(db, row["organization_id"], job, artifact)
            if meta["committed_at"] is not None:
                return meta
            chunks = db.execute("SELECT * FROM compute_artifact_chunks WHERE artifact_id=? ORDER BY chunk_index", (artifact,)).fetchall()
            if len(chunks) != (meta["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES:
                raise StoreError(409, "artifact chunks are incomplete")
        # Uploaded metadata and chunk references are immutable. Hash outside the
        # SQLite write lock so a large output cannot stall worker heartbeats.
        hasher, size = hashlib.sha256(), 0
        for index, chunk in enumerate(chunks):
            body = self._read_chunk(artifact, index)
            if (
                chunk["chunk_index"] != index
                or len(body) != chunk["size_bytes"]
                or "sha256:" + hashlib.sha256(body).hexdigest() != chunk["digest"]
            ):
                raise StoreError(409, "artifact chunk integrity check failed")
            hasher.update(body)
            size += len(body)
        if size != meta["size_bytes"] or "sha256:" + hasher.hexdigest() != meta["digest"]:
            raise StoreError(409, "artifact digest does not match its declared bytes")
        with self.store.transaction(write=True) as db:
            _, row = self._delivery_lease(db, credential, job, attempt, lease)
            meta = self._artifact(db, row["organization_id"], job, artifact)
            if meta["committed_at"] is not None:
                return meta
            now = int(time.time())
            db.execute("UPDATE compute_artifacts SET committed_at=? WHERE id=?", (now, artifact))
            self._event(
                db,
                row["organization_id"],
                job,
                {"type": "artifact", "artifact_id": artifact, "name": meta["name"], "size_bytes": size, "digest": meta["digest"]},
            )
            return {**meta, "committed_at": now}

    def artifacts(self, user: str, org: str, job: str) -> list[dict]:
        with self.store.transaction() as db:
            row = self._job(db, org, job)
            self._access(db, user, org, row)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM compute_artifacts WHERE organization_id=? AND job_id=? AND committed_at IS NOT NULL ORDER BY name",
                    (org, job),
                )
            ]

    def download(self, user: str, org: str, job: str, artifact: str) -> tuple[dict, list[dict]]:
        with self.store.transaction() as db:
            row = self._job(db, org, job)
            self._access(db, user, org, row)
            meta = self._artifact(db, org, job, artifact)
            if meta["committed_at"] is None:
                raise StoreError(404, "not found")
            chunks = [
                dict(row)
                for row in db.execute("SELECT * FROM compute_artifact_chunks WHERE artifact_id=? ORDER BY chunk_index", (artifact,))
            ]
            return meta, chunks

    def download_chunks(self, meta: dict, chunks: list[dict]):
        for chunk in chunks:
            body = self._read_chunk(meta["id"], chunk["chunk_index"])
            if len(body) != chunk["size_bytes"] or "sha256:" + hashlib.sha256(body).hexdigest() != chunk["digest"]:
                raise ValueError("artifact chunk integrity check failed")
            yield body
