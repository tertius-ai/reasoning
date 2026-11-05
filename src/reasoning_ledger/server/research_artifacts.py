"""Versioned text artifacts shared within a research project."""

import hashlib
import json
import re
import time
from urllib.parse import urlencode

from .research_file_limits import MAX_ARTIFACT_TEXT_BYTES
from .store import StoreError, _id, _row

PATH = re.compile(r"[A-Za-z0-9_.-][A-Za-z0-9._/-]{0,199}\Z")
# 256 sections of the direct-save maximum fill the 64 MiB file allowance.
MAX_ASSEMBLY_PARTS = 256


def safe_path(path):
    return isinstance(path, str) and PATH.fullmatch(path) and all(part not in {"", ".", ".."} for part in path.split("/"))


def file_links(org, topic, path, revision):
    query = urlencode({"path": path, "revision": revision})
    url = f"/api/organizations/{org}/topics/{topic}/research-files/content?{query}"
    return {"url": url, "download_url": url + "&download=true",
            "markdown": f"[{path} (revision {revision})]({url})"}


BOT_REPLY = re.compile(r"bot-run-([0-9a-f]{32})-([0-9]+)")


def reply_files(db, message):
    """Project files a reply's own run saved: the last saved revision of each, in save order.
    Atomic publication receipts survive a failed outer tool receipt. Section revisions that the
    run joined into an assembled file are listed only through that file. A bot reply split into
    several messages lists files under its last part.
    """
    org, topic = message["organization_id"], message["topic_id"]
    consumed = set()
    if message.get("inference_run_id"):
        rows = db.execute(
            "SELECT name,arguments_json,result_json FROM research_tool_receipts WHERE organization_id=? AND run_id=? "
            "AND status='completed' AND name IN ('artifact_write','artifact_assemble','tool_call') ORDER BY sequence",
            (org, message["inference_run_id"]),
        ).fetchall()
        results = []
        for row in rows:
            result = json.loads(row["result_json"] or "null")
            if row["name"] == "tool_call":
                actual = json.loads(row["arguments_json"]).get("name")
                if actual not in {"artifact_write", "artifact_assemble"} or not isinstance(result, dict):
                    continue
                result = result.get("result")
            results.append(result)
        for row in db.execute(
            "SELECT a.path,p.revision,p.assembled_from_json FROM research_artifact_publications p "
            "JOIN research_artifacts a ON a.id=p.artifact_id AND a.organization_id=p.organization_id "
            "WHERE p.organization_id=? AND p.run_id=? ORDER BY p.sequence",
            (org, message["inference_run_id"]),
        ):
            results.append({"path": row["path"], "revision": row["revision"]})
            consumed.update((part["path"], part["revision"]) for part in json.loads(row["assembled_from_json"] or "[]"))
        results.extend(json.loads(row[0]) for row in db.execute(
            "SELECT result_json FROM research_artifact_recoveries WHERE organization_id=? AND run_id=? ORDER BY event_sequence,tool_index",
            (org, message["inference_run_id"]),
        ))
    else:
        match = BOT_REPLY.fullmatch(message.get("client_id") or "")
        if not match or message.get("author_kind", message.get("sender_kind")) != "bot":
            return []
        run_id, index = match.group(1), int(match.group(2))
        results = [json.loads(row[0]) for row in db.execute(
            "SELECT result_json FROM bot_tool_receipts WHERE run_id=? AND name='artifact_write' ORDER BY sequence", (run_id,))]
        if not results or db.execute("SELECT 1 FROM messages WHERE organization_id=? AND topic_id=? AND client_id=?",
                                     (org, topic, f"bot-run-{run_id}-{index + 1}")).fetchone():
            return []
    saved = {}
    for result in results:
        if isinstance(result, dict) and safe_path(result.get("path")) and type(result.get("revision")) is int:
            if saved.get(result["path"], 0) >= result["revision"]:
                continue  # A retry can return an older pinned revision without saving it again.
            saved.pop(result["path"], None)
            saved[result["path"]] = result["revision"]
    files = []
    for path, revision in saved.items():
        if (path, revision) in consumed:
            continue
        # Each receipt is confirmed against the retained revision of this topic's project.
        row = db.execute(
            "SELECT v.sha256,COALESCE(o.total_characters,LENGTH(v.text)) AS total_characters FROM research_artifacts a "
            "JOIN research_artifact_versions v ON v.artifact_id=a.id AND v.revision=? "
            "LEFT JOIN research_artifact_blobs b ON b.artifact_id=a.id AND b.revision=v.revision "
            "LEFT JOIN research_blob_objects o ON o.organization_id=b.organization_id "
            "AND o.project_id=b.project_id AND o.digest=b.digest "
            "WHERE a.organization_id=? AND a.path=? AND a.project_id="
            "(SELECT c.project_id FROM topics t JOIN channels c ON c.id=t.channel_id WHERE t.organization_id=? AND t.id=?)",
            (revision, org, path, org, topic),
        ).fetchone()
        if row:
            files.append({"path": path, "revision": revision, "sha256": row["sha256"],
                          "total_characters": row["total_characters"], **file_links(org, topic, path, revision)})
    return files


