"""Content-addressed attachments; no remote fetches or host-side interpreters."""

import base64
import binascii
import hashlib
import json
import re
import time

from .store import StoreError, _id, _row

MAX_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 32768
MAX_ORGANIZATION_BYTES = 256 * 1024 * 1024
MAX_ORGANIZATION_FILES = 10000
MAX_PENDING_FILES = 32
BEGINS_PER_MINUTE = 30
MEDIA_READ_TOOLS = {"media_list", "media_read", "media_download_chunk"}
MIMES = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
REFERENCE = re.compile(r"/api/organizations/([a-f0-9]{32})/topics/([a-f0-9]{32})/media/([a-f0-9]{32})/([a-f0-9]{64})/content")
# Native models read extracted PDF text; CLI bots can also download exact bytes.
PDF_TEXT_NOTICE = (
    "Extracted text has [Page n] markers. Check extraction coverage and truncation; "
    "formulas, figures and layout may be missing. Use exact media text ranges or authorized original-byte download. "
    "For an arXiv paper, arxiv_read or arxiv_source_read provides a source alternative."
)
PDF_NO_TEXT_NOTICE = (
    "No readable PDF text is available. Inspect the extraction status and retry extraction where supported. "
    "A scanned or image-only document needs OCR or a TeX/text source. The original private PDF remains downloadable; "
    "native models have not inspected its pages. For an arXiv paper, use arxiv_read or arxiv_source_read."
)


def content_path(row):
    return f"/api/organizations/{row['organization_id']}/topics/{row['topic_id']}/media/{row['id']}/{row['sha256']}/content"


def view(row):
    value = {key: row[key] for key in ("id", "name", "mime", "sha256", "size_bytes", "topic_id", "created_at")}
    value["url"] = content_path(row)
    value["markdown"] = f"![{row['name']}]({value['url']})"
    if row["mime"] == "application/pdf":
        metadata = json.loads(dict(row).get("extraction_json", "{}"))
        value["extraction"] = {key: metadata[key] for key in (
            "status", "method", "error", "truncated", "pages_read", "total_pages"
        ) if key in metadata}
        if not value["extraction"]:
            value["extraction"] = {"status": "partial" if row["preview_text"] else "unavailable", "method": "browser"}
    return value


def valid_signature(body, mime):
    return {
        "image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": body.startswith(b"\xff\xd8\xff"),
        "image/webp": body.startswith(b"RIFF") and body[8:12] == b"WEBP",
        "application/pdf": body.startswith(b"%PDF-") and b"%%EOF" in body[-1024:],
    }.get(mime, False)


