"""Tenant-scoped capacity observers and recoverable bounded worker enrollment."""

from __future__ import annotations

import json
import re
import time

from .compute_catalog import canonical, fits
from .compute_store import SCALER_SEATS
from .security import digest, token
from .store import StoreError, _id, _row

SCALER_TOKEN = re.compile(r"rns_[A-Za-z0-9_-]{43}\Z")
WORKER_TOKEN = re.compile(r"rnw_[A-Za-z0-9_-]{43}\Z")
REQUEST_ID = re.compile(r"[a-f0-9]{32}\Z")


class ComputeScalers:
    def __init__(self, compute):
        self.compute, self.store = compute, compute.store

    @staticmethod
    def _lifetime(value):
        if type(value) is not int or not 3600 <= value <= 31_536_000:
            raise StoreError(400, "scaler lifetime must be one hour through 365 days")

    @staticmethod
    def _public(db, row):
        sponsor = db.execute(
            "SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
            "WHERE m.organization_id=? AND m.user_id=? AND m.role IN ('owner','admin') AND u.account_kind='human'",
            (row["organization_id"], row["authorized_by"]),
        ).fetchone()
        return {
            **{
                field: row[field]
                for field in ("id", "organization_id", "authorized_by", "name", "maximum_workers", "created_at", "expires_at", "revoked_at")
            },
            "image_ids": json.loads(row["image_ids_json"]),
            "resources": json.loads(row["resources_json"]),
            # Workers holding its seats; at maximum_workers the grant can enroll no one, so it adds no capacity.
            "enrolled_workers": db.execute(SCALER_SEATS.format(scaler="?"), (row["id"],)).fetchone()[0],
            "scopes": ["compute:snapshot", "workers:enroll"],
            "enabled": sponsor is not None and row["revoked_at"] is None and row["expires_at"] > int(time.time()),
        }

    def create(self, user, org, name, images, limits, maximum_workers, lifetime=2_592_000):
        limits = self.compute._worker_parameters(name, images, limits)
        if type(maximum_workers) is not int or not 1 <= maximum_workers <= 64:
            raise StoreError(400, "scaler worker ceiling must be one through 64")
        if any(not fits(limits, self.compute.images[image].maximum_resources) for image in images):
            raise StoreError(400, "scaler allowance must fit all selected approved images")
        self._lifetime(lifetime)
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            now = int(time.time())
            if (
                db.execute(
                    "SELECT COUNT(*) FROM compute_scalers WHERE organization_id=? AND revoked_at IS NULL AND expires_at>?", (org, now)
                ).fetchone()[0]
                >= 8
            ):
                raise StoreError(429, "organization already has eight current scaler credentials")
            identity, credential = _id(), "rns_" + token()
            db.execute(
                "INSERT INTO compute_scalers VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    identity,
                    org,
                    user,
                    name,
                    digest(credential),
                    canonical(sorted(images)),
                    canonical(limits),
                    maximum_workers,
                    now,
                    now + lifetime,
                ),
            )
            self.store._audit(db, org, user, "compute.scaler-create", identity)
            return {**self._public(db, db.execute("SELECT * FROM compute_scalers WHERE id=?", (identity,)).fetchone()), "token": credential}

    def list(self, user, org):
        with self.store.transaction() as db:
            self.store._require(db, user, org, admin=True)
            return [
                self._public(db, row)
                for row in db.execute("SELECT * FROM compute_scalers WHERE organization_id=? ORDER BY created_at,id", (org,))
            ]

    def revoke(self, user, org, identity):
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            row = _row(db.execute("SELECT * FROM compute_scalers WHERE organization_id=? AND id=?", (org, identity)).fetchone())
            if row["revoked_at"] is None:
                db.execute("UPDATE compute_scalers SET revoked_at=? WHERE id=?", (int(time.time()), identity))
                self.store._audit(db, org, user, "compute.scaler-revoke", identity)

    def rotate(self, user, org, identity, lifetime=2_592_000):
        self._lifetime(lifetime)
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            row = _row(db.execute("SELECT * FROM compute_scalers WHERE organization_id=? AND id=?", (org, identity)).fetchone())
            if row["revoked_at"] is not None:
                raise StoreError(409, "revoked scaler credentials cannot be reactivated")
            now = int(time.time())
            if (
                row["expires_at"] <= now
                and db.execute(
                    "SELECT COUNT(*) FROM compute_scalers WHERE organization_id=? AND revoked_at IS NULL AND expires_at>?", (org, now)
                ).fetchone()[0]
                >= 8
            ):
                raise StoreError(429, "organization already has eight current scaler credentials")
            credential = "rns_" + token()
            db.execute(
                "UPDATE compute_scalers SET token_hash=?,expires_at=?,authorized_by=? WHERE id=?",
                (digest(credential), now + lifetime, user, identity),
            )
            self.store._audit(db, org, user, "compute.scaler-rotate", identity)
            return {**self._public(db, db.execute("SELECT * FROM compute_scalers WHERE id=?", (identity,)).fetchone()), "token": credential}

    @staticmethod
    def _authenticate(db, credential):
        if not isinstance(credential, str) or not SCALER_TOKEN.fullmatch(credential):
            raise StoreError(401, "current compute scaler credential required")
        row = db.execute(
            "SELECT * FROM compute_scalers WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
            (digest(credential), int(time.time())),
        ).fetchone()
        if row is None or not ComputeScalers._public(db, row)["enabled"]:
            raise StoreError(401, "current compute scaler credential required")
        return row

    def identity(self, credential):
        with self.store.transaction() as db:
            row = self._authenticate(db, credential)
            return {
                **self._public(db, row),
                "images": [
                    self.compute.images[image].record() for image in json.loads(row["image_ids_json"]) if image in self.compute.images
                ],
            }

    def snapshot(self, credential):
        with self.store.transaction() as db:
            row = self._authenticate(db, credential)
            return self.compute._scaling_snapshot(db, row["organization_id"])

    def _rate(self, credential):
        with self.store.transaction(write=True) as db:
            scaler = self._authenticate(db, credential)
            now = int(time.time())
            db.execute("DELETE FROM compute_scaler_requests WHERE attempted_at<?", (now - 86400,))
            if (
                db.execute(
                    "SELECT COUNT(*) FROM compute_scaler_requests WHERE scaler_id=? AND attempted_at>?", (scaler["id"], now - 60)
                ).fetchone()[0]
                >= 30
            ):
                raise StoreError(429, "scaler enrollment request rate exceeded; retry later")
            db.execute("INSERT INTO compute_scaler_requests VALUES(?,?)", (scaler["id"], now))

    def enroll(self, credential, request_id, name, images, limits, worker_token):
        self._rate(credential)
        limits = self.compute._worker_parameters(name, images, limits)
        if (
            not isinstance(request_id, str)
            or not REQUEST_ID.fullmatch(request_id)
            or not isinstance(worker_token, str)
            or not WORKER_TOKEN.fullmatch(worker_token)
        ):
            raise StoreError(400, "recoverable enrollment requires an exact request ID and client-generated worker credential")
        requested = canonical({"name": name, "image_ids": sorted(images), "resources": limits, "credential_sha256": digest(worker_token)})
        with self.store.transaction(write=True) as db:
            scaler = self._authenticate(db, credential)
            org, now = scaler["organization_id"], int(time.time())
            prior = db.execute(
                "SELECT * FROM compute_enrollments WHERE scaler_id=? AND request_id=?", (scaler["id"], request_id)
            ).fetchone()
            if prior:
                if prior["request_json"] != requested:
                    raise StoreError(409, "enrollment request ID belongs to a different original request")
                worker = _row(db.execute("SELECT * FROM compute_workers WHERE id=?", (prior["worker_id"],)).fetchone())
                if worker["revoked_at"] is not None or worker["retired_at"] is not None:
                    raise StoreError(409, "original worker enrollment is revoked or retired; this request cannot replace it")
                return {"format": 1, "scaler_id": scaler["id"], "worker": self.compute._worker_view(worker)}
            if not set(images) <= set(json.loads(scaler["image_ids_json"])) or not fits(limits, json.loads(scaler["resources_json"])):
                raise StoreError(403, "worker enrollment exceeds its scaler image/resource grant")
            if any(not fits(limits, self.compute.images[image].maximum_resources) for image in images):
                raise StoreError(409, "approved image resource limits changed; no worker was enrolled")
            if not set(images) <= set(self.compute._policy(db, org)["allowed_images"]):
                raise StoreError(409, "organization compute policy does not currently allow these enrollment images")
            count = db.execute(SCALER_SEATS.format(scaler="?"), (scaler["id"],)).fetchone()[0]
            if count >= scaler["maximum_workers"]:
                raise StoreError(429, "scaler already has its maximum unrevoked or still-busy workers")
            worker = self.compute._register_worker(db, scaler["authorized_by"], org, name, sorted(images), limits, worker_token)
            db.execute("INSERT INTO compute_enrollments VALUES(?,?,?,?,?,?)", (org, scaler["id"], request_id, worker["id"], requested, now))
            self.store._audit(db, org, scaler["authorized_by"], "compute.scaler-enroll", worker["id"])
            return {"format": 1, "scaler_id": scaler["id"], "worker": worker}
