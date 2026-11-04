"""Durable file assembly with exact retries and atomic publication."""

import hashlib
import json
import re
import time

from .artifact_chunks import MAX_BYTES
from .research_artifacts import ResearchArtifacts, file_links, safe_path
from .research_file_limits import MAX_ARTIFACT_TEXT_BYTES
from .store import StoreError, _id, _row

MAX_DRAFT_CHUNKS = 4096
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ResearchArtifactDrafts:
    def __init__(self, store, *, authorize=None):
        self.store = store
        self.artifacts = ResearchArtifacts(store, authorize=authorize)

    def _lock(self, db, scope):
        if self.store._postgres:
            self.store._postgres.lock(db.native, "file-draft:" + scope)

    def _load(self, db, user, org, topic, draft_id, *, write=False):
        project = self.artifacts._topic(db, user, org, topic, write=write)
        if write:
            self._lock(db, draft_id)
        return _row(db.execute(
            "SELECT * FROM research_artifact_drafts WHERE id=? AND organization_id=? AND project_id=? "
            "AND topic_id=? AND created_by=?", (draft_id, org, project, topic, user),
        ).fetchone())

    @staticmethod
    def _metadata(row):
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {"draft_id": row["id"], "path": row["path"], "total_characters": row["total_characters"],
                "size_bytes": row["size_bytes"], "next_offset": row["total_characters"], "sha256": row["sha256"],
                "chunk_count": row["chunk_count"], "status": "completed" if result else "draft", "result": result}

    def begin(self, user, org, topic, path, client_id, expected_sha256=None, run_id=None):
        try:
            valid_client = isinstance(client_id, str) and 1 <= len(client_id) <= 200 and "\0" not in client_id
            if valid_client:
                client_id.encode("utf8")
        except UnicodeError:
            valid_client = False
        if not safe_path(path) or not valid_client:
            raise StoreError(400, "file draft requires a safe relative name and a stable client ID of at most 200 characters")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256)):
            raise StoreError(400, "file draft requires an exact current SHA-256 before replacing a file")
        with self.store.transaction(write=True) as db:
            project = self.artifacts._topic(db, user, org, topic, write=True)
            self._lock(db, f"begin:{org}:{user}:{client_id}")
            old = db.execute("SELECT * FROM research_artifact_drafts WHERE organization_id=? AND created_by=? AND client_id=?",
                             (org, user, client_id)).fetchone()
            if old:
                if old["topic_id"] != topic or old["project_id"] != project or old["path"] != path or (
                    old["expected_sha256"] != expected_sha256
                ):
                    raise StoreError(409, "file draft client ID already identifies different content")
                return self._metadata(old)
            if run_id is not None and not db.execute(
                "SELECT 1 FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id AND q.organization_id=r.organization_id "
                "WHERE r.id=? AND r.organization_id=? AND q.topic_id=? AND q.requested_by=?",
                (run_id, org, topic, user),
            ).fetchone():
                raise StoreError(404, "file draft source run not found")
            # Reject an invalid base before the caller spends work drafting.
            # Existing client IDs above remain resumable after intervening edits.
            if self.store._postgres:
                self.store._postgres.lock(db.native, f"research-artifact:{org}:{project}:{path}")
            current = db.execute(
                "SELECT v.sha256 FROM research_artifacts a JOIN research_artifact_versions v "
                "ON v.artifact_id=a.id AND v.revision=a.head_revision "
                "WHERE a.organization_id=? AND a.project_id=? AND a.path=?", (org, project, path),
            ).fetchone()
            if current and expected_sha256 != current["sha256"]:
                raise StoreError(409, "artifact changed; read its current hash before starting a replacement draft")
            if not current and expected_sha256 is not None:
                raise StoreError(409, "artifact does not yet exist")
            now, identity = int(time.time()), _id()
            db.execute(
                "INSERT INTO research_artifact_drafts "
                "(id,organization_id,project_id,topic_id,created_by,client_id,path,expected_sha256,run_id,sha256,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, org, project, topic, user, client_id, path, expected_sha256, run_id, hashlib.sha256(b"").hexdigest(), now, now),
            )
            return self._metadata(db.execute("SELECT * FROM research_artifact_drafts WHERE id=?", (identity,)).fetchone())

    def list(self, user, org, topic):
        with self.store.transaction() as db:
            project = self.artifacts._topic(db, user, org, topic)
            return [self._metadata(row) for row in db.execute(
                "SELECT * FROM research_artifact_drafts WHERE organization_id=? AND project_id=? AND topic_id=? "
                "AND created_by=? AND result_json IS NULL ORDER BY updated_at DESC,id LIMIT 200", (org, project, topic, user),
            )]

    @staticmethod
    def _chunks(db, row):
        offset, size, count, digest = 0, 0, 0, hashlib.sha256()
        for chunk in db.execute("SELECT * FROM research_artifact_draft_chunks WHERE draft_id=? ORDER BY character_offset", (row["id"],)):
            body = chunk["text"].encode("utf8")
            if chunk["character_offset"] != offset or chunk["size_bytes"] != len(body) or (
                hashlib.sha256(body).hexdigest() != chunk["sha256"]
            ):
                raise StoreError(409, "file draft chunks do not match their retained content")
            digest.update(body)
            offset, size, count = offset + len(chunk["text"]), size + len(body), count + 1
            yield chunk["text"]
        if (offset, size, count, digest.hexdigest()) != (
            row["total_characters"], row["size_bytes"], row["chunk_count"], row["sha256"]
        ):
            raise StoreError(409, "file draft does not match its retained checksum")

    def append(self, user, org, topic, draft_id, offset, text):
        try:
            body = text.encode("utf8") if isinstance(text, str) else b""
        except UnicodeError:
            raise StoreError(400, "file draft chunk requires valid UTF-8 text") from None
        if type(offset) is not int or offset < 0 or not body or "\0" in text or len(body) > MAX_ARTIFACT_TEXT_BYTES:
            raise StoreError(400, "file draft chunk requires a nonnegative character offset and 1 to 256 KiB of UTF-8 text without NUL")
        with self.store.transaction(write=True) as db:
            row = self._load(db, user, org, topic, draft_id, write=True)
            old = db.execute("SELECT text FROM research_artifact_draft_chunks WHERE draft_id=? AND character_offset=?",
                             (draft_id, offset)).fetchone()
            if old:
                if old["text"] != text:
                    raise StoreError(409, "file draft chunk conflicts with content already saved at this offset")
                return self._metadata(row)
            if row["result_json"] or offset != row["total_characters"]:
                raise StoreError(409, "file draft is completed or its next offset changed; read its progress before appending")
            if row["size_bytes"] + len(body) > MAX_BYTES or row["chunk_count"] >= MAX_DRAFT_CHUNKS:
                raise StoreError(413, "file draft exceeds its 64 MiB or 4096 chunk allowance")
            digest = hashlib.sha256()
            for chunk in self._chunks(db, row):
                digest.update(chunk.encode("utf8"))
            digest.update(body)
            db.execute("INSERT INTO research_artifact_draft_chunks VALUES(?,?,?,?,?)",
                       (draft_id, offset, text, hashlib.sha256(body).hexdigest(), len(body)))
            db.execute(
                "UPDATE research_artifact_drafts SET total_characters=?,size_bytes=?,chunk_count=?,sha256=?,updated_at=? WHERE id=?",
                (offset + len(text), row["size_bytes"] + len(body), row["chunk_count"] + 1, digest.hexdigest(), int(time.time()), draft_id),
            )
            return self._metadata(db.execute("SELECT * FROM research_artifact_drafts WHERE id=?", (draft_id,)).fetchone())

    def read(self, user, org, topic, draft_id, offset=0, length=12000):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 16000:
            raise StoreError(400, "file draft slice requires a nonnegative offset and at most 16000 characters")
        with self.store.transaction() as db:
            row = self._load(db, user, org, topic, draft_id)
            text = []
            for chunk in db.execute(
                "SELECT * FROM research_artifact_draft_chunks WHERE draft_id=? AND character_offset<? "
                "AND character_offset+LENGTH(text)>? ORDER BY character_offset", (draft_id, offset + length, offset),
            ):
                body = chunk["text"].encode("utf8")
                if hashlib.sha256(body).hexdigest() != chunk["sha256"] or len(body) != chunk["size_bytes"]:
                    raise StoreError(409, "file draft chunks do not match their retained content")
                start = max(0, offset - chunk["character_offset"])
                end = min(len(chunk["text"]), offset + length - chunk["character_offset"])
                text.append(chunk["text"][start:end])
            value = "".join(text)
            if len(value) != min(length, max(0, row["total_characters"] - offset)):
                raise StoreError(409, "file draft has missing retained source chunks")
            return {**self._metadata(row), "text": value, "offset": offset, "next_offset": offset + len(value),
                    "truncated": offset + len(value) < row["total_characters"]}

    def content(self, user, org, topic, draft_id):
        with self.store.transaction() as db:
            row = self._load(db, user, org, topic, draft_id)
            return "".join(self._chunks(db, row)).encode("utf8"), row["sha256"]

    def finish(self, user, org, topic, draft_id, sha256, *, run_id=None):
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            raise StoreError(400, "file draft completion requires the exact source SHA-256")
        with self.store.transaction(write=True) as db:
            row = self._load(db, user, org, topic, draft_id, write=True)
            if sha256 != row["sha256"]:
                raise StoreError(409, "file draft source changed; read its current checksum before completing")
            publishing_run = run_id if run_id is not None else row["run_id"]
            if row["result_json"]:
                result = json.loads(row["result_json"])
                self.artifacts._record_publication(db, user, org, topic, publishing_run, result)
                return result
            text = "".join(self._chunks(db, row))
            saved = self.artifacts._write(db, user, org, row["project_id"], row["path"], text,
                                          row["expected_sha256"], topic=topic, run_id=publishing_run, strict=True)
            result = {**saved, **file_links(org, topic, row["path"], saved["revision"])}
            self.artifacts._record_publication(db, user, org, topic, publishing_run, saved)
            db.execute("UPDATE research_artifact_drafts SET result_json=?,updated_at=? WHERE id=?",
                       (json.dumps(result), int(time.time()), draft_id))
            return result


