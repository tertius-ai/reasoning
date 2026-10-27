"""Pictures and PDFs shared in a direct conversation.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

The bytes live in their own tables, outside topic media, so no bot, model, search, export or text extraction
path reads them. Every operation requires the caller to be one of the conversation's two participants;
uploading also requires that the other participant can still receive messages. A message refers to a file by
its content path, exactly as topic messages refer to topic media.
"""

import base64
import hashlib
import time
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Response
from pydantic import Field

from .direct_messages import DirectMessages
from .request_input import RequestInput
from .research_media import CHUNK_BYTES, MAX_BYTES, bounded_image, check_chunk, check_upload, valid_signature
from .store import StoreError, _id, _row

MAX_ORGANIZATION_BYTES = 256 * 1024 * 1024
MAX_ORGANIZATION_FILES = 10000
MAX_PENDING_FILES = 32
BEGINS_PER_MINUTE = 30
MAX_LISTED = 500


def content_path(row):
    return (f"/api/organizations/{row['organization_id']}/direct-conversations/{row['conversation_id']}"
            f"/media/{row['id']}/{row['sha256']}/content")


def view(row):
    value = {key: row[key] for key in ("id", "name", "mime", "sha256", "size_bytes", "created_by", "created_at")}
    value["url"] = content_path(row)
    value["markdown"] = f"![{row['name']}]({value['url']})"
    return value


