"""An organization's Lean environments: requested by members, approved or declined by its administrators.

A request names a project's `lean-toolchain` and `lake-manifest.json`; the server keeps only the specification they
pin (see `lean_environments`) under its key. Asking again for a key the organization already has returns that record
unchanged, so reuse needs no approval. Only a human owner or administrator decides, and the decision names the key, so
it covers exactly the sources the record lists.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import lean_environments
from .compute_catalog import canonical
from .store import Store, StoreError, _id, _row

# Undecided requests an organization, and one member, may hold; past these, requests wait for an administrator.
MAX_REQUESTED = 16
MAX_REQUESTED_PER_MEMBER = 4
DECISIONS = ("approved", "declined")


class ComputeEnvironments:
    def __init__(self, store: Store, clock=time.time):
        self.store, self.clock = store, clock

    @staticmethod
    def _view(row: sqlite3.Row | dict) -> dict:
        # Validated when requested, and fixed by the schema since; not re-checked, so later rules never hide a record.
        specification = json.loads(row["specification_json"])
        return {
            "id": row["id"], "organization_id": row["organization_id"], "key": row["environment_key"],
            "status": row["status"], "specification": specification, "sources": lean_environments.sources(specification),
            "requested_by": row["requested_by"], "requested_at": row["requested_at"],
            "decided_by": row["decided_by"], "decided_at": row["decided_at"],
        }

    @staticmethod
    def _record(db: sqlite3.Connection, org: str, environment: str) -> dict:
        return _row(db.execute(
            "SELECT * FROM compute_environments WHERE organization_id=? AND id=?", (org, environment)
        ).fetchone())

    def request(self, user: str, org: str, lean_toolchain: str, lake_manifest: str) -> dict:
        """The organization's environment for these two files, recorded as a request if it is new."""
        try:
            specification = lean_environments.specification(lean_toolchain, lake_manifest)
        except lean_environments.SpecificationError as exc:
            raise StoreError(400, str(exc)) from exc
        key = lean_environments.key(specification)
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, write=True)
            existing = db.execute(
                "SELECT * FROM compute_environments WHERE organization_id=? AND environment_key=?", (org, key)
            ).fetchone()
            if existing is not None:
                return self._view(existing)
            waiting, mine = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN requested_by=? THEN 1 ELSE 0 END),0) FROM compute_environments "
                "WHERE organization_id=? AND status='requested'", (user, org)
            ).fetchone()
            if waiting >= MAX_REQUESTED:
                raise StoreError(429, f"this organization has {MAX_REQUESTED} Lean environment requests waiting; "
                                      "an administrator must decide some first")
            if mine >= MAX_REQUESTED_PER_MEMBER:
                raise StoreError(429, f"you have {MAX_REQUESTED_PER_MEMBER} Lean environment requests waiting; "
                                      "an administrator must decide some first")
            environment = _id()
            db.execute(
                "INSERT INTO compute_environments(id,organization_id,environment_key,specification_json,status,"
                "requested_by,requested_at) VALUES(?,?,?,?,'requested',?,?)",
                (environment, org, key, canonical(specification), user, int(self.clock())),
            )
            self.store._audit(db, org, user, "compute.environment.request", environment)
            return self._view(self._record(db, org, environment))

    def environments(self, user: str, org: str) -> list[dict]:
        """Every environment the organization has, waiting requests first, then the most recent."""
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            rows = db.execute(
                "SELECT * FROM compute_environments WHERE organization_id=? "
                "ORDER BY status<>'requested', requested_at DESC, id", (org,)
            ).fetchall()
            return [self._view(row) for row in rows]

    def environment(self, user: str, org: str, environment: str) -> dict:
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            return self._view(self._record(db, org, environment))

    def for_worker(self, credential: str, environment: str) -> dict:
        """An approved environment of the worker's own organization, for a relay to prepare. Waiting, declined and other
        organizations' environments are not found."""
        from .compute_store import ComputeStore

        with self.store.transaction() as db:
            worker = ComputeStore._authenticate_worker(db, credential)
            row = _row(db.execute(
                "SELECT * FROM compute_environments WHERE organization_id=? AND id=? AND status='approved'",
                (worker["organization_id"], environment),
            ).fetchone())
            view = self._view(row)
            return {key: view[key] for key in ("id", "organization_id", "key", "status", "specification")}

    def decide(self, user: str, org: str, environment: str, key: str, decision: str) -> dict:
        """Approve or decline a request. The key must be the record's, so a decision covers only what it listed.
        A declined environment can still be approved later; an approved one stays approved."""
        if decision not in DECISIONS:
            raise StoreError(400, "decide approved or declined")
        if not isinstance(key, str) or lean_environments.KEY.fullmatch(key) is None:
            raise StoreError(400, "name the environment key being decided")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            row = self._record(db, org, environment)
            if row["environment_key"] != key:
                raise StoreError(409, "this decision names a different Lean environment; reload and review its sources")
            if row["status"] == decision:
                return self._view(row)
            if row["status"] == "approved":
                raise StoreError(409, "this Lean environment is already approved")
            db.execute(
                "UPDATE compute_environments SET status=?,decided_by=?,decided_at=? WHERE organization_id=? AND id=?",
                (decision, user, int(self.clock()), org, environment),
            )
            self.store._audit(db, org, user, "compute.environment." + ("approve" if decision == "approved" else "decline"),
                              environment)
            return self._view(self._record(db, org, environment))