def image_dimensions(body, mime):
    """Header-only allocation guard; decoding is delegated to the browser, never the server."""
    if mime == "image/png" and body[12:16] == b"IHDR" and len(body) >= 24:
        return int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
    if mime == "image/webp":
        if body[12:16] == b"VP8X" and len(body) >= 30:
            return int.from_bytes(body[24:27], "little") + 1, int.from_bytes(body[27:30], "little") + 1
        if body[12:16] == b"VP8L" and len(body) >= 25 and body[20] == 0x2F:
            packed = int.from_bytes(body[21:25], "little")
            return (packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1
        if body[12:16] == b"VP8 " and len(body) >= 30 and body[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(body[26:28], "little") & 0x3FFF, int.from_bytes(body[28:30], "little") & 0x3FFF
    if mime == "image/jpeg":
        offset = 2
        while offset + 4 <= len(body) and body[offset] == 0xFF:
            marker = body[offset + 1]
            offset += 2
            if marker == 0xFF:
                offset -= 1
                continue
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            size = int.from_bytes(body[offset : offset + 2], "big")
            if size < 2 or offset + size > len(body):
                break
            if marker in {0xC0, 0xC1, 0xC2} and size >= 8:
                return int.from_bytes(body[offset + 5 : offset + 7], "big"), int.from_bytes(body[offset + 3 : offset + 5], "big")
            offset += size
    return 0, 0


def bounded_image(body, mime):
    width, height = image_dimensions(body, mime)
    return valid_signature(body, mime) and 1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 32_000_000


class ResearchMedia:
    def __init__(self, store, *, authorize=None):
        self.store = store
        self.authorize = authorize

    def _check(self, db):
        if self.authorize:
            self.authorize(db)

    def _row(self, db, user, org, topic, identity, *, write=False, complete=True):
        self._check(db)
        self.store._topic_access(db, user, org, topic, write=write)
        row = _row(
            db.execute("SELECT * FROM research_media WHERE organization_id=? AND topic_id=? AND id=?", (org, topic, identity)).fetchone()
        )
        if complete and not row["complete"]:
            raise StoreError(404, "attachment is not complete")
        if write and not row["complete"] and row["created_by"] != user:
            raise StoreError(404, "upload unavailable")
        return row

    def begin(self, user, org, topic, name, mime, size_bytes, sha256):
        try:
            if isinstance(name, str):
                name.encode("utf8")
        except UnicodeError:
            raise StoreError(400, "attachment filename requires valid UTF-8") from None
        if (
            mime not in MIMES
            or not isinstance(name, str)
            or not 1 <= len(name) <= 200
            or any(char in name for char in "/\\[]\r\n\0")
            or any(ord(char) < 32 for char in name)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 1 <= size_bytes <= MAX_BYTES
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", sha256)
        ):
            raise StoreError(400, "attach a named PNG, JPEG, WebP or PDF of at most 8 MiB with an exact SHA-256")
        with self.store.transaction(write=True) as db:
            self._check(db)
            project = self.store._topic_access(db, user, org, topic, write=True)[1]["id"]
            if self.store._postgres:
                self.store._postgres.lock(db.native, "research-media-quota:" + org)
            # Only unfinished uploads expire; a completed immutable reference never changes.
            db.execute(
                "DELETE FROM research_media WHERE organization_id=? AND created_by=? AND complete=0 AND created_at<?",
                (org, user, int(time.time()) - 3600),
            )
            files = db.execute("SELECT COUNT(*) FROM research_media WHERE organization_id=?", (org,)).fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM research_media WHERE organization_id=? AND created_by=? AND complete=0", (org, user)
            ).fetchone()[0]
            if files >= MAX_ORGANIZATION_FILES or pending >= MAX_PENDING_FILES:
                raise StoreError(413, "attachment count allowance reached: 10000 organization files or 32 pending uploads per uploader")
            quota, now = "media-begin:" + org + ":" + user, int(time.time())
            db.execute("DELETE FROM auth_attempts WHERE scope=? AND attempted_at<?", (quota, now - 60))
            if db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=?", (quota,)).fetchone()[0] >= BEGINS_PER_MINUTE:
                raise StoreError(429, "attachment creation rate is limited to 30 files per minute per uploader")
            used = db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM research_media WHERE organization_id=?", (org,)).fetchone()[0]
            if used + size_bytes > MAX_ORGANIZATION_BYTES:
                raise StoreError(413, "organization attachment storage allowance is 256 MiB")
            db.execute("INSERT INTO auth_attempts VALUES(?,?)", (quota, now))
            identity = _id()
            db.execute(
                "INSERT INTO research_media(id,organization_id,project_id,topic_id,created_by,name,mime,sha256,size_bytes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identity, org, project, topic, user, name, mime, sha256, size_bytes, int(time.time())),
            )
            return {"id": identity, "chunk_bytes": CHUNK_BYTES}

    def chunk(self, user, org, topic, identity, index, data_base64):
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < MAX_BYTES // CHUNK_BYTES
            or not isinstance(data_base64, str)
            or len(data_base64) > 4 * ((CHUNK_BYTES + 2) // 3)
        ):
            raise StoreError(400, "upload a bounded numbered attachment chunk")
        try:
            body = base64.b64decode(data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise StoreError(400, "attachment chunk requires base64") from None
        if not 1 <= len(body) <= CHUNK_BYTES:
            raise StoreError(400, "attachment chunk is empty or exceeds 32 KiB")
        with self.store.transaction(write=True) as db:
            row = self._row(db, user, org, topic, identity, write=True, complete=False)
            if self.store._postgres:
                self.store._postgres.lock(db.native, "research-media:" + identity)
            previous = db.execute(
                "SELECT data_base64 FROM research_media_chunks WHERE media_id=? AND chunk_index=?", (identity, index)
            ).fetchone()
            if previous:
                if previous[0] != data_base64:
                    raise StoreError(409, "an attachment chunk cannot be replaced")
                return {"id": identity, "index": index}
            if row["complete"]:
                raise StoreError(409, "a completed attachment cannot be changed")
            expected = min(CHUNK_BYTES, row["size_bytes"] - index * CHUNK_BYTES)
            if expected != len(body):
                raise StoreError(400, "attachment chunk differs from its declared size")
            db.execute("INSERT INTO research_media_chunks VALUES(?,?,?,?)", (identity, index, data_base64, len(body)))
            return {"id": identity, "index": index}

    @staticmethod
    def _bytes(db, row):
        body = b"".join(
            base64.b64decode(part[0])
            for part in db.execute("SELECT data_base64 FROM research_media_chunks WHERE media_id=? ORDER BY chunk_index", (row["id"],))
        )
        if len(body) != row["size_bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise StoreError(409, "attachment is incomplete or its checksum differs")
        return body

    def finish(self, user, org, topic, identity, preview_text="", vision_base64=""):
        try:
            valid_text = isinstance(preview_text, str) and len(preview_text.encode("utf8")) <= 64000 and "\0" not in preview_text
        except UnicodeError:
            valid_text = False
        if not valid_text:
            raise StoreError(400, "attachment extracted-text preview must be valid UTF-8 of at most 64,000 bytes without NUL")
        if not isinstance(vision_base64, str):
            raise StoreError(400, "vision preview requires base64")
        try:
            vision = base64.b64decode(vision_base64, validate=True)
        except (ValueError, binascii.Error):
            raise StoreError(400, "vision preview requires base64") from None
        if len(vision) > 32768 or (
            vision and (not bounded_image(vision, "image/jpeg") or max(image_dimensions(vision, "image/jpeg")) > 512)
        ):
            raise StoreError(400, "vision preview must be a JPEG of at most 32 KiB and 512 pixels per side")
        input_hash = hashlib.sha256(preview_text.encode()).hexdigest()

        def completed(row):
            metadata = json.loads(row["extraction_json"])
            expected = metadata.get("input_text_sha256", hashlib.sha256(row["preview_text"].encode()).hexdigest())
            if expected != input_hash or row["vision_base64"] != vision_base64:
                raise StoreError(409, "a completed attachment preview cannot be replaced")
            return view(row)

        # Parsing must not hold SQLite's write lock or a PostgreSQL transaction.
        with self.store.transaction() as db:
            row = self._row(db, user, org, topic, identity, write=True, complete=False)
            if row["complete"]:
                return completed(row)
            body = self._bytes(db, row)
            valid = bounded_image(body, row["mime"]) if row["mime"].startswith("image/") else valid_signature(body, row["mime"])
            if not valid:
                raise StoreError(400, "attachment content does not match its declared image/PDF format")
            if vision and not row["mime"].startswith("image/"):
                raise StoreError(400, "only image attachments can supply an image preview")
        metadata = {"input_text_sha256": input_hash}
        text = preview_text
        if row["mime"] == "application/pdf":
            text, extraction = self._extract(body, preview_text)
            metadata.update(extraction)
        with self.store.transaction(write=True) as db:
            if self.store._postgres:
                self.store._postgres.lock(db.native, "research-media:" + identity)
            row = self._row(db, user, org, topic, identity, write=True, complete=False)
            if row["complete"]:
                return completed(row)
            db.execute(
                "UPDATE research_media SET complete=1,preview_text=?,vision_base64=?,extraction_json=? WHERE id=?",
                (text, vision_base64, json.dumps(metadata), identity)
            )
            self.store._audit(db, org, user, "research.media", identity)
            return view(self._row(db, user, org, topic, identity))

    @staticmethod
    def _extract(body, fallback="", fallback_method="browser"):
        from .pdf_extract import extract_pdf

        result = extract_pdf(body)
        text = result.pop("text")
        result["method"] = "server"
        if not text and fallback:
            text = fallback
            result.update(status="partial", method=fallback_method, truncated=True)
        return text, result

    def extract_again(self, user, org, topic, identity, sha256):
        with self.store.transaction() as db:
            row = self._row(db, user, org, topic, identity, write=True)
            if row["sha256"] != sha256 or row["mime"] != "application/pdf":
                raise StoreError(404, "exact PDF attachment unavailable")
            body, fallback = self._bytes(db, row), row["preview_text"]
            method = json.loads(row["extraction_json"]).get("method", "browser")
        text, metadata = self._extract(body, fallback, method)
        with self.store.transaction(write=True) as db:
            row = self._row(db, user, org, topic, identity, write=True)
            original = json.loads(row["extraction_json"])
            metadata["input_text_sha256"] = original.get("input_text_sha256", hashlib.sha256(fallback.encode()).hexdigest())
            db.execute("UPDATE research_media SET preview_text=?,extraction_json=? WHERE id=?",
                       (text, json.dumps(metadata), identity))
            self.store._audit(db, org, user, "research.media.extract", identity)
            return view(self._row(db, user, org, topic, identity))

    def read(self, user, org, topic, identity, sha256, *, offset=0, length=12000, binary=False, source_topic_id=None):
        with self.store.transaction() as db:
            if source_topic_id and source_topic_id != topic:
                self._check(db)
                project = self.store._topic_access(db, user, org, topic)[1]["id"]
                if self.store._topic_access(db, user, org, source_topic_id)[1]["id"] != project:
                    raise StoreError(404, "attachment tools cannot cross project boundaries")
                topic = source_topic_id
            row = self._row(db, user, org, topic, identity)
            if row["sha256"] != sha256:
                raise StoreError(404, "exact attachment revision unavailable")
            if binary:
                return row["mime"], self._bytes(db, row)
            if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or not 1 <= length <= 12000:
                raise StoreError(400, "read a bounded extracted-text range")
            text = row["preview_text"]
            result = {
                **view(row),
                "text": text[offset : offset + length],
                "offset": offset,
                "total_characters": len(text),
                "next_offset": min(len(text), offset + length),
                "source": "extracted reference text; mathematical content and transcription remain unverified",
                "inspection": "image pixels require a vision-capable model"
                if row["mime"].startswith("image/")
                else "PDF text preview only; layout, diagrams and unextracted pages require direct inspection",
            }
            if row["mime"] == "application/pdf":
                method = json.loads(row["extraction_json"]).get("method", "browser")
                result["text_status"] = ("server_extracted" if method == "server" else "uploader_extracted") if text else "unavailable"
                result["notice"] = PDF_TEXT_NOTICE if text else PDF_NO_TEXT_NOTICE
            return result

    def list(self, user, org, topic):
        with self.store.transaction() as db:
            self._check(db)
            self.store._topic_access(db, user, org, topic)
            return [
                view(row)
                for row in db.execute(
                    "SELECT * FROM research_media WHERE organization_id=? AND topic_id=? AND complete=1 "
                    "ORDER BY created_at DESC,id LIMIT 200",
                    (org, topic),
                )
            ]

    def download_chunk(self, user, org, topic, identity, sha256, index=0, source_topic_id=None):
        if type(index) is not int or not 0 <= index < MAX_BYTES // CHUNK_BYTES:
            raise StoreError(400, "read a bounded numbered attachment chunk")
        with self.store.transaction() as db:
            if source_topic_id and source_topic_id != topic:
                self._check(db)
                project = self.store._topic_access(db, user, org, topic)[1]["id"]
                if self.store._topic_access(db, user, org, source_topic_id)[1]["id"] != project:
                    raise StoreError(404, "attachment tools cannot cross project boundaries")
                topic = source_topic_id
            row = self._row(db, user, org, topic, identity)
            if row["sha256"] != sha256:
                raise StoreError(404, "exact attachment revision unavailable")
            chunk = db.execute(
                "SELECT data_base64,size_bytes FROM research_media_chunks WHERE media_id=? AND chunk_index=?", (identity, index)
            ).fetchone()
            if chunk is None:
                raise StoreError(416, "attachment chunk is outside its byte range")
            body = base64.b64decode(chunk["data_base64"], validate=True)
            if len(body) != chunk["size_bytes"] or len(body) > CHUNK_BYTES:
                raise StoreError(409, "attachment chunk integrity differs")
            return {
                **view(row),
                "index": index,
                "chunk_bytes": CHUNK_BYTES,
                "data_base64": chunk["data_base64"],
                "chunk_sha256": hashlib.sha256(body).hexdigest(),
                "next_index": index + 1 if (index + 1) * CHUNK_BYTES < row["size_bytes"] else None,
            }


def register_media(research):
    from .research_tools import schema

    def authorize(db):
        current = db.execute(research.inference._joined() + "WHERE r.id=?", (research.row["id"],)).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["attempt_id"] != research.row["attempt_id"]
            or research.inference._stop_reason(db, dict(current))
        ):
            raise StoreError(409, "attachment tool execution is no longer authorized")
        topic = research.store._topic_access(db, research.user, research.org, research.topic, write=True)[4]
        import json

        settings = db.execute(
            "SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (research.org, research.topic)
        ).fetchone()
        if (settings and "knowledge" not in json.loads(settings[0])) or set(
            json.loads(topic["knowledge_policy_json"])
        ) != research.collections:
            raise StoreError(409, "attachment tool permissions changed")

    media = ResearchMedia(research.store, authorize=authorize)
    for name, description, properties, required, operation in media_tools():
        research.registry.tool(
            name=name,
            description=description,
            risk="read" if name in MEDIA_READ_TOOLS else "write",
            input_schema=schema(properties, required),
        )(lambda _operation=operation, **arguments: getattr(media, _operation)(research.user, research.org, research.topic, **arguments))


def media_tools():
    from .research_tools import string

    identity, sha = string(32), {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    return [
        ("media_list", "List complete attachments in this topic only; no organization-wide file dump.", {}, [], "list"),
        (
            "media_read",
            "Read an exact SHA-256 attachment's bounded extracted text and metadata. Image metadata is not pixel inspection.",
            {
                "identity": identity,
                "sha256": sha,
                "source_topic_id": identity,
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            ["identity", "sha256"],
            "read",
        ),
        (
            "media_download_chunk",
            "Retrieve one exact authorized attachment chunk for confined local inspection. "
            "Verify the complete file SHA-256 before use; binary bytes are not pixel interpretation.",
            {"identity": identity, "sha256": sha, "source_topic_id": identity, "index": {"type": "integer", "minimum": 0, "maximum": 255}},
            ["identity", "sha256"],
            "download_chunk",
        ),
        (
            "media_begin",
            "Begin an 8 MiB maximum PNG/JPEG/WebP/PDF upload in this topic; bytes remain private until checksum verification.",
            {
                "name": string(200),
                "mime": {"enum": sorted(MIMES)},
                "size_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_BYTES},
                "sha256": sha,
            },
            ["name", "mime", "size_bytes", "sha256"],
            "begin",
        ),
        (
            "media_chunk",
            "Upload one immutable numbered 32 KiB base64 chunk of a pending attachment. Exact retries are idempotent.",
            {"identity": identity, "index": {"type": "integer", "minimum": 0, "maximum": 255}, "data_base64": string(43692)},
            ["identity", "index", "data_base64"],
            "chunk",
        ),
        (
            "media_finish",
            "Verify an upload's hash and format and publish its immutable private Markdown reference. "
            "Optional extracted text is untrusted reference data.",
            {"identity": identity, "preview_text": {"type": "string", "maxLength": 24000}, "vision_base64": string(43692)},
            ["identity"],
            "finish",
        ),
    ]