class DirectMedia:
    def __init__(self, store):
        self.store = store

    def _row(self, db, user, org, conversation_id, identity, *, write=False, complete=True):
        _, conversation = DirectMessages.access(db, user, org, conversation_id, write=write)
        row = _row(db.execute(
            "SELECT * FROM direct_media WHERE organization_id=? AND conversation_id=? AND id=?", (org, conversation_id, identity)
        ).fetchone())
        if complete and not row["complete"]:
            raise StoreError(404, "attachment is not complete")
        # Only the uploader sees or continues an unfinished upload.
        if not row["complete"] and row["created_by"] != user:
            raise StoreError(404, "upload unavailable")
        return row, conversation

    def begin(self, user, org, conversation_id, name, mime, size_bytes, sha256):
        check_upload(name, mime, size_bytes, sha256)
        with self.store.transaction(write=True) as db:
            _, conversation = DirectMessages.access(db, user, org, conversation_id, write=True)
            DirectMessages(self.store)._deliverable(db, org, user, conversation["other_user_id"])
            if self.store._postgres:
                self.store._postgres.lock(db.native, "direct-media-quota:" + org)
            db.execute(
                "DELETE FROM direct_media WHERE organization_id=? AND created_by=? AND complete=0 AND created_at<?",
                (org, user, int(time.time()) - 3600),
            )
            files = db.execute("SELECT COUNT(*) FROM direct_media WHERE organization_id=?", (org,)).fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM direct_media WHERE organization_id=? AND created_by=? AND complete=0", (org, user)
            ).fetchone()[0]
            if files >= MAX_ORGANIZATION_FILES or pending >= MAX_PENDING_FILES:
                raise StoreError(413, "attachment count allowance reached: 10000 direct-message files or 32 pending uploads per uploader")
            quota, now = "direct-media-begin:" + org + ":" + user, int(time.time())
            db.execute("DELETE FROM auth_attempts WHERE scope=? AND attempted_at<?", (quota, now - 60))
            if db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=?", (quota,)).fetchone()[0] >= BEGINS_PER_MINUTE:
                raise StoreError(429, "attachment creation rate is limited to 30 files per minute per uploader")
            used = db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM direct_media WHERE organization_id=?", (org,)).fetchone()[0]
            if used + size_bytes > MAX_ORGANIZATION_BYTES:
                raise StoreError(413, "organization direct-message storage allowance is 256 MiB")
            db.execute("INSERT INTO auth_attempts VALUES(?,?)", (quota, now))
            identity = _id()
            db.execute(
                "INSERT INTO direct_media(id,organization_id,conversation_id,created_by,name,mime,sha256,size_bytes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (identity, org, conversation_id, user, name, mime, sha256, size_bytes, now),
            )
            return {"id": identity, "chunk_bytes": CHUNK_BYTES}

    def chunk(self, user, org, conversation_id, identity, index, data_base64):
        body = check_chunk(index, data_base64)
        with self.store.transaction(write=True) as db:
            row, _ = self._row(db, user, org, conversation_id, identity, write=True, complete=False)
            if self.store._postgres:
                self.store._postgres.lock(db.native, "direct-media:" + identity)
            previous = db.execute(
                "SELECT data_base64 FROM direct_media_chunks WHERE media_id=? AND chunk_index=?", (identity, index)
            ).fetchone()
            if previous:
                if previous[0] != data_base64:
                    raise StoreError(409, "an attachment chunk cannot be replaced")
                return {"id": identity, "index": index}
            if row["complete"]:
                raise StoreError(409, "a completed attachment cannot be changed")
            if min(CHUNK_BYTES, row["size_bytes"] - index * CHUNK_BYTES) != len(body):
                raise StoreError(400, "attachment chunk differs from its declared size")
            db.execute("INSERT INTO direct_media_chunks VALUES(?,?,?,?)", (identity, index, data_base64, len(body)))
            return {"id": identity, "index": index}

    @staticmethod
    def _bytes(db, row):
        body = b"".join(
            base64.b64decode(part[0])
            for part in db.execute("SELECT data_base64 FROM direct_media_chunks WHERE media_id=? ORDER BY chunk_index", (row["id"],))
        )
        if len(body) != row["size_bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise StoreError(409, "attachment is incomplete or its checksum differs")
        return body

    def finish(self, user, org, conversation_id, identity):
        with self.store.transaction(write=True) as db:
            if self.store._postgres:
                self.store._postgres.lock(db.native, "direct-media:" + identity)
            row, conversation = self._row(db, user, org, conversation_id, identity, write=True, complete=False)
            if row["complete"]:
                return view(row)
            DirectMessages(self.store)._deliverable(db, org, user, conversation["other_user_id"])
            body = self._bytes(db, row)
            valid = bounded_image(body, row["mime"]) if row["mime"].startswith("image/") else valid_signature(body, row["mime"])
            if not valid:
                raise StoreError(400, "attachment content does not match its declared image, PDF or Markdown format")
            db.execute("UPDATE direct_media SET complete=1 WHERE id=?", (identity,))
            return view({**row, "complete": 1})

    def shared(self, user, org, conversation_id):
        """Files a message of this conversation shares, newest first. An upload never sent is not listed, so the other
        participant never learns of it; each file names the first message that shares it."""
        with self.store.transaction() as db:
            DirectMessages.access(db, user, org, conversation_id)
            rows = db.execute(
                "SELECT * FROM direct_media WHERE organization_id=? AND conversation_id=? AND complete=1 "
                "ORDER BY created_at DESC,id LIMIT ?", (org, conversation_id, MAX_LISTED),
            ).fetchall()
            files = []
            for row in rows:
                # Content paths hold only hex identifiers, '/' and letters, so they are literal LIKE patterns.
                message = db.execute(
                    "SELECT id,sequence,author_id,created_at FROM direct_messages WHERE organization_id=? AND conversation_id=? "
                    "AND content LIKE ? ORDER BY sequence LIMIT 1", (org, conversation_id, "%" + content_path(row) + "%"),
                ).fetchone()
                if message is not None:
                    files.append({**view(row), "message_id": message["id"], "message_sequence": message["sequence"],
                                  "shared_by": message["author_id"], "shared_at": message["created_at"]})
            return sorted(files, key=lambda item: (-item["message_sequence"], item["name"]))

    def read(self, user, org, conversation_id, identity, sha256, *, binary=False):
        with self.store.transaction() as db:
            row, _ = self._row(db, user, org, conversation_id, identity)
            if row["sha256"] != sha256:
                raise StoreError(404, "exact attachment revision unavailable")
            return (row["mime"], row["name"], self._bytes(db, row)) if binary else view(row)


class DirectMediaBegin(RequestInput):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    mime: str
    size_bytes: Annotated[int, Field(ge=1, le=MAX_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DirectMediaChunk(RequestInput):
    data_base64: Annotated[str, Field(min_length=1, max_length=43692)]


def install_routes(app, store, authenticated):
    media = DirectMedia(store)
    User = Annotated[str, Depends(authenticated)]
    root = "/api/organizations/{organization_id}/direct-conversations/{conversation_id}/media"

    @app.get(root)
    def shared(organization_id: str, conversation_id: str, user: User):
        return media.shared(user, organization_id, conversation_id)

    @app.post(root)
    def begin(organization_id: str, conversation_id: str, body: DirectMediaBegin, user: User):
        return media.begin(user, organization_id, conversation_id, **body.model_dump())

    @app.put(root + "/{identity}/chunks/{index}")
    def chunk(organization_id: str, conversation_id: str, identity: str, index: int, body: DirectMediaChunk, user: User):
        return media.chunk(user, organization_id, conversation_id, identity, index, body.data_base64)

    @app.post(root + "/{identity}/finish")
    def finish(organization_id: str, conversation_id: str, identity: str, user: User):
        return media.finish(user, organization_id, conversation_id, identity)

    @app.get(root + "/{identity}/{sha256}")
    def metadata(organization_id: str, conversation_id: str, identity: str, sha256: str, user: User):
        return media.read(user, organization_id, conversation_id, identity, sha256)

    @app.get(root + "/{identity}/{sha256}/content")
    def content(organization_id: str, conversation_id: str, identity: str, sha256: str, user: User, download: bool = False):
        mime, name, body = media.read(user, organization_id, conversation_id, identity, sha256, binary=True)
        return Response(
            body,
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": ("attachment" if download else "inline") + "; filename*=UTF-8''" + quote(name, safe=""),
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            },
        )