class ResearchArtifacts:
    def __init__(self, store, *, authorize=None):
        self.store = store
        self.authorize = authorize

    def _topic(self, db, user, org, topic, write=False):
        if self.authorize:
            self.authorize(db)
        _, project, _, _, _ = self.store._topic_access(db, user, org, topic, write=write)
        return project["id"]

    def list(self, user, org, topic):
        with self.store.transaction() as db:
            project = self._topic(db, user, org, topic)
            return [
                {**dict(row), **file_links(org, topic, row["path"], row["head_revision"])}
                for row in db.execute(
                    "SELECT a.*,v.sha256,COALESCE(o.total_characters,LENGTH(v.text)) AS total_characters,v.created_at "
                    "FROM research_artifacts a "
                    "JOIN research_artifact_versions v ON v.artifact_id=a.id AND v.revision=a.head_revision "
                    "LEFT JOIN research_artifact_blobs b ON b.artifact_id=a.id AND b.revision=v.revision "
                    "LEFT JOIN research_blob_objects o ON o.organization_id=b.organization_id "
                    "AND o.project_id=b.project_id AND o.digest=b.digest "
                    "WHERE a.organization_id=? AND a.project_id=? ORDER BY v.created_at DESC,a.path LIMIT 200",
                    (org, project),
                )
            ]

    def read(self, user, org, topic, path, revision=None, offset=0, length=12000):
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**63 - 1):
            raise StoreError(400, "select an exact saved file revision")
        with self.store.transaction() as db:
            project = self._topic(db, user, org, topic)
            row = _row(
                db.execute(
                    "SELECT a.*,v.text,v.sha256,v.revision,v.created_at FROM research_artifacts a "
                    "JOIN research_artifact_versions v ON v.artifact_id=a.id AND v.revision=COALESCE(?,a.head_revision) "
                    "WHERE a.organization_id=? AND a.project_id=? AND a.path=?",
                    (revision, org, project, path),
                ).fetchone()
            )
            row.pop("text")
            from .artifact_chunks import read

            return {**row, **read(db, row["id"], row["revision"], offset, length),
                    **file_links(org, topic, path, row["revision"])}

    def content(self, user, org, topic, path, revision):
        from .artifact_chunks import full

        if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
            raise StoreError(400, "select an exact saved file revision")
        with self.store.transaction() as db:
            project = self._topic(db, user, org, topic)
            row = _row(db.execute("SELECT a.id,v.sha256 FROM research_artifacts a JOIN research_artifact_versions v "
                                  "ON v.artifact_id=a.id AND v.revision=? "
                                  "WHERE a.organization_id=? AND a.project_id=? AND a.path=?",
                                  (revision, org, project, path)).fetchone())
            return full(db, row["id"], revision).encode("utf8"), row["sha256"]

    def write(self, user, org, topic, path, text, expected_sha256=None, run_id=None, *, actor_id=None):
        try:
            valid = isinstance(text, str) and len(text.encode("utf8")) <= MAX_ARTIFACT_TEXT_BYTES and "\0" not in text
        except UnicodeError:
            valid = False
        if not safe_path(path) or not valid:
            raise StoreError(400, "artifact requires a safe relative name and at most 256 KiB of UTF-8 text")
        with self.store.transaction(write=True) as db:
            project = self._topic(db, user, org, topic, write=True)
            saved = self._write(db, actor_id or user, org, project, path, text, expected_sha256, topic=topic, run_id=run_id)
            self._record_publication(db, user, org, topic, run_id, saved)
            return {**saved, **file_links(org, topic, path, saved["revision"])}

    def assemble(self, user, org, topic, path, parts, expected_sha256=None, run_id=None):
        """Join pinned revisions of this project's text files, in order and byte for byte, into one file.
        The sections stay saved, so a failed or conflicting assembly is simply repeated; nothing is resent."""
        if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_ASSEMBLY_PARTS or not all(
            isinstance(part, dict) and part.keys() == {"path", "revision"} and safe_path(part["path"])
            and type(part["revision"]) is int and 1 <= part["revision"] <= 2**63 - 1 for part in parts
        ):
            raise StoreError(400, f"assembly requires 1 to {MAX_ASSEMBLY_PARTS} parts, each a saved file path and revision")
        if not safe_path(path):
            raise StoreError(400, "artifact requires a safe relative path")
        from .artifact_chunks import MAX_BYTES, full

        with self.store.transaction(write=True) as db:
            project = self._topic(db, user, org, topic, write=True)
            texts, size = [], 0
            for part in parts:
                row = db.execute(
                    "SELECT a.id FROM research_artifacts a JOIN research_artifact_versions v ON v.artifact_id=a.id AND v.revision=? "
                    "WHERE a.organization_id=? AND a.project_id=? AND a.path=?", (part["revision"], org, project, part["path"]),
                ).fetchone()
                if row is None:
                    raise StoreError(404, f"assembly part {part['path']} revision {part['revision']} is not saved in this project")
                texts.append(full(db, row["id"], part["revision"]))
                size += len(texts[-1].encode("utf8"))
                if size > MAX_BYTES:
                    raise StoreError(413, "assembled file exceeds its 64 MiB storage allowance")
            saved = self._write(db, user, org, project, path, "".join(texts), expected_sha256, topic=topic, run_id=run_id)
            self._record_publication(db, user, org, topic, run_id, saved, parts)
            return {**saved, "parts": len(parts), "size_bytes": size, **file_links(org, topic, path, saved["revision"])}

    @staticmethod
    def _record_publication(db, user, org, topic, run_id, saved, parts=None):
        if run_id is None:
            return
        if not db.execute(
            "SELECT 1 FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id AND q.organization_id=r.organization_id "
            "WHERE r.organization_id=? AND r.id=? AND q.topic_id=? AND q.requested_by=?",
            (org, run_id, topic, user),
        ).fetchone():
            raise StoreError(404, "file publication source run not found")
        # Commit the saved revision's run binding with the file itself, before
        # the generic tool runner can be cancelled or lose its completion receipt.
        db.execute(
            "INSERT OR IGNORE INTO research_artifact_publications(organization_id,run_id,artifact_id,revision,assembled_from_json) "
            "VALUES(?,?,?,?,?)",
            (org, run_id, saved["id"], saved["revision"], json.dumps(parts) if parts else None),
        )

    def _write(self, db, user, org, project, path, text, expected_sha256=None, *, topic=None, run_id=None, strict=False):
        from .artifact_chunks import MAX_BYTES, put

        if not safe_path(path) or not isinstance(text, str) or "\0" in text:
            raise StoreError(400, "artifact requires a safe relative path and UTF-8 text without NUL")
        try:
            if len(text.encode("utf8")) > MAX_BYTES:
                raise StoreError(413, "artifact exceeds its 64 MiB storage allowance")
        except UnicodeError:
            raise StoreError(400, "artifact requires valid UTF-8 text") from None
        if self.store._postgres:
            self.store._postgres.lock(db.native, f"research-artifact:{org}:{project}:{path}")
        checksum = hashlib.sha256(text.encode()).hexdigest()
        old = db.execute(
            "SELECT a.*,v.sha256 FROM research_artifacts a JOIN research_artifact_versions v "
            "ON v.artifact_id=a.id AND v.revision=a.head_revision "
            "WHERE a.organization_id=? AND a.project_id=? AND a.path=?",
            (org, project, path),
        ).fetchone()
        if old:
            if strict and expected_sha256 != old["sha256"]:
                raise StoreError(409, "artifact changed; read its current hash before replacing it")
            if checksum == old["sha256"]:
                return {"id": old["id"], "path": path, "revision": old["head_revision"], "sha256": checksum}
            if expected_sha256 != old["sha256"]:
                raise StoreError(409, "artifact changed; read its current hash before replacing it")
            identity, revision = old["id"], old["head_revision"] + 1
            db.execute("UPDATE research_artifacts SET head_revision=? WHERE id=?", (revision, identity))
        else:
            if expected_sha256 is not None:
                raise StoreError(409, "artifact does not yet exist")
            identity, revision = _id(), 1
            db.execute("INSERT INTO research_artifacts VALUES(?,?,?,?,?,?)", (identity, org, project, topic, path, revision))
        large = len(text.encode("utf8")) > 256 * 1024
        db.execute(
            "INSERT INTO research_artifact_versions VALUES(?,?,?,?,?,?,?)",
            (identity, revision, "" if large else text, checksum, user, run_id, int(time.time())),
        )
        if large:
            put(db, org, project, text)
            db.execute("INSERT INTO research_artifact_blobs VALUES(?,?,?,?,?)", (identity, revision, org, project, checksum))
        self.store._audit(db, org, user, "research.artifact", identity)
        return {"id": identity, "path": path, "revision": revision, "sha256": checksum}


