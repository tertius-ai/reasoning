"""Bounded knowledge working sets inside current authorization, with dependency closure."""

import json
import re

from ..derive import LedgerView
from .mathematical_store import MathematicalStore
from .store import StoreError

SCOPES = ("working_set", "topic", "project")


def account_row(db, org, topic, revision=None):
    if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**63 - 1):
        raise StoreError(400, "select an exact account revision")
    return db.execute(
        "SELECT v.*,a.head_revision FROM research_accounts a JOIN research_account_versions v "
        "ON v.organization_id=a.organization_id AND v.topic_id=a.topic_id AND v.revision=COALESCE(?,a.head_revision) "
        "WHERE a.organization_id=? AND a.topic_id=?",
        (revision, org, topic),
    ).fetchone()


class KnowledgeWindow:
    def __init__(self, store, db, user, org, topic, scope=None, *, topic_authorize=None):
        self.store, self.db, self.user, self.org, self.topic = store, db, user, org, topic
        self.project = store._topic_access(db, user, org, topic)[1]["id"]
        self.topic_authorize = topic_authorize
        settings = db.execute(
            "SELECT context_policy_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic)
        ).fetchone()
        policy = json.loads(settings[0]) if settings else {}
        self.requested = scope or policy.get("knowledge_scope", "working_set")
        if self.requested not in SCOPES:
            raise StoreError(400, "knowledge scope must be working_set, topic, or project")
        row = account_row(db, org, topic)
        self.body = json.loads(row["body_json"]) if row else {}
        self.scope = "topic" if self.requested == "working_set" and not row else self.requested
        self.info = {
            "requested": self.requested,
            "effective": self.scope,
            "account_revision": row["revision"] if row else None,
            "project_id": self.project,
            "topic_id": topic,
            "source_topic_ids": [],
            "notice": "This is a working view, not an assignment or a change of access. "
            "Exact-ID reads and explicit project searches recover wider authorized evidence.",
        }

    def base(self):
        return MathematicalStore(
            self.store,
            self.db,
            self.user,
            self.org,
            project=self.project,
            topic=self.topic,
            agent=True,
            allow_project_topics=True,
            topic_authorize=self.topic_authorize,
        )

    def ledger(self):
        base = self.base()
        full = base.load()
        if self.scope == "project":
            roots, refs = set(full.claim_heads), set(full.reference_heads)
        elif self.scope == "topic":
            rows = self.db.execute(
                "SELECT object_id,record_type FROM mathematical_records WHERE organization_id=? AND topic_id=? "
                "AND record_type IN ('claim-head','reference-head')",
                (self.org, self.topic),
            ).fetchall()
            roots = {r["object_id"] for r in rows if r["record_type"] == "claim-head"} & full.claim_heads.keys()
            refs = {r["object_id"] for r in rows if r["record_type"] == "reference-head"} & full.reference_heads.keys()
            # Project-shared records retain the topic of their creating/editing request.
            # Scope selection uses that provenance without narrowing their access policy.
            for request in self.db.execute(
                "SELECT result_json FROM mathematical_requests WHERE organization_id=? AND json_extract(arguments_json,'$.topic')=?",
                (self.org, self.topic),
            ):
                result = json.loads(request[0])
                if result.get("claim_id") in full.claim_heads:
                    roots.add(result["claim_id"])
                if result.get("reference_id") in full.reference_heads:
                    refs.add(result["reference_id"])
        else:
            roots = set(self.body.get("claim_ids", [])) & full.claim_heads.keys()
            refs = set(self.body.get("reference_ids", [])) & full.reference_heads.keys()
        selected = set(roots)
        # Keep all authorized premise revisions and evidence needed to derive status.
        # Historical proof revisions are included so a pinned older argument stays interpretable.
        while True:
            premises = {
                p.get("claim_id")
                for proof in full.proof_revisions.values()
                if proof["claim_id"] in selected
                for p in proof.get("premises", [])
            }
            enlarged = selected | (premises & full.claim_heads.keys())
            if enlarged == selected:
                break
            selected = enlarged
        refs |= {
            rev["reference_id"]
            for rev in full.claim_revisions.values()
            if rev["claim_id"] in selected and rev.get("reference_id") in full.reference_heads
        }
        view = LedgerView(
            claim_heads={k: v for k, v in full.claim_heads.items() if k in selected},
            claim_revisions={k: v for k, v in full.claim_revisions.items() if v["claim_id"] in selected},
            proof_heads={k: v for k, v in full.proof_heads.items() if v["claim_id"] in selected},
            proof_revisions={k: v for k, v in full.proof_revisions.items() if v["claim_id"] in selected},
            reference_heads={k: v for k, v in full.reference_heads.items() if k in refs},
            reference_revisions={k: v for k, v in full.reference_revisions.items() if v["reference_id"] in refs},
            verifications={k: v for k, v in full.verifications.items() if v["subject"]["claim_id"] in selected},
        )
        self.info.update(
            {
                "root_claim_ids": sorted(roots)[:50],
                "root_claim_count": len(roots),
                "dependency_count": len(selected - roots),
                "claims_in_view": len(selected),
                "references_in_view": len(refs),
                "incomplete_view": self.scope != "project",
            }
        )
        # This adapter is used only for reads. Domain derivation sees the closed view,
        # so hiding unrelated records never turns a missing premise into establishment.
        base.load = lambda: view
        return base

    def entry_allowed(self, entry, *, exact=False):
        if entry["project_id"] not in {None, self.project}:
            return False
        target = entry["topic_id"]
        if target:
            try:
                topic = self.store._topic_access(self.db, self.user, self.org, target)[4]
                if topic["kind"] == "alpha":
                    return False
                if self.topic_authorize:
                    self.topic_authorize(self.db, target)
            except StoreError:
                return False
        if exact or self.scope == "project":
            return True
        if self.scope == "topic":
            return target == self.topic
        return entry["id"] in self.body.get("entry_ids", [])

    def bounded(self, result):
        clipped, text_clipped = {}, {}

        def bound(value, path=""):
            if isinstance(value, list):
                if len(value) > 50:
                    clipped[path] = len(value)
                kept, size = [], 0
                for item in value[:50]:
                    item = bound(item, path)
                    size += len(json.dumps(item, ensure_ascii=False).encode())
                    if kept and size > 20000:
                        clipped[path] = len(value)
                        break
                    kept.append(item)
                return kept
            if isinstance(value, dict):
                return {key: bound(item, path + "." + key) for key, item in value.items()}
            if isinstance(value, str) and len(value) > 2000:
                text_clipped[path] = len(value)
                return value[:2000]
            return value

        result = bound(result)
        result["knowledge_scope"] = dict(self.info)
        if "nodes" in result and "edges" in result:
            visible = {node["claim_id"] for node in result["nodes"]}
            result["edges"] = [edge for edge in result["edges"] if edge["from_claim"] in visible and edge["to_claim"] in visible]
        if text_clipped:
            result["knowledge_scope"]["truncated_text_fields"] = text_clipped
            result["knowledge_scope"]["next_action"] = "Read a listed exact ID for the complete statement and dependencies."
        if clipped:
            result["knowledge_scope"]["truncated_lists"] = clipped
            result["knowledge_scope"]["next_action"] = "Narrow the search, read a listed exact ID, or explicitly expand scope."
        return result

    def note_sources(self, result):
        identities = set()

        def visit(value):
            if isinstance(value, str) and re.fullmatch(r"[a-z]+_[0-9a-f]{16,64}", value):
                identities.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(result)
        if len(identities) > 1000:
            raise StoreError(409, "knowledge view is too broad; narrow the requested records")
        topics = set(self.info.get("source_topic_ids", []))
        base = self.base()
        ordered = sorted(identities)
        for start in range(0, len(ordered), 200):
            batch = ordered[start : start + 200]
            rows = self.db.execute(
                "SELECT * FROM mathematical_records WHERE organization_id=? AND object_id IN (" + ",".join("?" for _ in batch) + ")",
                (self.org, *batch),
            )
            topics.update(row["topic_id"] for row in rows if row["topic_id"] and base._visible(row))
        topics.update(entry["topic_id"] for entry in result.get("entries", []) if entry.get("topic_id"))
        self.info["source_topic_ids"] = sorted(topics)
        result["knowledge_scope"] = dict(self.info) | result.get("knowledge_scope", {})
        result["knowledge_scope"]["source_topic_ids"] = sorted(topics)
        return result

    def search_entries(self, collections, *, kind=None, query="", limit=20, before=None, published_only=True, terms=None):
        if kind is not None and kind not in collections:
            raise StoreError(403, "this knowledge collection is not granted")
        if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 30:
            raise StoreError(400, "choose a bounded knowledge query and page size")
        kinds = [kind] if kind else sorted(collections & {"ledger", "bibliography", "journal"})
        if not kinds:
            return {"entries": [], "knowledge_scope": self.info, "next_before": None}
        conditions = [
            "e.organization_id=?",
            "(e.project_id IS NULL OR e.project_id=?)",
            "e.kind IN (" + ",".join("?" for _ in kinds) + ")",
            "(e.topic_id IS NULL OR EXISTS (SELECT 1 FROM topics t WHERE t.id=e.topic_id AND t.deleted_at IS NULL AND t.kind!='alpha'))",
            "NOT EXISTS (SELECT 1 FROM blog_deletions d WHERE d.organization_id=e.organization_id AND d.entry_id=e.id)",
        ]
        values = [self.org, self.project, *kinds]
        if self.scope == "topic":
            conditions.append("e.topic_id=?")
            values.append(self.topic)
        elif self.scope == "working_set":
            ids = self.body.get("entry_ids", [])
            if not ids:
                return {"entries": [], "knowledge_scope": self.info, "next_before": None}
            conditions.append("e.id IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        if self.topic_authorize:
            allowed = []
            for source in self.db.execute(
                "SELECT t.id FROM topics t JOIN channels c ON c.id=t.channel_id "
                "WHERE t.organization_id=? AND c.project_id=? AND t.kind!='alpha' "
                "AND t.deleted_at IS NULL AND c.deleted_at IS NULL",
                (self.org, self.project),
            ):
                try:
                    self.store._topic_access(self.db, self.user, self.org, source[0])
                    self.topic_authorize(self.db, source[0])
                    allowed.append(source[0])
                except StoreError:
                    continue
            conditions.append(
                "(e.topic_id IS NULL" + (" OR e.topic_id IN (" + ",".join("?" for _ in allowed) + ")" if allowed else "") + ")"
            )
            values.extend(allowed)
        if query:
            conditions.append("(LOWER(v.title) LIKE ? OR LOWER(v.body) LIKE ?)")
            values.extend(["%" + query.lower() + "%"] * 2)
        if terms:
            words = sorted(set(terms))[:20]
            conditions.append("(" + " OR ".join("(LOWER(v.title) LIKE ? OR LOWER(v.body) LIKE ?)" for _ in words) + ")")
            for word in words:
                values.extend(["%" + word.lower() + "%"] * 2)
        if before:
            try:
                stamp, identity = before.split(":", 1)
                stamp = int(stamp)
                if len(identity) != 32 or not all(c in "0123456789abcdef" for c in identity):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise StoreError(400, "invalid knowledge cursor") from None
            conditions.append("(v.created_at<? OR (v.created_at=? AND e.id<?))")
            values.extend([stamp, stamp, identity])
        version = (
            "CASE WHEN e.kind='journal' THEN (SELECT revision FROM blog_publications p "
            "WHERE p.organization_id=e.organization_id AND p.entry_id=e.id) ELSE e.head_revision END"
            if published_only
            else "e.head_revision"
        )
        rows = self.db.execute(
            "SELECT e.*,v.revision,v.title,v.body,v.state,v.created_at AS revised_at FROM knowledge_entries e "
            "JOIN knowledge_versions v ON v.organization_id=e.organization_id AND v.entry_id=e.id AND v.revision="
            + version
            + " WHERE "
            + " AND ".join(conditions)
            + " ORDER BY v.created_at DESC,e.id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        result, size = [], 0
        scanned = []
        for row in rows[:limit]:
            scanned.append(row)
            if not self.entry_allowed(row):
                continue
            entry = dict(row)
            text = entry["body"]
            entry["body"], entry["body_truncated"] = text[:2000], len(text) > 2000
            encoded_size = len(json.dumps(entry, ensure_ascii=False).encode())
            if result and size + encoded_size > 12000:
                scanned.pop()
                break
            result.append(entry)
            size += encoded_size
        more = len(scanned) < len(rows)
        return self.note_sources(
            {
                "entries": result,
                "knowledge_scope": self.info,
                "next_before": str(scanned[-1]["revised_at"]) + ":" + scanned[-1]["id"] if more and scanned else None,
            }
        )

    def reviews(self, *, limit=40, review_id=None):
        """Select critiques within the working view and recheck their source topics."""
        view = self.ledger().load()
        rows = self.db.execute(
            "SELECT r.* FROM mathematical_reviews r JOIN topics t ON t.id=r.topic_id "
            "JOIN channels c ON c.id=t.channel_id WHERE r.organization_id=? AND c.project_id=? "
            "AND (? IS NULL OR r.id=?) ORDER BY r.created_at DESC,r.id DESC LIMIT 200",
            (self.org, self.project, review_id, review_id),
        )
        result = []
        for row in rows:
            subject = json.loads(row["subject_json"])
            if not review_id:
                if self.scope == "topic" and row["topic_id"] != self.topic:
                    continue
                if self.scope == "working_set" and not (
                    subject.get("claim_id") in view.claim_heads or subject.get("account_revision") and row["topic_id"] == self.topic
                ):
                    continue
            sources = set(json.loads(row["input_json"]).get("source_topic_ids", [])) | {row["topic_id"]}
            try:
                for source in sources:
                    access = self.store._topic_access(self.db, self.user, self.org, source)
                    if access[1]["id"] != self.project or access[4]["kind"] == "alpha":
                        raise StoreError(404, "review source unavailable")
                    if self.topic_authorize:
                        self.topic_authorize(self.db, source)
            except StoreError:
                continue
            current = account_row(self.db, self.org, row["topic_id"]) if subject.get("account_revision") else None
            value = {key: row[key] for key in ("id", "topic_id", "status", "requested_by", "reviewer_model", "created_at")}
            value.update(
                subject=subject,
                result=json.loads(row["result_json"]) if row["result_json"] else None,
                stale=bool(current and current["revision"] != subject["account_revision"]),
            )
            result.append(value)
            self.info["source_topic_ids"] = sorted(set(self.info["source_topic_ids"]) | sources)
            if len(result) >= limit:
                break
        return result


def capture_knowledge_sources(store, db, run, result):
    topics = set(result.get("knowledge_scope", {}).get("source_topic_ids", []))
    if result.get("topic_id"):
        topics.add(result["topic_id"])
    captured = db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (run["request_id"],)).fetchone()
    if not captured or not topics:
        return
    snapshot = json.loads(captured[0])
    topics.update(snapshot.get("knowledge_source_topics", []))
    if len(topics) > 200:
        raise StoreError(409, "too many knowledge-source topics; use a narrower working set")
    project = store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"])[1]["id"]
    for topic in topics:
        access = store._topic_access(db, run["requested_by"], run["organization_id"], topic)
        if access[1]["id"] != project or access[4]["kind"] == "alpha":
            raise StoreError(409, "knowledge source is outside authorized research scope")
    snapshot["knowledge_source_topics"] = sorted(topics)
    db.execute("UPDATE research_contexts SET snapshot_json=? WHERE request_id=?", (json.dumps(snapshot), run["request_id"]))


def authorize_knowledge_sources(store, run):
    with store.transaction() as db:
        captured = db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (run["request_id"],)).fetchone()
        snapshot = json.loads(captured[0]) if captured else {}
        project = store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"])[1]["id"]
        for topic in snapshot.get("knowledge_source_topics", []):
            access = store._topic_access(db, run["requested_by"], run["organization_id"], topic)
            if access[1]["id"] != project or access[4]["kind"] == "alpha":
                raise StoreError(409, "captured knowledge source is no longer authorized")