def register_drafts(research):
    from .research_tools import DEFAULT_TOOLS, schema, string

    def authorize(db):
        current = _row(db.execute(research.inference._joined() + "WHERE r.id=?", (research.row["id"],)).fetchone())
        if current["status"] != "running" or current["attempt_id"] != research.row["attempt_id"] or (
            research.inference._stop_reason(db, current)
        ):
            raise StoreError(409, "execution no longer authorized")
        topic = research.store._topic_access(db, research.user, research.org, research.topic, write=True)[4]
        if set(json.loads(topic["knowledge_policy_json"])) != research.collections:
            raise StoreError(409, "topic knowledge permissions changed")
        settings = db.execute("SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                              (research.org, research.topic)).fetchone()
        if (set(json.loads(settings[0])) if settings else set(DEFAULT_TOOLS)) != research.enabled:
            raise StoreError(409, "topic tool permissions changed")

    drafts = ResearchArtifactDrafts(research.store, authorize=authorize)
    scope = research.user, research.org, research.topic
    research.registry.tool(
        name="artifact_begin", risk="write",
        description="Start or resume an unfinished project text file. Use a stable client_id to recover the same draft after "
                    "an interruption. Append chunks at the returned character offset, then artifact_finish with its SHA-256. "
                    "Draft sessions are scoped to this user and topic; only finishing publishes a downloadable project revision.",
        input_schema=schema({"path": string(200), "client_id": string(200), "expected_sha256": string(64)}, ["path", "client_id"]),
    )(lambda path, client_id, expected_sha256=None: drafts.begin(
        *scope, path, client_id, expected_sha256, run_id=research.row["id"],
    ))
    research.registry.tool(
        name="artifact_append", risk="write",
        description="Durably append 1 to 256 KiB of UTF-8 text at an unfinished file's next character offset. "
                    "Retrying an identical chunk at its original offset is safe. Return the next offset and exact source SHA-256. "
                    "A draft accepts at most 64 MiB across 4096 chunks; already saved source survives a stopped run.",
        input_schema=schema({"draft_id": string(32), "offset": {"type": "integer", "minimum": 0},
                             "text": {"type": "string", "minLength": 1, "maxLength": MAX_ARTIFACT_TEXT_BYTES}},
                            ["draft_id", "offset", "text"]),
    )(lambda draft_id, offset, text: drafts.append(*scope, draft_id, offset, text))
    research.registry.tool(
        name="artifact_drafts",
        description="List this user's unfinished text-file drafts in the current topic, with next character offsets and source checksums.",
        input_schema=schema({}),
    )(lambda: drafts.list(*scope))
    research.registry.tool(
        name="artifact_draft_read",
        description="Read an exact bounded slice of this user's saved file draft; total_characters is the next append offset, "
                    "and next_offset continues reading. This does not publish or regenerate the source.",
        input_schema=schema({"draft_id": string(32), "offset": {"type": "integer", "minimum": 0},
                             "length": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["draft_id"]),
    )(lambda draft_id, offset=0, length=12000: drafts.read(*scope, draft_id, offset, length))
    research.registry.tool(
        name="artifact_finish", risk="write",
        description="Publish the complete saved draft as a project file after verifying its exact source SHA-256. "
                    "Return the pinned downloadable revision link; repeated finishing returns that same revision. "
                    "Intervening edits to the destination require a new draft with its current checksum.",
        input_schema=schema({"draft_id": string(32), "sha256": string(64)}, ["draft_id", "sha256"]),
    )(lambda draft_id, sha256: drafts.finish(*scope, draft_id, sha256, run_id=research.row["id"]))
