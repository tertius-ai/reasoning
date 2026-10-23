"""Transactional storage adapter for the workbench's immutable mathematical domain.

Record construction, compare-and-set updates, proof pinning and derivation are
shared with LedgerStore. Paths are record keys only: no filesystem or Git runs.
The caller owns one organization transaction, including replay and ACL checks.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from ..canonical import canonical_json
from ..derive import LedgerView, integrity_issues
from ..records import validate_record
from ..store import LedgerStore, LedgerStoreError
from .store import StoreError


class MathematicalStore(LedgerStore):
    remote_sources = True

    def __init__(self, store, db, user, organization, *, project=None, topic=None, agent=False,
                 allow_project_topics=False, topic_authorize=None):
        super().__init__(Path("/mathematical-records"))
        self.store, self.db, self.user, self.org = store, db, user, organization
        self.project, self.topic, self.agent = project, topic, agent
        self.allow_project_topics, self.topic_authorize = allow_project_topics, topic_authorize
        self.importing = False
        self.role = store._require(db, user, organization, write=False)
        self._access_cache = {}

    @contextmanager
    def _lock(self, *, read_only=False):
        if not read_only:
            self.store._require(self.db, self.user, self.org, write=True)
            if self.topic:
                self.store._topic_access(self.db, self.user, self.org, self.topic, write=True)
            elif self.project:
                self.store._project_access(self.db, self.user, self.org, self.project, write=True)
        yield

    def _ensure_repo(self):
        pass

    def _git(self, *args, **kwargs):
        raise LedgerStoreError("organization records do not execute Git")

    def _commit(self, event, **details):
        self.store._audit(
            self.db,
            self.org,
            self.user,
            "ledger." + event,
            details.get("claim_id") or details.get("reference_id") or details.get("proof_id") or self.org,
        )

    def head_commit(self):
        return None

    def _key(self, path):
        return str(path.relative_to(self.root))

    def _visible(self, row):
        if (
            self.project is not None
            and row["project_id"]
            and row["project_id"] != self.project
            or self.agent
            and row["topic_id"]
            and row["topic_id"] != self.topic
            and not self.allow_project_topics
        ):
            return False
        key = (row["project_id"], row["topic_id"])
        if key not in self._access_cache:
            try:
                if row["topic_id"]:
                    topic = self.store._topic_access(self.db, self.user, self.org, row["topic_id"])[4]
                    if self.agent and topic["kind"] == "alpha":
                        return False
                    if self.topic_authorize:
                        self.topic_authorize(self.db, row["topic_id"])
                elif row["project_id"]:
                    self.store._project_access(self.db, self.user, self.org, row["project_id"])
                self._access_cache[key] = True
            except StoreError:
                self._access_cache[key] = False
        return self._access_cache[key]

    def _record(self, path):
        row = self.db.execute(
            "SELECT * FROM mathematical_records WHERE organization_id=? AND path=?", (self.org, self._key(path))
        ).fetchone()
        if row is None or not self._visible(row):
            raise LedgerStoreError("mathematical record unavailable")
        return row

    def _read(self, path, schema_name):
        record = json.loads(self._record(path)["body_json"])
        validate_record(record, schema_name)
        return record

    def _read_raw(self, path):
        return json.loads(self._record(path)["body_json"])

    @staticmethod
    def _type(path):
        parts = path.parts
        if "verifications" in parts:
            return "verification"
        family = "proof" if "proofs" in parts else "reference" if parts[0] == "references" else "claim"
        return family + ("-head" if parts[-1] == "head.json" else "-revision")

    def _scope(self, key, record):
        # Revisions and evidence inherit their object's original scope.
        head_key = key.split("/revisions/")[0] + "/head.json"
        if "/verifications/" in key:
            head_key = "claims/" + record["subject"]["claim_id"] + "/head.json"
        if "/proofs/" in key:
            head_key = "claims/" + record["claim_id"] + "/head.json"
        old = self.db.execute("SELECT * FROM mathematical_records WHERE organization_id=? AND path=?", (self.org, head_key)).fetchone()
        if old:
            if not self._visible(old):
                raise LedgerStoreError("mathematical record unavailable")
            return old["project_id"], old["topic_id"]
        if key.endswith("/head.json") and record.get("current_revision_id"):
            revision_key = key.removesuffix("head.json") + "revisions/" + record["current_revision_id"] + ".json"
            first = self.db.execute("SELECT project_id,topic_id FROM mathematical_records WHERE organization_id=? AND path=?",
                                    (self.org, revision_key)).fetchone()
            if first:
                return first["project_id"], first["topic_id"]
        if record.get("claim_id") and record.get("reference_id"):
            source = self._record(self.references_dir / record["reference_id"] / "head.json")
            if source["project_id"]:
                if self.project and self.project != source["project_id"]:
                    raise LedgerStoreError("source statement cannot expose a narrower-scope reference")
                self.store._project_access(self.db, self.user, self.org, source["project_id"], write=True)
                return source["project_id"], source["topic_id"]
        # New research is shared within its project, not accidentally org-wide.
        return self.project, None

    def _write_record(self, path, record):
        key = self._key(path)
        kind = self._type(Path(key))
        validate_record(record, kind)
        text = canonical_json(record)
        old = self.db.execute("SELECT * FROM mathematical_records WHERE organization_id=? AND path=?", (self.org, key)).fetchone()
        if old:
            if not self._visible(old):
                raise LedgerStoreError("mathematical record unavailable")
            if old["body_json"] != text:
                raise LedgerStoreError("immutable mathematical record differs")
            return False
        project, topic = self._scope(key, record)
        if kind == "proof-revision":
            for premise in record["premises"]:
                if "unresolved" in premise:
                    continue
                dependency = self._record(self._claim_head_path(premise["claim_id"]))
                if (dependency["project_id"] and dependency["project_id"] != project) or (
                    dependency["topic_id"] and dependency["topic_id"] != topic
                ):
                    raise LedgerStoreError("proof cannot expose a narrower-scope premise")
        object_id = record.get("verification_id") or record.get("proof_id") or record.get("claim_id") or record["reference_id"]
        claim = record.get("claim_id") or record.get("subject", {}).get("claim_id")
        self.db.execute(
            "INSERT INTO mathematical_records(organization_id,path,record_type,object_id,claim_id,project_id,topic_id,"
            "body_json,created_by,created_at,trust_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.org,
                key,
                kind,
                object_id,
                claim,
                project,
                topic,
                text,
                self.user,
                int(time.time()),
                "imported" if self.importing else "native",
            ),
        )
        return True

    def _write_atomic(self, path, text):
        key = self._key(path)
        kind = self._type(Path(key))
        if not kind.endswith("-head"):
            raise LedgerStoreError("only mathematical heads can change")
        self._record(path)
        record = json.loads(text)
        validate_record(record, kind)
        self.db.execute(
            "UPDATE mathematical_records SET body_json=? WHERE organization_id=? AND path=?", (canonical_json(record), self.org, key)
        )

    def reference_heads(self):
        return set(self.load().reference_heads)

    def _find_proof(self, proof_id):
        for row in self.db.execute(
            "SELECT * FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type='proof-head'", (self.org, proof_id)
        ):
            if self._visible(row):
                head = json.loads(row["body_json"])
                return head["claim_id"], head
        raise LedgerStoreError("mathematical proof unavailable")

    def load(self):
        view = LedgerView()
        fields = {
            "claim-head": ("claim_heads", "claim_id"),
            "claim-revision": ("claim_revisions", "revision_id"),
            "proof-head": ("proof_heads", "proof_id"),
            "proof-revision": ("proof_revisions", "revision_id"),
            "reference-head": ("reference_heads", "reference_id"),
            "reference-revision": ("reference_revisions", "revision_id"),
            "verification": ("verifications", "verification_id"),
        }
        for row in self.db.execute("SELECT * FROM mathematical_records WHERE organization_id=? ORDER BY path", (self.org,)):
            if self._visible(row):
                value = json.loads(row["body_json"])
                if row["record_type"] == "verification" and row["trust_state"] == "imported":
                    continue
                field, identity = fields[row["record_type"]]
                getattr(view, field)[value[identity]] = value
        return view

    def _load_view(self):
        return self.load()

    def claim_order(self, view):
        return {
            row["object_id"]: index
            for index, row in enumerate(
                self.db.execute(
                    "SELECT object_id FROM mathematical_records WHERE organization_id=? "
                    "AND record_type='claim-head' ORDER BY created_at,path",
                    (self.org,),
                )
            )
            if row["object_id"] in view.claim_heads
        }

    def fsck(self):
        issues = integrity_issues(self.load())
        return {"ok": not issues, "issues": issues}
