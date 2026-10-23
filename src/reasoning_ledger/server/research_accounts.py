"""One optional, versioned mathematical account per beta topic."""

import hashlib
import json
import re
import time
from urllib.parse import urlencode

from ..canonical import canonical_json
from .knowledge_store import KnowledgeStore
from .research_scope import KnowledgeWindow, account_row
from .store import StoreError

FIELDS = {
    "question": 8000,
    "hypotheses": 8000,
    "argument": 20000,
    "sources": 8000,
    "failed_approaches": 8000,
    "gaps": 8000,
    "next_steps": 4000,
    "approach": 2000,
}
LINKS = ("claim_ids", "reference_ids", "entry_ids")
REVIEW_LIMIT = 2


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ResearchAccounts:
    def __init__(self, store, *, authorize=None, topic_authorize=None):
        self.store, self.authorize, self.topic_authorize = store, authorize, topic_authorize

    def access(self, db, user, org, topic, write=False):
        if self.authorize:
            self.authorize(db)
        access = self.store._topic_access(db, user, org, topic, write=write)
        if access[4]["kind"] != "beta":
            raise StoreError(400, "research accounts belong to beta topics")
        return access

    def _pins(self, db, user, org, topic, body, old, changes, expected_links):
        policy = json.loads(self.store._topic_access(db, user, org, topic)[4]["knowledge_policy_json"])
        view = KnowledgeWindow(self.store, db, user, org, topic, "project", topic_authorize=self.topic_authorize).base().load()
        pins = dict(old.get("pins", {}))
        for field in LINKS:
            if field not in changes and old:
                continue
            if body[field] and {"claim_ids": "ledger", "reference_ids": "bibliography", "entry_ids": None}[field] not in [None, *policy]:
                raise StoreError(403, "the linked knowledge collection is not granted in this topic")
            resolved = []
            for identity in body[field]:
                if field == "entry_ids":
                    entry = KnowledgeStore._view(db, org, identity)
                    window = KnowledgeWindow(self.store, db, user, org, topic, "project", topic_authorize=self.topic_authorize)
                    if entry["kind"] not in policy or not window.entry_allowed(entry, exact=True):
                        raise StoreError(404, "linked knowledge is unavailable")
                    if entry["kind"] == "journal":
                        publication = db.execute(
                            "SELECT revision FROM blog_publications WHERE organization_id=? AND entry_id=?", (org, identity)
                        ).fetchone()
                        if not publication:
                            raise StoreError(409, "link a published journal revision")
                        entry = KnowledgeStore._view(db, org, identity, publication[0])
                    if expected_links.get(identity) != entry["revision"]:
                        raise StoreError(409, "read and pin the exact linked entry revision before saving")
                    resolved.append({"id": identity, "revision": entry["revision"]})
                else:
                    heads = view.claim_heads if field == "claim_ids" else view.reference_heads
                    if identity not in heads:
                        raise StoreError(404, "linked mathematical record is unavailable")
                    if expected_links.get(identity) != heads[identity]["current_revision_id"]:
                        raise StoreError(409, "linked mathematical revision changed or was not read; refresh its exact revision")
                    resolved.append({"id": identity, "revision_id": heads[identity]["current_revision_id"]})
            pins[field] = resolved
        return pins

    def view(self, db, row, *, user, summary=False):
        if not row:
            return None
        value = {
            key: row[key] for key in ("revision", "head_revision", "sha256", "content_sha256", "review_cycle", "created_by", "created_at")
        }
        body = json.loads(row["body_json"])
        value["question"] = body["question"][:300]
        value["url"] = "/organization.html?" + urlencode(
            {"organization": row["organization_id"], "topic": row["topic_id"], "account_revision": row["revision"]}
        )
        value["reviews_used"] = db.execute(
            "SELECT COUNT(*) FROM mathematical_reviews WHERE organization_id=? AND topic_id=? "
            "AND json_extract(subject_json,'$.account_cycle')=?",
            (row["organization_id"], row["topic_id"], row["review_cycle"]),
        ).fetchone()[0]
        value["review_limit"] = REVIEW_LIMIT
        value["authority"] = "Working account; saving does not certify mathematics or change ledger status."
        if not summary:
            value["body"] = body
            value["linked_changes"] = []
            window = KnowledgeWindow(
                self.store, db, user, row["organization_id"], row["topic_id"], "project", topic_authorize=self.topic_authorize
            )
            base = window.base()
            collections = set(
                json.loads(self.store._topic_access(db, user, row["organization_id"], row["topic_id"])[4]["knowledge_policy_json"])
            )
            for field in LINKS:
                for pin in body.get("pins", {}).get(field, []):
                    current, changed = None, False
                    if field == "entry_ids":
                        try:
                            entry = KnowledgeStore._view(db, row["organization_id"], pin["id"])
                            if entry["kind"] in collections and window.entry_allowed(entry, exact=True):
                                current = entry["head_revision"]
                                if entry["kind"] == "journal":
                                    published = db.execute(
                                        "SELECT revision FROM blog_publications WHERE organization_id=? AND entry_id=?",
                                        (row["organization_id"], pin["id"]),
                                    ).fetchone()
                                    current = published[0] if published else None
                                changed = current is not None and current != pin["revision"]
                        except StoreError:
                            pass
                    elif {"claim_ids": "ledger", "reference_ids": "bibliography"}[field] in collections:
                        head = db.execute(
                            "SELECT * FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type LIKE '%-head'",
                            (row["organization_id"], pin["id"]),
                        ).fetchone()
                        if head and base._visible(head):
                            current = json.loads(head["body_json"])["current_revision_id"]
                            changed = current != pin["revision_id"]
                    if changed or current is None:
                        value["linked_changes"].append({"id": pin["id"], "changed": bool(changed), "unavailable": current is None})
            value["history"] = [
                dict(r)
                for r in db.execute(
                    "SELECT revision,sha256,created_by,created_at FROM research_account_versions "
                    "WHERE organization_id=? AND topic_id=? ORDER BY revision DESC LIMIT 50",
                    (row["organization_id"], row["topic_id"]),
                )
            ]
        return value

    def read(self, user, org, topic, revision=None, *, summary=False):
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**63 - 1):
            raise StoreError(400, "select an exact account revision")
        with self.store.transaction() as db:
            self.access(db, user, org, topic)
            row = account_row(db, org, topic, revision)
            if revision is not None and not row:
                raise StoreError(404, "account revision unavailable")
            return self.view(db, row, user=user, summary=summary)

    def update(
        self,
        user,
        org,
        topic,
        changes,
        expected_revision,
        client_id,
        *,
        run_id=None,
        actor_id=None,
        new_approach=False,
        expected_links=None,
    ):
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - set(FIELDS) - set(LINKS)
            or type(expected_revision) is not int
            or not 0 <= expected_revision < 2**63 - 1
            or not isinstance(client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", client_id)
        ):
            raise StoreError(400, "account updates require valid changes, an exact revision and a stable client identity")
        if new_approach and (run_id is not None or not isinstance(changes.get("approach"), str) or not changes["approach"].strip()):
            raise StoreError(403, "a person must describe a new approach to start another review cycle")
        actor = actor_id or user
        if expected_links is not None and (not isinstance(expected_links, dict) or len(expected_links) > 120):
            raise StoreError(400, "choose bounded exact linked revisions")
        expected_links = expected_links or {}
        valid_id = r"(?:[a-f0-9]{32}|[a-z]+_[a-f0-9]{16,64})"
        if any(
            not isinstance(key, str)
            or not re.fullmatch(valid_id, key)
            or not (type(value) is int and 1 <= value <= 2**63 - 1 or isinstance(value, str) and re.fullmatch(valid_id, value))
            for key, value in expected_links.items()
        ):
            raise StoreError(400, "linked revisions must be exact record identities or positive entry revisions")
        arguments = canonical_json(
            {"changes": changes, "expected_revision": expected_revision, "new_approach": new_approach, "expected_links": expected_links}
        )
        with self.store.transaction(write=True) as db:
            self.access(db, user, org, topic, write=True)
            if self.store._postgres:
                self.store._postgres.lock(db.native, "research-account:" + org + ":" + topic)
            replay = db.execute(
                "SELECT topic_id,revision,arguments_json FROM research_account_versions "
                "WHERE organization_id=? AND created_by=? AND client_id=?",
                (org, actor, client_id),
            ).fetchone()
            if replay:
                if replay["topic_id"] != topic or replay["arguments_json"] != arguments:
                    raise StoreError(409, "account client identity was reused for a different update")
                return self.view(db, account_row(db, org, topic, replay["revision"]), user=user)
            old = account_row(db, org, topic)
            head = old["revision"] if old else 0
            if expected_revision != head:
                raise StoreError(409, "research account changed; read the current version before saving")
            body = json.loads(old["body_json"]) if old else {**dict.fromkeys(FIELDS, ""), **{key: [] for key in LINKS}}
            for field, value in changes.items():
                if field in FIELDS:
                    if not isinstance(value, str) or len(value) > FIELDS[field] or "\0" in value:
                        raise StoreError(400, "account text exceeds its field allowance or contains invalid characters")
                elif (
                    not isinstance(value, list)
                    or len(value) > 40
                    or any(
                        not isinstance(identity, str) or not re.fullmatch(r"(?:[a-f0-9]{32}|[a-z]+_[a-f0-9]{16,64})", identity)
                        for identity in value
                    )
                    or len(set(value)) != len(value)
                ):
                    raise StoreError(400, "link at most 40 distinct exact knowledge identities per collection")
                body[field] = value
            if not body["question"].strip():
                raise StoreError(400, "the account needs an exact question")
            if new_approach and old and body["approach"].strip() == json.loads(old["body_json"])["approach"].strip():
                raise StoreError(409, "describe a changed approach before starting another review cycle")
            body["pins"] = self._pins(db, user, org, topic, body, json.loads(old["body_json"]) if old else {}, changes, expected_links)
            try:
                encoded = canonical_json(body)
                valid = len(encoded.encode()) <= 48 * 1024
            except UnicodeError:
                valid = False
            if not valid:
                raise StoreError(400, "account exceeds 48 KiB of valid UTF-8; link a complete manuscript instead")
            cycle = (old["review_cycle"] if old else 1) + int(new_approach and old is not None)
            revision = head + 1
            checksum = digest(body)
            content_hash = digest({key: body[key] for key in (*FIELDS, "pins") if key != "next_steps"})
            db.execute(
                "INSERT INTO research_accounts(organization_id,topic_id,head_revision,review_cycle) VALUES(?,?,?,?) "
                "ON CONFLICT(organization_id,topic_id) DO UPDATE SET "
                "head_revision=excluded.head_revision,review_cycle=excluded.review_cycle",
                (org, topic, revision, cycle),
            )
            db.execute(
                "INSERT INTO research_account_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (org, topic, revision, encoded, checksum, content_hash, cycle, actor, run_id, client_id, arguments, int(time.time())),
            )
            self.store._audit(db, org, actor, "research.account.new_approach" if new_approach else "research.account.update", topic)
            return self.view(db, account_row(db, org, topic, revision), user=user)

    def material(self, db, user, org, topic, revision):
        access = self.access(db, user, org, topic)
        collections = set(json.loads(access[4]["knowledge_policy_json"]))
        row = account_row(db, org, topic, revision)
        if not row:
            raise StoreError(404, "account revision unavailable")
        body = json.loads(row["body_json"])
        ledger = KnowledgeWindow(self.store, db, user, org, topic, "project", topic_authorize=self.topic_authorize).base()
        view = ledger.load()
        evidence = []
        sources = set()
        for field, revisions in (("claim_ids", view.claim_revisions), ("reference_ids", view.reference_revisions)):
            if body["pins"].get(field) and {"claim_ids": "ledger", "reference_ids": "bibliography"}[field] not in collections:
                raise StoreError(403, "a linked mathematical collection is no longer granted")
            for pin in body["pins"].get(field, []):
                record = revisions.get(pin["revision_id"])
                if not record:
                    raise StoreError(409, "a linked revision is no longer accessible; resolve its scope before review")
                heads = view.claim_heads if field == "claim_ids" else view.reference_heads
                current_revision = heads[pin["id"]]["current_revision_id"]
                evidence.append(
                    {
                        "id": pin["id"],
                        "revision_id": pin["revision_id"],
                        "record": record,
                        "current_revision_id": current_revision,
                        "stale": current_revision != pin["revision_id"],
                    }
                )
                source = db.execute(
                    "SELECT topic_id FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type LIKE '%-head'",
                    (org, pin["id"]),
                ).fetchone()
                if source and source[0]:
                    sources.add(source[0])
        for pin in body["pins"].get("entry_ids", []):
            entry = KnowledgeStore._view(db, org, pin["id"], pin["revision"])
            window = KnowledgeWindow(self.store, db, user, org, topic, "project", topic_authorize=self.topic_authorize)
            if entry["kind"] not in collections or not window.entry_allowed(entry, exact=True):
                raise StoreError(409, "a linked entry is no longer accessible")
            evidence.append({"id": pin["id"], "revision": pin["revision"], "title": entry["title"], "body": entry["body"]})
            if entry["topic_id"]:
                sources.add(entry["topic_id"])
        return row, {
            "account": body,
            "linked_evidence": evidence,
            "source_topic_ids": sorted(sources),
            "notice": "Review this exact working account. Saving or favorably reviewing it does not promote ledger records. "
            "Read pinned premises on demand; retain uncertainty.",
        }

    def find(self, user, org, topic, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise StoreError(400, "enter a short knowledge search")
        with self.store.transaction() as db:
            access = self.access(db, user, org, topic)
            collections = set(json.loads(access[4]["knowledge_policy_json"]))
            window = KnowledgeWindow(self.store, db, user, org, topic, "project", topic_authorize=self.topic_authorize)
            ledger = window.base()
            view, results = ledger.load(), []
            for kind, heads, revisions, field in (
                ("ledger", view.claim_heads, view.claim_revisions, "statement_text"),
                ("bibliography", view.reference_heads, view.reference_revisions, "title"),
            ):
                if kind not in collections:
                    continue
                for identity, head in heads.items():
                    revision = revisions[head["current_revision_id"]]
                    if query.casefold() in revision[field].casefold():
                        results.append(
                            {"id": identity, "revision_id": revision["revision_id"], "label": revision[field][:600], "kind": kind}
                        )
                        if len(results) >= 20:
                            break
            entries = window.search_entries(collections, query=query, limit=10)["entries"]
            results += [
                {"id": entry["id"], "revision": entry["revision"], "label": entry["title"], "kind": entry["kind"]} for entry in entries
            ]
            return results[:30]