def register_artifacts(research):
    from .research_tools import schema, string

    artifacts = ResearchArtifacts(research.store)
    research.registry.tool(
        name="artifact_list",
        description="List versioned research files shared by this project; no local filesystem access.",
        input_schema=schema({}),
    )(lambda: artifacts.list(research.user, research.org, research.topic))
    research.registry.tool(
        name="artifact_write",
        description="Save a persistent project text file of at most 256 KiB UTF-8. Return its clickable revision link to the user. "
                    "Overwrites require the current SHA-256; all revisions are retained. For a longer document, save it as "
                    "section files and join them with artifact_assemble; saved sections survive interruptions.",
        risk="write",
        input_schema=schema(
            {"path": string(200), "text": {"type": "string", "maxLength": MAX_ARTIFACT_TEXT_BYTES},
             "expected_sha256": string(64)}, ["path", "text"]
        ),
    )(
        lambda path, text, expected_sha256=None: artifacts.write(
            research.user, research.org, research.topic, path, text, expected_sha256, research.row["id"]
        )
    )
    research.registry.tool(
        name="artifact_assemble",
        description="Publish one project text file of up to 64 MiB by joining saved file revisions in order, byte for byte, "
                    "without resending their text. Save each section with artifact_write first (end sections with a newline "
                    "where the format needs one), then pass their paths and returned revisions. Overwrites require the "
                    "destination's current SHA-256; after a conflict, assemble again with the new hash. Repeating an "
                    "assembly returns the same revision. Return the assembled file's revision link to the user.",
        risk="write",
        input_schema=schema(
            {"path": string(200), "expected_sha256": string(64),
             "parts": {"type": "array", "minItems": 1, "maxItems": MAX_ASSEMBLY_PARTS, "items": schema(
                 {"path": string(200), "revision": {"type": "integer", "minimum": 1}}, ["path", "revision"])}},
            ["path", "parts"],
        ),
    )(
        lambda path, parts, expected_sha256=None: artifacts.assemble(
            research.user, research.org, research.topic, path, parts, expected_sha256, research.row["id"]
        )
    )
    research.registry.tool(
        name="research_file_read",
        description="Read an exact bounded revision of a shared research file; returns its checksum and recovery offsets.",
        input_schema=schema(
            {
                "path": string(200),
                "revision": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            ["path"],
        ),
    )(
        lambda path, revision=None, offset=0, length=12000: artifacts.read(
            research.user, research.org, research.topic, path, revision, offset, length
        )
    )
